#!/usr/bin/env python3
"""
findex - local filename + full-text index for Windows.

Commands:
    findex index [ROOT ...]     Build or update the index
    findex watch [ROOT ...]     Live updates: index changes as they happen
    findex find "QUERY"         Everything-style search (content:, C:\\, ext:, !)
    findex search "QUERY"       Full-text search of file contents
    findex name "PATTERN"       Filename search (substring or *wildcard*)
    findex dupes                Duplicate files (same name and size)
    findex stats                Index statistics
    findex vacuum               Compact the database
    findex clear                Delete the index and start fresh
    findex gui                  Open the desktop app (findex_gui.py)

EVERY file AND folder under the indexed roots is recorded by name, size and
date, so filename search covers the whole drive - like Everything does. Text
extraction on top of that is limited to the types in DOC_EXTS/TEXT_EXTS.

The database lives next to this script as findex.db unless --db is given.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# realpath, not abspath: the folder must work when launched through a
# symlink, a shortcut, or from a drive mounted under a different letter.
# In a standalone (.exe) build, "the folder" is wherever the exe lives.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.realpath(sys.executable))
else:
    HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_DB = os.path.join(HERE, "findex.db")

# Per-file cap on extracted text. 400k chars is roughly a 150-page book.
MAX_TEXT_CHARS = 400000

# Files larger than this are recorded by name but their contents not opened.
MAX_FILE_BYTES = 300 * 1024 * 1024

# Extraction tasks dispatched to the worker pool per batch. Keeps memory flat.
CHUNK = 4000

# Name-only records written per transaction.
NAME_CHUNK = 8000

# OCR of scanned PDFs (opt-in via --ocr): pages are rendered and read with
# tesseract. Capped so one huge scan cannot stall the whole run.
OCR_MAX_PAGES = 20
OCR_ENABLED = os.environ.get("FINDEX_OCR") == "1"

DOC_EXTS = {".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm", ".rtf",
            ".epub", ".odt", ".ods", ".odp",     # ebooks + LibreOffice
            ".doc", ".xls", ".ppt",              # pre-2007 Office (best effort)
            ".eml",                              # saved emails
            ".zip", ".cbz"}                      # archives: member names
MSG_EXTS = {".msg"}                              # Outlook (needs extract-msg)
AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus",
              ".wma", ".wav", ".aiff", ".mp4", ".m4v", ".mov"}  # tags (mutagen)
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".html", ".htm",
    ".ini", ".cfg", ".conf", ".yml", ".yaml", ".py", ".js", ".ts", ".css",
    ".c", ".h", ".cpp", ".cs", ".java", ".sql", ".ps1", ".bat", ".cmd", ".sh",
}
INDEXABLE = DOC_EXTS | TEXT_EXTS | MSG_EXTS | AUDIO_EXTS

SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "node_modules", "__pycache__", ".git", ".svn", ".hg", ".venv", "venv",
    ".venv-win", ".venv-mac", ".venv-linux",
    "env", "site-packages", "appdata", ".cache", ".gradle", ".nuget",
    "windowsapps", "msocache",
}

# Windows file attributes for OneDrive / cloud placeholder files.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_MASK = (FILE_ATTRIBUTE_OFFLINE
              | FILE_ATTRIBUTE_RECALL_ON_OPEN
              | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

# Libraries bundled inside the folder: pure-Python components ship with the
# app in vendor/, so a fresh clone needs no installs for tags or .msg files.
_VENDOR = os.path.join(HERE, "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

try:
    import pymupdf as fitz             # modern import name - no warning
    HAVE_FITZ = True
except ImportError:
    try:
        import fitz                    # older PyMuPDF releases
        HAVE_FITZ = True
    except ImportError:
        HAVE_FITZ = False

if HAVE_FITZ:
    try:    # corrupt PDFs otherwise spam stderr with raw "MuPDF error" lines;
            # real failures are still raised and recorded per file
        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
    except Exception:
        pass

try:
    import mutagen                     # audio/video tags (optional)
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

try:
    import extract_msg                 # Outlook .msg (optional)
    HAVE_MSG = True
except ImportError:
    HAVE_MSG = False


def can_extract(ext):
    """Is content extraction possible for this type on this machine?
    Types whose optional library is missing simply stay name-only."""
    if ext in MSG_EXTS:
        return HAVE_MSG
    if ext in AUDIO_EXTS:
        return HAVE_MUTAGEN
    return ext in DOC_EXTS or ext in TEXT_EXTS


# ----------------------------------------------------------------------------
# Path helpers
# ----------------------------------------------------------------------------

def lp(path):
    """Prefix long Windows paths so they survive the 260-character limit."""
    if os.name != "nt" or len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


# ----------------------------------------------------------------------------
# Text extraction (runs inside worker processes)
# ----------------------------------------------------------------------------

_TAG = re.compile(rb"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v\u00a0]+")
_NL = re.compile(r"\n{3,}")


def _xml_text(data):
    """Strip XML tags, keeping paragraph breaks where the format marks them."""
    data = data.replace(b"</w:p>", b"\n</w:p>")
    data = data.replace(b"</a:p>", b"\n</a:p>")
    data = data.replace(b"</text:p>", b"\n</text:p>")
    data = data.replace(b"</p>", b"\n</p>")
    data = data.replace(b"<w:br/>", b"\n")
    txt = _TAG.sub(b" ", data).decode("utf-8", "ignore")
    return html.unescape(txt)


def _zip_parts(path, wanted):
    """Pull the named XML parts out of an OOXML package and flatten to text."""
    out = []
    total = 0
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if wanted(n)]
        names.sort(key=lambda n: (len(n), n))
        for n in names:
            chunk = _xml_text(z.read(n))
            out.append(chunk)
            total += len(chunk)
            if total > MAX_TEXT_CHARS:
                break
    return "\n".join(out)


def _tesseract_bin():
    """Locate tesseract. Apps launched from Finder get a bare PATH, so the
    usual install locations are checked as well."""
    import shutil
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(cand):
            return cand
    return ""


def _ocr_vision(png):
    """macOS: Apple's Vision OCR, built into the operating system."""
    if sys.platform != "darwin":
        return None
    try:
        import Vision
        from Foundation import NSData
    except ImportError:
        return None
    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        data, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    try:
        request.setRecognitionLevel_(
            Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
    except Exception:
        pass
    ok = handler.performRequests_error_([request], None)
    if isinstance(ok, tuple):
        ok = ok[0]
    if not ok:
        return None
    out = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if cands and len(cands):
            out.append(str(cands[0].string()))
    return "\n".join(out) or None


def _ocr_windows(png):
    """Windows 10/11: the OCR engine built into the operating system.
    Reached through the maintained winrt-* packages, or the older winsdk
    package when that happens to be installed."""
    if os.name != "nt":
        return None
    try:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import (DataWriter,
                                                   InMemoryRandomAccessStream)
    except ImportError:
        try:
            from winsdk.windows.graphics.imaging import BitmapDecoder
            from winsdk.windows.media.ocr import OcrEngine
            from winsdk.windows.storage.streams import (
                DataWriter, InMemoryRandomAccessStream)
        except ImportError:
            return None
    import asyncio

    async def _run():
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(png)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return None
        result = await engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    try:
        return asyncio.run(_run()) or None
    except Exception:
        return None


def _ocr_tesseract(png):
    """Fallback: the tesseract program, when installed."""
    import subprocess
    tess = _tesseract_bin()
    if not tess:
        return None
    try:
        r = subprocess.run([tess, "stdin", "stdout", "--psm", "3"],
                           input=png, capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "ignore") or None
    except Exception:
        return None


def _ocr_png(png):
    """Read the text in one page image, using whichever engine works here:
    the OS's own OCR first, tesseract as a fallback."""
    for backend in (_ocr_vision, _ocr_windows, _ocr_tesseract):
        try:
            text = backend(png)
        except Exception:
            text = None
        if text and text.strip():
            return text
    return ""


def have_ocr_backend():
    """Is ANY OCR engine available on this machine?"""
    if sys.platform == "darwin":
        try:
            import Vision                      # noqa: F401
            return True
        except ImportError:
            pass
    if os.name == "nt":
        for mod in ("winrt.windows.media.ocr", "winsdk.windows.media.ocr"):
            try:
                __import__(mod)
                return True
            except Exception:
                pass
    return bool(_tesseract_bin())



def _pdf_ocr(path):
    """Render the first OCR_MAX_PAGES pages and read them with the best
    available OCR engine."""
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= OCR_MAX_PAGES:
                break
            png = page.get_pixmap(dpi=150).tobytes("png")
            text = _ocr_png(png)
            if text:
                out.append(text)
    return "\n".join(out)


def _pdf(path):
    if not HAVE_FITZ:
        raise RuntimeError("PyMuPDF not installed - run: pip install pymupdf")
    out = []
    total = 0
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise RuntimeError("password protected")
        for page in doc:
            t = page.get_text("text")
            out.append(t)
            total += len(t)
            if total > MAX_TEXT_CHARS:
                break
    text = "\n".join(out)
    # A "PDF" with almost no text layer is a scan: OCR it if enabled.
    # Whatever real text existed is kept and the OCR result added to it.
    if OCR_ENABLED and len(text.strip()) < 100:
        ocr = _pdf_ocr(path)
        if ocr.strip():
            text = (text + "\n" + ocr).strip()
    return text


def _epub(path):
    def wanted(n):
        return n.lower().endswith((".xhtml", ".html", ".htm", ".opf"))
    return _zip_parts(path, wanted)


def _odf(path):
    def wanted(n):
        return n in ("content.xml", "meta.xml")
    return _zip_parts(path, wanted)


_ANSI_RUN = re.compile(rb"[\x20-\x7e]{4,}")
_WIDE_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
_WORDY = re.compile(r"[A-Za-z]{3,}")


def _legacy_office(path):
    """Best-effort text scrape of binary .doc/.xls/.ppt - readable words come
    out, formatting junk is filtered. Good enough for search."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT_CHARS * 8)
    out = []
    for m in _WIDE_RUN.finditer(raw):
        s = m.group().decode("utf-16-le", "ignore")
        if _WORDY.search(s):
            out.append(s)
    for m in _ANSI_RUN.finditer(raw):
        s = m.group().decode("cp1252", "ignore")
        if _WORDY.search(s):
            out.append(s)
    return "\n".join(out)


def _eml(path):
    import email
    from email import policy
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    out = ["{}: {}".format(h, msg.get(h)) for h in
           ("From", "To", "Cc", "Subject", "Date") if msg.get(h)]
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except Exception:
            content = ""
        if body.get_content_type() == "text/html":
            content = _TAG.sub(b" ", content.encode("utf-8", "ignore")
                               ).decode("utf-8", "ignore")
            content = html.unescape(content)
        out.append(content)
    return "\n".join(out)


def _msgfile(path):
    if not HAVE_MSG:
        raise RuntimeError("extract-msg not installed - pip install extract-msg")
    m = extract_msg.Message(path)
    try:
        parts = (m.sender, m.to, m.subject,
                 str(m.date) if m.date else None, m.body)
        return "\n".join(p for p in parts if p)
    finally:
        try:
            m.close()
        except Exception:
            pass


def _audio(path):
    if not HAVE_MUTAGEN:
        raise RuntimeError("mutagen not installed - pip install mutagen")
    m = mutagen.File(path, easy=True)
    if m is None or not getattr(m, "tags", None):
        return ""
    out = []
    for key in ("title", "artist", "albumartist", "album", "genre", "date",
                "composer", "comment", "tracknumber"):
        try:
            values = m.tags.get(key) or []
        except Exception:
            values = []
        for v in values:
            out.append("{}: {}".format(key, v))
    return "\n".join(out)


def _zipnames(path):
    """The file names inside an archive, so you can find which zip holds a
    file without opening anything."""
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.namelist()[:20000])


def _docx(path):
    def wanted(n):
        return (n in ("word/document.xml", "word/footnotes.xml",
                      "word/endnotes.xml", "word/comments.xml")
                or n.startswith("word/header")
                or n.startswith("word/footer"))
    return _zip_parts(path, wanted)


def _xlsx(path):
    # Only the shared string table. Sheet XML is mostly numeric cell values and
    # shared-string indices, which would poison the index with noise digits.
    def wanted(n):
        return n == "xl/sharedStrings.xml" or n.startswith("xl/comments")
    return _zip_parts(path, wanted)


def _pptx(path):
    def wanted(n):
        return ((n.startswith("ppt/slides/slide") and n.endswith(".xml"))
                or (n.startswith("ppt/notesSlides/") and n.endswith(".xml")))
    return _zip_parts(path, wanted)


_RTF_HEX = re.compile(r"\\'[0-9a-fA-F]{2}")
_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?")
_RTF_BRACE = re.compile(r"[{}]")


def _rtf(path):
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT_CHARS * 4).decode("latin-1", "ignore")
    raw = _RTF_HEX.sub(" ", raw)
    raw = _RTF_CTRL.sub(" ", raw)
    return _RTF_BRACE.sub(" ", raw)


def _plain(path):
    with open(path, "rb") as fh:
        raw = fh.read(MAX_TEXT_CHARS * 4)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "ignore")
    return raw.decode("utf-8", "ignore")


def _clean(text):
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


def extract_one(path):
    """Worker entry point. Returns (path, status, error, text)."""
    ext = os.path.splitext(path)[1].lower()
    p = lp(path)
    try:
        if ext == ".pdf":
            text = _pdf(p)
        elif ext in (".docx", ".docm"):
            text = _docx(p)
        elif ext in (".xlsx", ".xlsm"):
            text = _xlsx(p)
        elif ext in (".pptx", ".pptm"):
            text = _pptx(p)
        elif ext == ".rtf":
            text = _rtf(p)
        elif ext == ".epub":
            text = _epub(p)
        elif ext in (".odt", ".ods", ".odp"):
            text = _odf(p)
        elif ext in (".doc", ".xls", ".ppt"):
            text = _legacy_office(p)
        elif ext == ".eml":
            text = _eml(p)
        elif ext == ".msg":
            text = _msgfile(p)
        elif ext in (".zip", ".cbz"):
            text = _zipnames(p)
        elif ext in AUDIO_EXTS:
            text = _audio(p)
        else:
            text = _plain(p)
    except Exception as exc:
        return path, "error", (type(exc).__name__ + ": " + str(exc))[:200], ""
    text = _clean(text)[:MAX_TEXT_CHARS]
    return path, ("ok" if text else "empty"), "", text


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id      INTEGER PRIMARY KEY,
    path    TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL,
    ext     TEXT,
    size    INTEGER,
    mtime   REAL,
    indexed REAL,
    chars   INTEGER DEFAULT 0,
    status  TEXT,
    error   TEXT,
    is_dir  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_ext  ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_stat ON files(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    body,
    tokenize = "unicode61 remove_diacritics 2",
    prefix = '3'
);
"""

# Instant filename search: a trigram index over names (SQLite 3.34+), kept in
# sync by triggers so every write path - extraction, name-only batches,
# pruning, the GUI's deletes - maintains it automatically.
NAMES_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(
    name,
    content='files', content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS files_names_ai AFTER INSERT ON files BEGIN
    INSERT INTO names(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS files_names_ad AFTER DELETE ON files BEGIN
    INSERT INTO names(names, rowid, name) VALUES('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS files_names_au AFTER UPDATE OF name ON files
BEGIN
    INSERT INTO names(names, rowid, name) VALUES('delete', old.id, old.name);
    INSERT INTO names(rowid, name) VALUES (new.id, new.name);
END;
"""


def get_meta(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def open_db_ro(path):
    """Read-only connection for searching. Never creates or writes - and in
    WAL mode a pure reader is never made to wait behind an indexing run,
    which is what made searches stall in bursts while indexing."""
    import urllib.request
    uri = "file:{}?mode=ro".format(
        urllib.request.pathname2url(os.path.abspath(path)))
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.execute("PRAGMA query_only=1")
    return conn


def open_db(path):
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-262144")   # 256 MB page cache
    conn.executescript(SCHEMA)
    # An index built by an older findex predates folder indexing: add the
    # column in place, keeping every row. New databases have it from SCHEMA.
    have = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "is_dir" not in have:
        conn.execute("ALTER TABLE files ADD COLUMN "
                     "is_dir INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    try:
        conn.executescript(NAMES_SCHEMA)
    except sqlite3.OperationalError:
        pass    # SQLite too old for trigram - the plain scan still works
    return conn


# ----------------------------------------------------------------------------
# Filesystem walk
# ----------------------------------------------------------------------------

def walk(roots):
    """Yield (path, name, ext, size, mtime, is_cloud_placeholder, is_dir) for
    EVERY file AND folder under the roots. Folders are yielded too - ext ''
    and size 0 - so folder names are searchable, the way Everything mixes
    files and folders. What to do with each entry is the caller's decision."""
    stack = [os.path.abspath(r) for r in roots]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        low = entry.name.lower()
                        if low in SKIP_DIRS or low.startswith("$"):
                            continue
                        stack.append(entry.path)
                        try:
                            st = entry.stat()
                            yield (entry.path, entry.name, "", 0,
                                   st.st_mtime, False, True)
                        except OSError:
                            pass
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    st = entry.stat()
                    attrs = getattr(st, "st_file_attributes", 0)
                    yield (entry.path, entry.name, ext, st.st_size,
                           st.st_mtime, bool(attrs & CLOUD_MASK), False)
                except OSError:
                    continue


# ----------------------------------------------------------------------------
# Index command
# ----------------------------------------------------------------------------

# SQLite 3.35+ can hand back the row id from the upsert itself, halving the
# statements on the extraction write path.
USE_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)

UPSERT = """
INSERT INTO files (path, name, ext, size, mtime, indexed, chars, status, error,
                   is_dir)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    size=excluded.size, mtime=excluded.mtime, indexed=excluded.indexed,
    chars=excluded.chars, status=excluded.status, error=excluded.error,
    is_dir=excluded.is_dir
"""


def flush(conn, executor, batch, stats):
    """Extract a batch in parallel and write the results in one transaction."""
    meta = {}
    for rec in batch:
        meta[rec[0]] = rec
    paths = [rec[0] for rec in batch]
    now = time.time()
    cur = conn.cursor()
    cur.execute("BEGIN")
    for path, status, error, text in executor.map(extract_one, paths, chunksize=8):
        _, name, ext, size, mtime = meta[path]
        values = (path, name, ext, size, mtime, now,
                  len(text), status, error or None, 0)
        if USE_RETURNING:
            fid = cur.execute(UPSERT + " RETURNING id", values).fetchone()[0]
        else:
            cur.execute(UPSERT, values)
            fid = cur.execute("SELECT id FROM files WHERE path=?",
                              (path,)).fetchone()[0]
        cur.execute("DELETE FROM docs WHERE rowid=?", (fid,))
        if text:
            cur.execute("INSERT INTO docs(rowid, body) VALUES (?, ?)", (fid, text))
        stats[status] = stats.get(status, 0) + 1
        stats["done"] += 1
    conn.commit()


def flush_names(conn, batch, stats):
    """Record name/size/date for folders and for files whose contents are not
    extracted, so filename search covers everything on the drive - like
    Everything does."""
    now = time.time()
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.executemany(
        UPSERT,
        [(path, name, ext, size, mtime, now, 0,
          "folder" if is_dir else "listed", None, 1 if is_dir else 0)
         for path, name, ext, size, mtime, is_dir in batch])
    conn.commit()
    stats["listed"] += len(batch)


def emit_progress(stats, start):
    """Machine-readable progress line consumed by findex_gui. Harmless noise
    for a human reading the console. `done` folds in name-only records so the
    GUI's "updated" counter reflects all rows written this run."""
    print("@P seen={} done={} unchanged={} ok={} empty={} error={} "
          "skipped={} elapsed={:.1f}"
          .format(stats["seen"], stats["done"] + stats["listed"],
                  stats["unchanged"], stats.get("ok", 0),
                  stats.get("empty", 0), stats.get("error", 0),
                  stats["skipped"], time.time() - start), flush=True)


def cmd_index(args):
    if not HAVE_FITZ:
        sys.stderr.write("WARNING: PyMuPDF is not installed - PDFs will error.\n"
                         "         pip install pymupdf\n\n")

    global OCR_ENABLED
    if getattr(args, "ocr", False):
        if have_ocr_backend():
            OCR_ENABLED = True
            os.environ["FINDEX_OCR"] = "1"   # inherited by worker processes
        else:
            sys.stderr.write(
                "WARNING: --ocr requested but no OCR engine is available - "
                "OCR skipped this run.\n"
                "         The desktop app installs one automatically; or "
                "install tesseract yourself.\n\n")

    roots = args.roots or [os.path.expanduser("~")]
    conn = open_db(args.db)

    if get_meta(conn, "names_ready") != "1":
        try:
            print("Building the instant filename index (one-off)...",
                  flush=True)
            conn.execute("INSERT INTO names(names) VALUES('rebuild')")
            conn.commit()
            set_meta(conn, "names_ready", "1")
        except sqlite3.OperationalError:
            pass    # no trigram support - plain filename search continues

    print("Loading existing index...", flush=True)
    known = {}
    for fid, path, size, mtime, status in conn.execute(
            "SELECT id, path, size, mtime, status FROM files"):
        known[path] = (fid, size, mtime, status)
    print("  {:,} files already indexed".format(len(known)))

    workers = args.workers or max(1, (os.cpu_count() or 4))
    print("Scanning {} with {} workers...\n".format(", ".join(roots), workers),
          flush=True)

    stats = {"done": 0, "seen": 0, "skipped": 0, "unchanged": 0, "listed": 0}
    start = time.time()
    batch = []
    name_batch = []
    progress = getattr(args, "progress", False)
    last_emit = 0.0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        for path, name, ext, size, mtime, cloud, is_dir in walk(roots):
            stats["seen"] += 1
            if progress and time.time() - last_emit > 0.4:
                last_emit = time.time()
                emit_progress(stats, start)

            wants_text = (not is_dir and can_extract(ext)
                          and (size <= MAX_FILE_BYTES or ext in AUDIO_EXTS)
                          and (args.include_cloud or not cloud))

            prev = known.pop(path, None)
            if (prev and not args.rebuild
                    and prev[1] == size and abs(prev[2] - mtime) < 1e-6
                    and not (wants_text and prev[3] == "listed")):
                # Unchanged - and not a name-only row that newly qualifies for
                # extraction (e.g. --include-cloud turned on).
                stats["unchanged"] += 1
                continue

            if not wants_text:
                if not is_dir and ext in INDEXABLE and size > MAX_FILE_BYTES:
                    stats["skipped"] += 1
                name_batch.append((path, name, ext, size, mtime, is_dir))
                if len(name_batch) >= NAME_CHUNK:
                    flush_names(conn, name_batch, stats)
                    name_batch = []
                continue

            batch.append((path, name, ext, size, mtime))
            if len(batch) >= CHUNK:
                flush(conn, executor, batch, stats)
                batch = []
                elapsed = time.time() - start
                rate = stats["done"] / elapsed if elapsed else 0
                print("  {:,} indexed | {:,} seen | {:,.0f} files/s".format(
                    stats["done"], stats["seen"], rate), flush=True)
                if progress:
                    last_emit = time.time()
                    emit_progress(stats, start)
        if batch:
            flush(conn, executor, batch, stats)
        if name_batch:
            flush_names(conn, name_batch, stats)
        if progress:
            emit_progress(stats, start)

    # Anything left in `known` was not seen during this run. Only prune rows
    # that live UNDER the roots actually walked - otherwise indexing one drive
    # would wipe the rows for every other drive, and a portable copy would lose
    # its index every time it ran with fewer drives attached.
    if known and not args.rebuild:
        prefixes = [os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep
                    for r in roots]
        stale = [meta for path, meta in known.items()
                 if any(os.path.normcase(path).startswith(p) for p in prefixes)]
        if stale:
            cur = conn.cursor()
            cur.execute("BEGIN")
            for meta in stale:
                cur.execute("DELETE FROM docs WHERE rowid=?", (meta[0],))
                cur.execute("DELETE FROM files WHERE id=?", (meta[0],))
            conn.commit()
        print("\nPruned {:,} files that no longer exist".format(len(stale)))
        kept = len(known) - len(stale)
        if kept:
            print("Kept   {:,} indexed files outside the folders scanned"
                  .format(kept))

    elapsed = time.time() - start
    set_meta(conn, "last_index", int(time.time()))
    set_meta(conn, "last_roots", "\n".join(os.path.abspath(r) for r in roots))
    set_meta(conn, "last_summary",
             "{:,} seen, {:,} updated, {:,} unchanged".format(
                 stats["seen"], stats["done"] + stats["listed"],
                 stats["unchanged"]))
    print("\nDone in {:.1f} min".format(elapsed / 60))
    print("  seen       {:,}".format(stats["seen"]))
    print("  unchanged  {:,}  (already indexed, not re-read)"
          .format(stats["unchanged"]))
    print("  text read  {:,}".format(stats["done"]))
    print("  names only {:,}  (type not extracted - still findable by name)"
          .format(stats["listed"]))
    print("  with text  {:,}".format(stats.get("ok", 0)))
    print("  no text    {:,}".format(stats.get("empty", 0)))
    print("  errors     {:,}".format(stats.get("error", 0)))
    print("  too big    {:,}".format(stats["skipped"]))
    conn.close()
    return 0


# ----------------------------------------------------------------------------
# Watch command - live index updates
# ----------------------------------------------------------------------------

def _watch_skip(path):
    """Is this path inside a folder that indexing skips (SKIP_DIRS)?"""
    parts = path.replace("\\", "/").lower().split("/")
    return any(p in SKIP_DIRS or p.startswith("$") for p in parts[:-1])


def cmd_watch(args):
    """Live updates: watch the roots and fold filesystem changes into the
    index within seconds - new and modified files are (re)extracted,
    deletions pruned, renames and new folders handled. Runs until stopped
    (Ctrl+C, or the app's Live updates tick box)."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver
    except ImportError:
        sys.stderr.write(
            "Live updates need the 'watchdog' package:\n"
            "    pip install watchdog\n"
            "(the desktop app installs it automatically on launch)\n")
        return 1

    global OCR_ENABLED
    if getattr(args, "ocr", False) and have_ocr_backend():
        OCR_ENABLED = True
        os.environ["FINDEX_OCR"] = "1"
    include_cloud = getattr(args, "include_cloud", False)

    roots = [os.path.abspath(r) for r in (args.roots or [])
             if os.path.isdir(r)]
    if not roots:
        sys.stderr.write("watch: none of the given folders exist here\n")
        return 1

    import stat as statmod
    import threading
    lock = threading.Lock()
    pending = {}          # path -> arrived-as-directory (walk it if so)
    gone = set()

    class Handler(FileSystemEventHandler):
        def on_created(self, e):
            with lock:
                pending[e.src_path] = e.is_directory

        def on_modified(self, e):
            if not e.is_directory:      # folders "modify" constantly - noise
                with lock:
                    pending[e.src_path] = False

        def on_moved(self, e):
            with lock:
                gone.add(e.src_path)
                pending[e.dest_path] = e.is_directory

        def on_deleted(self, e):
            with lock:
                gone.add(e.src_path)

    handler = Handler()
    observer = None
    for maker in (Observer, lambda: PollingObserver(timeout=30)):
        candidate = maker()
        ok = 0
        for r in roots:
            try:
                candidate.schedule(handler, r, recursive=True)
                ok += 1
            except OSError as exc:
                sys.stderr.write("watch: cannot watch {}: {}\n".format(r, exc))
        if ok:
            observer = candidate
            break
    if observer is None:
        return 1
    observer.daemon = True
    observer.start()

    conn = open_db(args.db)
    print("Watching {} - changes land in the index within seconds."
          .format(", ".join(roots)), flush=True)

    def upsert_one(cur, path):
        """Stat + record one path; extract text when the type qualifies.
        Returns 1 when a row was written."""
        try:
            st = os.stat(path)
        except OSError:
            return 0
        name = os.path.basename(path)
        now = time.time()
        if statmod.S_ISDIR(st.st_mode):
            cur.execute(UPSERT, (path, name, "", 0, st.st_mtime, now, 0,
                                 "folder", None, 1))
            return 1
        if not statmod.S_ISREG(st.st_mode):
            return 0
        ext = os.path.splitext(name)[1].lower()
        attrs = getattr(st, "st_file_attributes", 0)
        cloud = bool(attrs & CLOUD_MASK)
        wants = (can_extract(ext)
                 and (st.st_size <= MAX_FILE_BYTES or ext in AUDIO_EXTS)
                 and (include_cloud or not cloud))
        if not wants:
            cur.execute(UPSERT, (path, name, ext, st.st_size, st.st_mtime,
                                 now, 0, "listed", None, 0))
            return 1
        _, status, error, text = extract_one(path)
        values = (path, name, ext, st.st_size, st.st_mtime, now,
                  len(text), status, error or None, 0)
        if USE_RETURNING:
            fid = cur.execute(UPSERT + " RETURNING id", values).fetchone()[0]
        else:
            cur.execute(UPSERT, values)
            fid = cur.execute("SELECT id FROM files WHERE path=?",
                              (path,)).fetchone()[0]
        cur.execute("DELETE FROM docs WHERE rowid=?", (fid,))
        if text:
            cur.execute("INSERT INTO docs(rowid, body) VALUES (?, ?)",
                        (fid, text))
        return 1

    def drop_gone(cur, path):
        """Remove a vanished path - and, if it was a folder, everything the
        index holds underneath it. Returns rows removed."""
        sep = "\\" if "\\" in path or (len(path) > 1 and path[1] == ":") \
            else "/"
        rows = cur.execute(
            "SELECT id FROM files WHERE path = ? OR path LIKE ?",
            (path, path.rstrip("\\/") + sep + "%")).fetchall()
        for (fid,) in rows:
            cur.execute("DELETE FROM docs WHERE rowid=?", (fid,))
            cur.execute("DELETE FROM files WHERE id=?", (fid,))
        return len(rows)

    try:
        while True:
            time.sleep(2)
            with lock:
                todo = dict(pending)
                pending.clear()
                dead = set(gone)
                gone.clear()
            if not todo and not dead:
                continue
            updated = removed = 0
            try:
                cur = conn.cursor()
                cur.execute("BEGIN")
                for path in dead:
                    if not _watch_skip(path) and not os.path.exists(path):
                        removed += drop_gone(cur, path)
                for path, was_dir in todo.items():
                    if _watch_skip(path):
                        continue
                    if was_dir and os.path.isdir(path):
                        # a whole folder appeared (created or moved in): its
                        # contents may never get events of their own - walk it
                        updated += upsert_one(cur, path)
                        for tup in walk([path]):
                            updated += upsert_one(cur, tup[0])
                    else:
                        updated += upsert_one(cur, path)
                conn.commit()
            except sqlite3.OperationalError:
                # database busy (an index run is writing): put everything
                # back and try again on the next tick
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                with lock:
                    for k, v in todo.items():
                        pending.setdefault(k, v)
                    gone.update(dead)
                continue
            if updated or removed:
                try:
                    set_meta(conn, "last_index", int(time.time()))
                except sqlite3.OperationalError:
                    pass
                print("  live: {:,} updated, {:,} removed".format(
                    updated, removed), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            observer.stop()
        except Exception:
            pass
        conn.close()
    return 0


# ----------------------------------------------------------------------------
# Search commands
# ----------------------------------------------------------------------------

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "{:.0f}{}".format(n, unit)
        n /= 1024
    return "{:.1f}TB".format(n)


def search_rows(conn, query, limit=25, exts=None, snippet_len=14):
    """Full-text search. Returns [(path, size, mtime, snippet, rank)] rows."""
    sql = ("SELECT f.path, f.size, f.mtime, "
           "snippet(docs, 0, '>>', '<<', ' ... ', {}) AS snip, "
           "bm25(docs) AS r "
           "FROM docs JOIN files f ON f.id = docs.rowid "
           "WHERE docs MATCH ?".format(int(snippet_len)))
    params = [query]
    if exts:
        norm = ["." + e.lstrip(".").lower() for e in exts]
        sql += " AND f.ext IN (" + ",".join("?" * len(norm)) + ")"
        params += norm
    sql += " ORDER BY r"
    if int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def name_rows(conn, pattern, limit=50, exts=None):
    """Filename search over every recorded file: [(path, size, mtime)].
    Uses the trigram filename index when the database has one - instant even
    at hundreds of thousands of files - and falls back to a plain scan."""
    if "*" in pattern or "?" in pattern:
        like = pattern.replace("*", "%").replace("?", "_")
    else:
        like = "%" + pattern + "%"
    where_ext, ext_params = "", []
    if exts:
        norm = ["." + e.lstrip(".").lower() for e in exts]
        where_ext = " AND f.ext IN (" + ",".join("?" * len(norm)) + ")"
        ext_params = norm
    tail, tail_params = " ORDER BY f.mtime DESC", []
    if int(limit) > 0:
        tail += " LIMIT ?"
        tail_params = [int(limit)]
    # a match-everything pattern gains nothing from the index
    if like.strip("%_") and get_meta(conn, "names_ready") == "1":
        try:
            return conn.execute(
                "SELECT f.path, f.size, f.mtime FROM names "
                "JOIN files f ON f.id = names.rowid "
                "WHERE names.name LIKE ?" + where_ext + tail,
                [like] + ext_params + tail_params).fetchall()
        except sqlite3.OperationalError:
            pass
    return conn.execute(
        "SELECT f.path, f.size, f.mtime FROM files f "
        "WHERE f.name LIKE ?" + where_ext + tail,
        [like] + ext_params + tail_params).fetchall()


# ----------------------------------------------------------------------------
# Everything-style unified search
# ----------------------------------------------------------------------------

# A token is either plain, or carries one double-quoted section (so
# content:"exact phrase" and "two words" survive as single tokens).
_TOKEN = re.compile(r'[^\s"]*"[^"]*"|[^\s"]+')


def _is_pathish(tok):
    """Does this token look like a path scope? C:  C:\\Users  /home  \\\\nas"""
    if len(tok) >= 2 and tok[1] == ":" and tok[0].isalpha():
        return len(tok) == 2 or tok[2] in "\\/"
    return tok.startswith(("/", "\\\\", "~"))


def parse_query(text):
    """Everything-style query -> filter dict. One box does it all:

        bare word         must appear in the file/folder NAME (* ? wildcards)
        "two words"       one name term with the space kept
        content:word      must appear in the file's extracted text
        content:"a b"     exact phrase in the text
        C:   C:\\Users     only results under that drive/folder
        ext:pdf;docx      only those types
        file:  folder:    only files / only folders
        !anything         the same, negated: !draft  !ext:tmp  !C:\\Windows
    """
    q = {"name": [], "name_not": [], "content": [], "content_not": [],
         "paths": [], "paths_not": [], "exts": [], "exts_not": [],
         "kind": None}
    for tok in _TOKEN.findall(text or ""):
        neg = tok.startswith("!")
        if neg:
            tok = tok[1:]
        if not tok:
            continue
        low = tok.lower()
        if low.startswith("content:"):
            term = tok[8:].strip()
            if term:
                q["content_not" if neg else "content"].append(term)
            continue
        if low.startswith("ext:"):
            for e in re.split(r"[;,]", tok[4:]):
                e = e.strip().lstrip(".").lower()
                if e:
                    q["exts_not" if neg else "exts"].append("." + e)
            continue
        if low.startswith("path:"):
            rest = tok[5:].strip('"')
            if rest:
                q["paths_not" if neg else "paths"].append(rest)
            continue
        if low.startswith(("file:", "folder:", "folders:", "dir:")):
            q["kind"] = "file" if low.startswith("file:") else "folder"
            tok = tok.split(":", 1)[1]
            if not tok:
                continue
        if _is_pathish(tok):
            q["paths_not" if neg else "paths"].append(tok.strip('"'))
            continue
        q["name_not" if neg else "name"].append(tok.strip('"'))
    return q


def _name_like(term):
    if "*" in term or "?" in term:
        return term.replace("*", "%").replace("?", "_")
    return "%" + term + "%"


def _path_prefix(p):
    """'C:' -> 'C:\\%', '/Users/x/' -> '/Users/x/%' - everything under it."""
    if p.startswith("~"):
        p = os.path.expanduser(p)
    sep = "\\" if (len(p) >= 2 and p[1] == ":") or p.startswith("\\\\") \
        else "/"
    return p.rstrip("\\/") + sep + "%"


def _safe_content(terms):
    """Mid-typing fallback for broken FTS syntax: the real words, each as a
    quoted prefix, operators dropped."""
    words = []
    for t in terms:
        words += [w for w in re.findall(r"\w+", t)
                  if w.upper() not in ("AND", "OR", "NOT", "NEAR")]
    return " ".join('"{}"*'.format(w) for w in words)


def query_rows(conn, text, limit=0, exts=None, kind=None, live=False,
               snippet_len=18):
    """One Everything-style query over the whole index.

    Returns [(path, size, mtime, snippet, is_dir)]. snippet is '' unless the
    query has content: terms. Results are weighted: content matches come back
    best-first (bm25), name matches exact-name first, then names starting
    with the term, then newest; a browse (no terms) is newest-first.
    """
    q = parse_query(text)
    if exts:
        q["exts"] += ["." + e.lstrip(".").lower() for e in exts]
    if kind and not q["kind"]:
        q["kind"] = kind
    if live and q["content"]:
        last = q["content"][-1]
        if last and (last[-1].isalnum() or last[-1] == "_"):
            q["content"][-1] = last + "*"   # the word being typed matches
                                            # as a prefix while you type

    conds, params = [], []
    for t in q["name"]:
        conds.append("f.name LIKE ?")
        params.append(_name_like(t))
    for t in q["name_not"]:
        conds.append("f.name NOT LIKE ?")
        params.append(_name_like(t))
    if q["paths"]:
        conds.append("(" + " OR ".join(["f.path LIKE ?"] * len(q["paths"]))
                     + ")")
        params += [_path_prefix(p) for p in q["paths"]]
    for p in q["paths_not"]:
        conds.append("f.path NOT LIKE ?")
        params.append(_path_prefix(p))
    if q["exts"]:
        conds.append("f.ext IN (" + ",".join("?" * len(q["exts"])) + ")")
        params += q["exts"]
    if q["exts_not"]:
        conds.append("f.ext NOT IN (" + ",".join("?" * len(q["exts_not"]))
                     + ")")
        params += q["exts_not"]
    if q["kind"] == "file":
        conds.append("f.is_dir=0")
    elif q["kind"] == "folder":
        conds.append("f.is_dir=1")

    not_params = []
    if q["content_not"]:
        conds.append("f.id NOT IN (SELECT rowid FROM docs WHERE docs MATCH ?)")
        not_params = [" ".join(q["content_not"])]

    lim, lim_params = "", []
    if int(limit) > 0:
        lim = " LIMIT ?"
        lim_params = [int(limit)]

    if q["content"]:
        # content search: join through the FTS table for snippets and rank
        sql = ("SELECT f.path, f.size, f.mtime, "
               "snippet(docs, 0, '>>', '<<', ' ... ', {}), f.is_dir "
               "FROM docs JOIN files f ON f.id = docs.rowid "
               "WHERE ".format(int(snippet_len))
               + " AND ".join(["docs MATCH ?"] + conds)
               + " ORDER BY bm25(docs)" + lim)
        try:
            return conn.execute(
                sql, [" ".join(q["content"])] + params + not_params
                + lim_params).fetchall()
        except sqlite3.OperationalError:
            safe = _safe_content(q["content"])
            if not safe:
                raise
            safe_not = ([_safe_content(q["content_not"])]
                        if q["content_not"] else [])
            if q["content_not"] and not safe_not[0]:
                safe_not = ['"findex0nomatch0"']   # valid, matches nothing
            return conn.execute(
                sql, [safe] + params + safe_not + lim_params).fetchall()

    # name / filter search - weighted: exact name, then starts-with, then
    # newest first. A browse with no terms at all is just newest first.
    order, order_params = " ORDER BY f.mtime DESC", []
    if q["name"]:
        first = q["name"][0].strip("*?").lower()
        if first:
            order = (" ORDER BY (lower(f.name) = ? OR lower(f.name) LIKE ?) "
                     "DESC, (f.name LIKE ?) DESC, f.mtime DESC")
            order_params = [first, first + ".%", first + "%"]

    tail_params = params + not_params + order_params + lim_params
    if q["name"] and get_meta(conn, "names_ready") == "1" \
            and _name_like(q["name"][0]).strip("%_"):
        tri = list(conds)
        tri[0] = tri[0].replace("f.name", "names.name", 1)
        try:
            return conn.execute(
                "SELECT f.path, f.size, f.mtime, '', f.is_dir FROM names "
                "JOIN files f ON f.id = names.rowid WHERE "
                + " AND ".join(tri) + order + lim, tail_params).fetchall()
        except sqlite3.OperationalError:
            pass    # no trigram support - the plain scan below still works
    sql = "SELECT f.path, f.size, f.mtime, '', f.is_dir FROM files f"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return conn.execute(sql + order + lim, tail_params).fetchall()


def dupe_rows(conn, limit=0, exts=None):
    """Duplicate candidates: files sharing NAME and SIZE with at least one
    other file. Grouped in the output (biggest first) so the copies sit next
    to each other: [(path, size, mtime, copies_in_group)]."""
    inner_w, outer_w, params_in, params_out = "", "", [], []
    if exts:
        norm = ["." + e.lstrip(".").lower() for e in exts]
        marks = ",".join("?" * len(norm))
        inner_w = " AND ext IN ({})".format(marks)
        outer_w = " AND f.ext IN ({})".format(marks)
        params_in, params_out = norm, norm
    sql = ("SELECT f.path, f.size, f.mtime, d.n FROM files f JOIN "
           "(SELECT name, size, COUNT(*) AS n FROM files "
           "WHERE is_dir=0 AND size>0{} GROUP BY name, size "
           "HAVING COUNT(*) > 1) d "
           "ON f.name = d.name AND f.size = d.size "
           "WHERE f.is_dir=0{} "
           "ORDER BY f.size DESC, f.name, f.path".format(inner_w, outer_w))
    params = params_in + params_out
    if int(limit) > 0:
        sql += " LIMIT ?"
        params = params + [int(limit)]
    return conn.execute(sql, params).fetchall()


def dupe_summary(conn, exts=None):
    """(groups, files, wasted_bytes) for the same-name-same-size duplicates.
    wasted = what deleting all but one copy of each group would free."""
    where, params = "", []
    if exts:
        norm = ["." + e.lstrip(".").lower() for e in exts]
        where = " AND ext IN ({})".format(",".join("?" * len(norm)))
        params = norm
    return conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM((n-1)*size),0) "
        "FROM (SELECT size, COUNT(*) AS n FROM files "
        "WHERE is_dir=0 AND size>0{} GROUP BY name, size "
        "HAVING COUNT(*) > 1)".format(where), params).fetchone()


def cmd_search(args):
    conn = open_db(args.db)
    try:
        rows = search_rows(conn, args.query, args.limit, args.ext)
    except sqlite3.OperationalError as exc:
        sys.stderr.write("Query error: {}\n".format(exc))
        sys.stderr.write('FTS5 syntax: word, "exact phrase", a AND b, a OR b, '
                         "a NOT b, prefix*, NEAR(a b, 5)\n")
        return 1

    if not rows:
        print("No matches.")
        return 0

    for i, row in enumerate(rows, 1):
        path, size, mtime, snip = row[0], row[1], row[2], row[3]
        when = time.strftime("%Y-%m-%d", time.localtime(mtime))
        print("\n{:>3}. {}".format(i, path))
        print("     {}  {}".format(human(size), when))
        print("     {}".format(snip))
    print("\n{} result(s)".format(len(rows)))
    return 0


def cmd_name(args):
    conn = open_db(args.db)
    rows = name_rows(conn, args.pattern, args.limit, getattr(args, "ext", None))
    for i, (path, size, mtime) in enumerate(rows, 1):
        when = time.strftime("%Y-%m-%d", time.localtime(mtime))
        print("{:>4}. {:>7}  {}  {}".format(i, human(size), when, path))
    print("\n{} result(s)".format(len(rows)))
    return 0


def cmd_find(args):
    """Everything-style search - one query does names, content, paths,
    types and exclusions. See parse_query for the syntax."""
    conn = open_db(args.db)
    try:
        rows = query_rows(conn, args.query, args.limit)
    except sqlite3.OperationalError as exc:
        sys.stderr.write("Query error: {}\n".format(exc))
        return 1
    for i, (path, size, mtime, snip, is_dir) in enumerate(rows, 1):
        when = time.strftime("%Y-%m-%d", time.localtime(mtime))
        print("{:>4}. {:>7}  {}  {}".format(
            i, "folder" if is_dir else human(size), when, path))
        if snip:
            print("      {}".format(snip))
    print("\n{} result(s)".format(len(rows)))
    return 0


def cmd_dupes(args):
    conn = open_db(args.db)
    groups, files, wasted = dupe_summary(conn, getattr(args, "ext", None))
    rows = dupe_rows(conn, args.limit, getattr(args, "ext", None))
    last = None
    for path, size, mtime, n in rows:
        key = (os.path.basename(path), size)
        if key != last:
            last = key
            print("\n{} - {} - {:,} copies:".format(key[0], human(size), n))
        print("    {}".format(path))
    if groups:
        print("\n{:,} duplicate set(s), {:,} files - {} reclaimable if each "
              "set kept one copy".format(groups, files, human(wasted)))
    else:
        print("No duplicates found (matched by name + size).")
    return 0


def cmd_stats(args):
    conn = open_db(args.db)
    total, chars = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(chars),0) FROM files").fetchone()
    dirs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE is_dir=1").fetchone()[0]
    print("Files indexed : {:,}".format(total - dirs))
    print("Folders       : {:,}".format(dirs))
    print("Text captured : {}".format(human(chars)))
    db_size = os.path.getsize(args.db) if os.path.exists(args.db) else 0
    print("Database size : {}".format(human(db_size)))

    print("\nBy status:")
    for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status "
            "ORDER BY COUNT(*) DESC"):
        print("  {:<10} {:>10,}".format(status or "unknown", n))

    print("\nTop extensions:")
    for ext, n, c in conn.execute(
            "SELECT ext, COUNT(*), COALESCE(SUM(chars),0) FROM files "
            "GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 15"):
        print("  {:<8} {:>10,}  {:>9}".format(ext, n, human(c)))

    errs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
    if errs:
        print("\nSample errors ({:,} total):".format(errs))
        for path, err in conn.execute(
                "SELECT path, error FROM files WHERE status='error' LIMIT 10"):
            print("  {}: {}".format(os.path.basename(path), err))
    return 0


def cmd_gui(args):
    """Launch the Tkinter desktop app that lives next to this script."""
    gui_path = os.path.join(HERE, "findex_gui.py")
    if not os.path.exists(gui_path):
        sys.stderr.write("findex_gui.py was not found next to findex.py\n")
        return 1
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import findex_gui
    return findex_gui.main(["--db", args.db])


def clear_index(path):
    """Start fresh: delete the index - every recorded file, all extracted
    text, and the run history. The files on your disk are untouched."""
    try:
        if os.path.exists(path):
            os.remove(path)
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
        return "index deleted - the next run starts from scratch"
    except OSError:
        pass
    # something still has the file open: empty it in place instead
    conn = open_db(path)
    cur = conn.cursor()
    cur.execute("DELETE FROM docs")
    cur.execute("DELETE FROM files")
    cur.execute("DELETE FROM meta")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return "index emptied - the next run starts from scratch"


def cmd_clear(args):
    if not getattr(args, "yes", False):
        answer = input("Delete the ENTIRE index at {}?\nYour files on disk "
                       "are untouched. [y/N] ".format(args.db)).strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing done.")
            return 0
    print(clear_index(args.db))
    return 0


def cmd_vacuum(args):
    conn = open_db(args.db)
    print("Optimising FTS index...")
    conn.execute("INSERT INTO docs(docs) VALUES('optimize')")
    conn.commit()
    print("Vacuuming...")
    conn.execute("VACUUM")
    conn.close()
    print("Database size: {}".format(human(os.path.getsize(args.db))))
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="findex", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help="index database path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="build or update the index")
    p.add_argument("roots", nargs="*", help="folders or drives to index")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--rebuild", action="store_true",
                   help="re-extract everything, ignore mtime checks")
    p.add_argument("--include-cloud", action="store_true",
                   help="extract text from OneDrive online-only files "
                        "(forces downloads; their names are indexed either way)")
    p.add_argument("--ocr", action="store_true",
                   help="OCR scanned PDFs with tesseract (slow; first {} pages "
                        "of each)".format(OCR_MAX_PAGES))
    p.add_argument("--progress", action="store_true",
                   help="emit machine-readable @P progress lines (used by the GUI)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("watch", help="live updates: index changes as they "
                                     "happen (until stopped)")
    p.add_argument("roots", nargs="*", help="folders or drives to watch")
    p.add_argument("--include-cloud", action="store_true")
    p.add_argument("--ocr", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("find", help="Everything-style search: bare words = "
                       "names, content:word, C:\\ paths, ext:pdf, !not")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("dupes", help="duplicate files (same name and size)")
    p.add_argument("-n", "--limit", type=int, default=0)
    p.add_argument("-e", "--ext", nargs="+", help="restrict to extensions")
    p.set_defaults(func=cmd_dupes)

    p = sub.add_parser("search", help="full-text search of file contents")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("-e", "--ext", nargs="+", help="restrict to extensions")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("name", help="filename search")
    p.add_argument("pattern")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("-e", "--ext", nargs="+", help="restrict to extensions")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser("gui", help="open the desktop app")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("clear", help="delete the index and start fresh")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("stats", help="index statistics")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("vacuum", help="optimise and compact the database")
    p.set_defaults(func=cmd_vacuum)

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
