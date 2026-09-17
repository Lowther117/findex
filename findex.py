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
def writable(folder):
    """True when a file can actually be created in folder.

    An actual write, not os.access: on Windows os.access reports the ACL and
    cheerfully says yes for folders that virtualisation, Controlled Folder
    Access or an anti-virus policy will then refuse.
    """
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, ".findex-write-test")
        with open(probe, "w"):
            pass
    except OSError:
        return False
    try:
        os.remove(probe)     # tidiness, not the test: creating it is the test
    except OSError:
        pass
    return True


def _user_data_dir():
    """Per-user folder used when the app's own folder cannot be written to."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support")
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, "findex")


def _app_dir():
    """The folder findex treats as its own - where the index and settings live.

    Normally that is the folder holding the scripts. In a standalone build it
    is the folder holding the exe. On macOS the executable sits three levels
    down inside findex.app, and anything written inside a bundle invalidates
    its code signature (the next launch is then killed by Gatekeeper), so the
    folder *containing* findex.app is used instead.

    A standalone build can end up somewhere it may not write - Program Files,
    a network share, a folder an anti-virus policy protects. Rather than dying
    at the first write with a permission error, it falls back to the usual
    per-user data folder for the platform.
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.realpath(__file__))
    exe_dir = os.path.dirname(os.path.realpath(sys.executable))
    parts = exe_dir.split(os.sep)
    if (sys.platform == "darwin" and len(parts) >= 3
            and parts[-1] == "MacOS" and parts[-2] == "Contents"
            and parts[-3].endswith(".app")):
        exe_dir = os.path.dirname(os.path.dirname(os.path.dirname(exe_dir)))
    if writable(exe_dir):
        return exe_dir
    fallback = _user_data_dir()
    return fallback if writable(fallback) else exe_dir


HERE = _app_dir()
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
# In a frozen build these are compiled in by PyInstaller, and HERE is a
# user folder that may hold anything, so vendor/ is only consulted when
# running from source.
if not getattr(sys, "frozen", False):
    _VENDOR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "vendor")
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
    """Prefix long Windows paths so they survive the 260-character limit.

    Every filesystem call findex makes goes through this - the walk, every
    extractor, the watcher's stats - because the limit is enforced per
    call, and a single unprefixed one deep in a tree silently loses the file.
    Python itself declares long-path support, but a frozen exe's bootloader
    does not, so the prefix is the only thing that works everywhere.
    """
    if os.name != "nt" or len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def downloads_dir():
    """The user's Downloads folder - the place anything findex writes FOR
    the user goes unless told otherwise. On Windows the real one is asked
    of the shell (it can be moved); elsewhere ~/Downloads; the home folder
    if there is no such thing."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            FOLDERID_Downloads = ctypes.c_char_p(  # noqa: N806
                b"\x90\xe2\x4d\x37\x3f\x12\x65\x45"
                b"\x91\x64\x39\xc4\x92\x5e\x46\x7b")
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.c_char_p, wintypes.DWORD, wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(FOLDERID_Downloads, 0, None,
                                            ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path and os.path.isdir(path):
                    return path
        except Exception:
            pass
    home = os.path.expanduser("~")
    cand = os.path.join(home, "Downloads")
    return cand if os.path.isdir(cand) else home


def default_save_dir(save_dir=None):
    """Where copies, moves and any other user output default to: the folder
    chosen under File > Default save folder... (settings key "save_dir",
    read from findex_gui.json when not passed in) if it still exists,
    otherwise the Downloads folder."""
    if save_dir is None:
        try:
            import json
            with open(os.path.join(HERE, "findex_gui.json"),
                      encoding="utf-8") as fh:
                save_dir = json.load(fh).get("save_dir")
        except (OSError, ValueError, AttributeError):
            save_dir = None
    if save_dir:
        if not os.path.isabs(save_dir):
            save_dir = os.path.normpath(os.path.join(HERE, save_dir))
        if os.path.isdir(save_dir):
            return save_dir
    return downloads_dir()


def like_escape(text):
    """Make a literal string safe inside a LIKE pattern (pair it with
    ESCAPE '!'). '_' and '%' are wildcards to LIKE, so an unescaped scope or
    delete for  D:\\my_docs  would also hit  D:\\myXdocs."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


# ----------------------------------------------------------------------------
# Text extraction (runs inside worker processes)
# ----------------------------------------------------------------------------

_TAG = re.compile(rb"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v\u00a0]+")
_NL = re.compile(r"\n{3,}")


# Word, PowerPoint and Excel cut a word into several "runs" whenever the
# formatting, the spell-check state or an edit boundary changes part-way
# through it: "Hello" is stored as <w:t>Hel</w:t>...<w:t>lo</w:t>. Turning
# every tag into a space then indexed "Hel lo" and a search for the word
# missed the file. So two text runs with nothing but tags between them are
# stitched together first - unless one of those tags is a real break (end of
# paragraph or table cell, tab, line break, the next Excel string).
_RUN_GAP = re.compile(rb"</((?:w:|a:)?)t>((?:<[^>]+>)*?)<\1t(?:\s[^>]*)?>")
_RUN_SEP = re.compile(
    rb"</(?:w:|a:)?p>|<(?:w:|a:)p[ >]|</(?:w:|a:)tc>"
    rb"|<(?:w:|a:)(?:tab|br|cr|noBreakHyphen|ptab)\b"
    rb"|</si>|</is>|<rPh\b")
# OpenDocument keeps the text between the tags instead, so there it is the
# inline tags themselves that must vanish rather than become a space.
_ODF_INLINE = re.compile(rb"</?text:(?:span|a)(?:\s[^>]*)?>")
_HTML_INLINE = re.compile(
    rb"</?(?:span|b|i|em|strong|u|sub|sup|small|font|mark)(?:\s[^>]*)?>", re.I)
_ODF_SPACE = re.compile(rb"<text:s(?:\s[^>]*)?/>")


def _join_runs(data):
    if b"</w:t>" in data or b"</a:t>" in data or b"</t>" in data:
        data = _RUN_GAP.sub(
            lambda m: m.group(0) if _RUN_SEP.search(m.group(2)) else b"", data)
    if b"<span" in data or b"</em>" in data or b"</i>" in data or b"</b>" in data:
        data = _HTML_INLINE.sub(b"", data)       # EPUB: <span class="dropcap">T</span>he
    if b"<text:" in data:
        data = _ODF_SPACE.sub(b" ", data)
        data = _ODF_INLINE.sub(b"", data)
    return data


def _xml_text(data):
    """Strip XML tags, keeping paragraph breaks where the format marks them."""
    data = _join_runs(data)
    data = data.replace(b"</w:p>", b"\n</w:p>")
    data = data.replace(b"</a:p>", b"\n</a:p>")
    data = data.replace(b"</text:p>", b"\n</text:p>")
    data = data.replace(b"</p>", b"\n</p>")
    data = data.replace(b"<w:br/>", b"\n")
    txt = _TAG.sub(b" ", data).decode("utf-8", "ignore")
    return html.unescape(txt)


# Bump when an extractor gets better at the SAME bytes, and list the types it
# affects: the next full index run re-reads those once, unchanged or not.
#   2 - Office/OpenDocument/EPUB words split across formatting runs are joined
EXTRACT_VERSION = 2
REEXTRACT_EXTS = frozenset((".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
                            ".odt", ".ods", ".odp", ".epub"))


# Cap on the bytes inflated from any one member of a zip-based document. A
# few MB of zip can declare gigabytes of XML; reading it whole took a worker
# (and with a full pool, the machine) out of memory.
MAX_PART_BYTES = 32 * 1024 * 1024


def _zread(z, name):
    with z.open(name) as fh:
        return fh.read(MAX_PART_BYTES)


def _zip_parts(path, wanted):
    """Pull the named XML parts out of an OOXML package and flatten to text."""
    out = []
    total = 0
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if wanted(n)]
        names.sort(key=lambda n: (len(n), n))
        for n in names:
            chunk = _xml_text(_zread(z, n))
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
        return None                    # the engine failed, not the page
    out = []
    for obs in (request.results() or []):
        cands = obs.topCandidates_(1)
        if cands and len(cands):
            out.append(str(cands[0].string()))
    return "\n".join(out)             # "" = it looked and the page is blank


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
            return None                # no OCR language pack installed
        result = await engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    try:
        return asyncio.run(_run())     # "" = it looked and the page is blank
    except Exception:
        return None


_TESS_BROKEN = None     # once tesseract has failed to run, why - per worker


def _tess_give_up(reason):
    """Record that tesseract is unusable and say so - once per worker, on
    stderr, which the desktop app shows in its Output pane. Without this a
    broken install fails silently on every page, or surfaces as a bare
    Windows error code with the actual message thrown away."""
    global _TESS_BROKEN
    if _TESS_BROKEN is None:
        _TESS_BROKEN = reason
        try:
            sys.stderr.write("ocr: tesseract unusable, not retrying - {}\n"
                             .format(reason))
        except Exception:
            pass


def _ocr_tesseract(png):
    """Fallback: the tesseract program, when installed."""
    import subprocess
    if _TESS_BROKEN:
        return None
    tess = _tesseract_bin()
    if not tess:
        return None
    try:
        r = subprocess.run([tess, "stdin", "stdout", "--psm", "3"],
                           input=png, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None                    # one slow page; the engine is fine
    except OSError as exc:
        # A closed pipe (WinError 232, "the pipe is being closed") means
        # tesseract exited before it had read the image - it will do the
        # same on every page, so stop after the first.
        _tess_give_up("could not run it: {}".format(exc))
        return None
    except Exception as exc:                                   # noqa: BLE001
        _tess_give_up("{}: {}".format(type(exc).__name__, exc))
        return None
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "ignore").strip().splitlines()
        _tess_give_up("exit {} - {}".format(
            r.returncode, err[-1] if err else "no message"))
        return None
    return r.stdout.decode("utf-8", "ignore")


def _ocr_png(png):
    """Read the text in one page image, using whichever engine works here:
    the OS's own OCR first, tesseract as a fallback.

    Each engine answers one of three ways: None - it cannot run here (not
    this platform, not installed, broken), so try the next; "" - it ran and
    the page is blank, so stop; or the text. The distinction matters: with
    None and "" treated alike, every blank page in a scan was pushed
    through all three engines in turn, and tesseract - last in line, and
    often not even wanted - got to complain about each one."""
    for backend in (_ocr_vision, _ocr_windows, _ocr_tesseract):
        try:
            text = backend(png)
        except Exception:                                      # noqa: BLE001
            text = None
        if text is None:
            continue
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



def _fitz_open(path):
    """MuPDF opens files by name itself. Should it refuse a \\\\?\\-prefixed
    path, read the bytes here - Python has no trouble with the prefix - and
    hand them over instead."""
    try:
        return fitz.open(path)
    except Exception:                                          # noqa: BLE001
        if not path.startswith("\\\\?\\"):
            raise
    with open(path, "rb") as fh:
        data = fh.read()
    return fitz.open(stream=data, filetype="pdf")


def _pdf_ocr(path):
    """Render the first OCR_MAX_PAGES pages and read them with the best
    available OCR engine."""
    out = []
    with _fitz_open(path) as doc:
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
    with _fitz_open(path) as doc:
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


_XL_INLINE = re.compile(rb"<is>(.*?)</is>", re.S)


def _xlsx(path):
    # The shared string table, not the sheet XML: sheets are mostly numeric
    # cell values and shared-string indices, which would poison the index
    # with noise digits. Inline-string cells (t="inlineStr" - what openpyxl,
    # pandas and Google Sheets exports write) never reach sharedStrings.xml,
    # so those are picked out of the sheets by their <is> wrapper.
    def wanted(n):
        return n == "xl/sharedStrings.xml" or n.startswith("xl/comments")
    out = [_zip_parts(path, wanted)]
    total = len(out[0])
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        names.sort(key=lambda n: (len(n), n))
        for n in names:
            if total > MAX_TEXT_CHARS:
                break
            for m in _XL_INLINE.finditer(_zread(z, n)):
                chunk = _xml_text(m.group(1))
                out.append(chunk)
                total += len(chunk)
    return "\n".join(p for p in out if p)


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

CREATE TABLE IF NOT EXISTS roots (
    path       TEXT PRIMARY KEY,
    added      REAL,
    last_index REAL
);

CREATE TABLE IF NOT EXISTS journal (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    event    TEXT NOT NULL,
    path     TEXT NOT NULL,
    old_path TEXT,
    size     INTEGER,
    is_dir   INTEGER NOT NULL DEFAULT 0,
    source   TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts   ON journal(ts);
CREATE INDEX IF NOT EXISTS idx_journal_path ON journal(path);

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


# The folders, drives and other locations chosen for indexing live in the
# database itself, so they travel with the index: point findex at an existing
# findex.db and the list comes back with it, rather than living in a settings
# file beside whichever copy of the app happened to write it.

def saved_roots(conn):
    """The remembered roots, oldest first."""
    try:
        return [r[0] for r in conn.execute(
            "SELECT path FROM roots ORDER BY added, path")]
    except sqlite3.OperationalError:       # database predates the table
        return []


def remember_roots(conn, roots, indexed=False):
    """Add these roots to the remembered set (existing ones are kept).
    indexed=True also stamps them as just indexed."""
    now = time.time()
    for r in roots:
        r = os.path.abspath(r)
        conn.execute("INSERT INTO roots(path, added, last_index) VALUES (?, ?, ?) "
                     "ON CONFLICT(path) DO UPDATE SET last_index = "
                     "CASE WHEN ? THEN excluded.last_index ELSE last_index END",
                     (r, now, now if indexed else None, 1 if indexed else 0))
    conn.commit()


def set_roots(conn, roots):
    """Make the remembered set exactly these roots - what the desktop app
    calls after its list is edited. Existing entries keep their dates."""
    keep = {os.path.abspath(r) for r in roots}
    conn.execute("DELETE FROM roots WHERE path NOT IN ({})".format(
        ",".join("?" * len(keep)) or "''"), tuple(keep))
    remember_roots(conn, keep)


def forget_roots(conn, roots):
    for r in roots:
        conn.execute("DELETE FROM roots WHERE path=?", (os.path.abspath(r),))
    conn.commit()


# The journal: every change findex notices inside the indexed locations -
# a file or folder added, modified, renamed or deleted - with when it was
# seen and by what (an index run, or live updates). Renames are only known
# as renames when live updates saw them happen; an index run sees a rename
# as one path gone and another arrived, and records exactly that.
#
# The first time a location is indexed nothing is journaled: every file
# would be "added", which is true but useless. From the second run on, and
# whenever live updates are on, changes are recorded.

JOURNAL_EVENTS = ("added", "modified", "renamed", "deleted")
JOURNAL_DAYS = 90             # default retention; "journal_days" in meta


def journal_add(cur, rows):
    """rows: (ts, event, path, old_path, size, is_dir, source)."""
    cur.executemany(
        "INSERT INTO journal(ts, event, path, old_path, size, is_dir, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def journal_rows(conn, since=None, event=None, text=None, limit=500,
                 under=None):
    """Newest first. since = epoch seconds; text matches path or old path;
    under = only paths beneath this folder."""
    where, params = [], []
    if since:
        where.append("ts >= ?")
        params.append(float(since))
    if event and event != "all":
        where.append("event = ?")
        params.append(event)
    if text:
        like = "%" + like_escape(text) + "%"
        where.append("(path LIKE ? ESCAPE '!' OR old_path LIKE ? ESCAPE '!')")
        params += [like, like]
    if under:
        sep = "\\" if "\\" in under or (len(under) > 1 and under[1] == ":") \
            else "/"
        where.append("path LIKE ? ESCAPE '!'")
        params.append(like_escape(under.rstrip("\\/")) + sep + "%")
    sql = ("SELECT id, ts, event, path, old_path, size, is_dir, source "
           "FROM journal")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC"
    if limit:
        sql += " LIMIT {:d}".format(int(limit))
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:      # database predates the journal
        return []


def journal_prune(conn, days=None):
    """Drop entries older than the retention period. Returns rows removed."""
    if days is None:
        try:
            days = float(get_meta(conn, "journal_days", JOURNAL_DAYS))
        except (TypeError, ValueError):
            days = JOURNAL_DAYS
    if days <= 0:                          # 0 = keep forever
        return 0
    cur = conn.execute("DELETE FROM journal WHERE ts < ?",
                       (time.time() - days * 86400,))
    conn.commit()
    return cur.rowcount


def journal_clear(conn):
    conn.execute("DELETE FROM journal")
    conn.commit()


def parse_since(text):
    """'30m', '6h', '7d', '2w', or a date 'YYYY-MM-DD' -> epoch seconds."""
    if not text:
        return None
    text = str(text).strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return time.time() - float(text[:-1]) * units[text[-1]]
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError("cannot read a time from {!r} - try 7d, 12h, 30m, "
                     "or 2026-09-01".format(text))


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


def open_db(path, timeout=60):
    conn = sqlite3.connect(path, timeout=timeout)
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
            # Scan through the long-path prefix, but record paths WITHOUT
            # it: entry.path would carry \\?\ into the index and the GUI.
            it = os.scandir(lp(d))
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    path = os.path.join(d, entry.name)
                    try:    # a name that is not valid Unicode cannot be
                            # stored in SQLite - it used to end the whole run
                        entry.name.encode("utf-8")
                    except UnicodeEncodeError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        low = entry.name.lower()
                        if low in SKIP_DIRS or low.startswith("$"):
                            continue
                        stack.append(path)
                        try:
                            st = entry.stat()
                            yield (path, entry.name, "", 0,
                                   st.st_mtime, False, True)
                        except OSError:
                            pass
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    st = entry.stat()
                    attrs = getattr(st, "st_file_attributes", 0)
                    yield (path, entry.name, ext, st.st_size,
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


DROP_TEXT = ("DELETE FROM docs WHERE rowid IN "
             "(SELECT id FROM files WHERE path=? AND chars>0)")


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
        if stats["done"] % 250 == 0:
            # Commit as results arrive. The write lock is otherwise held for
            # the whole batch's extraction time (minutes on big PDFs), and
            # everything else that writes - live updates, the app's deletes -
            # waits behind it or gives up.
            conn.commit()
            cur.execute("BEGIN")
    conn.commit()


def flush_names(conn, batch, stats):
    """Record name/size/date for folders and for files whose contents are not
    extracted, so filename search covers everything on the drive - like
    Everything does."""
    now = time.time()
    cur = conn.cursor()
    cur.execute("BEGIN")
    # a file that HAD text and is now name-only (grew past the size cap,
    # became a cloud placeholder): its old text must not stay searchable
    cur.executemany(DROP_TEXT, [(rec[0],) for rec in batch])
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

    conn = open_db(args.db)
    roots = args.roots or saved_roots(conn)
    # Files read by an older, worse extractor are read once more even though
    # they have not changed - only the cheap zip-based formats, never PDFs/OCR.
    try:
        stale_exts = (REEXTRACT_EXTS if int(get_meta(conn, "extract_version") or 1)
                      < EXTRACT_VERSION else frozenset())
    except (TypeError, ValueError):
        stale_exts = REEXTRACT_EXTS
    roots_before = [os.path.normcase(os.path.abspath(r)) for r in saved_roots(conn)]
    if not roots:
        sys.stderr.write("No folders given and none remembered - indexing "
                         "your home folder.\n")
        roots = [os.path.expanduser("~")]

    # A location that is not there right now - an unplugged drive, a share
    # that is offline - must be left out, not walked: the walk would find
    # nothing, and every row under it would then be pruned as "deleted".
    absent = [r for r in roots if not os.path.isdir(lp(os.path.abspath(r)))]
    if absent:
        sys.stderr.write("Not found, left alone: {}\n".format(
            ", ".join(absent)))
        roots = [r for r in roots if r not in absent]
        if not roots:
            conn.close()
            return 1

    # A root inside another root would be walked twice: the second pass saw
    # every file as new, so each run re-read them and journaled them "added".
    all_roots, roots, seen_pfx = roots, [], []
    for r in sorted(all_roots, key=lambda r: len(os.path.abspath(r))):
        pfx = os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep
        if not any(pfx.startswith(k) for k in seen_pfx):
            roots.append(r)
            seen_pfx.append(pfx)

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

    # Journal only for locations the index already knew about: the first
    # run over a location would log every file as "added", which is true
    # but tells nobody anything.
    prefixes = [os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep
                for r in roots]
    seeded = set()
    for kpath in known:
        nk = os.path.normcase(kpath)
        for pfx in prefixes:
            if nk.startswith(pfx):
                seeded.add(pfx)
                break
        if len(seeded) == len(prefixes):
            break

    def root_of(path):
        n = os.path.normcase(path)
        for pfx in prefixes:
            if n.startswith(pfx):
                return pfx
        return None

    journal = []          # (ts, event, path, old_path, size, is_dir, source)

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
            if prev is None:
                if root_of(path) in seeded:
                    journal.append((time.time(), "added", path, None,
                                    size, int(is_dir), "index"))
            elif prev[1] != size or abs(prev[2] - mtime) >= 1e-6:
                journal.append((time.time(), "modified", path, None,
                                size, int(is_dir), "index"))
            if (prev and not args.rebuild
                    and prev[1] == size and abs(prev[2] - mtime) < 1e-6
                    and not (wants_text and prev[3] == "listed")
                    and not (wants_text and ext in stale_exts)):
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
    if known:
        stale = [(path, meta) for path, meta in known.items()
                 if root_of(path) is not None]
        if stale:
            cur = conn.cursor()
            cur.execute("BEGIN")
            now = time.time()
            for path, meta in stale:
                cur.execute("DELETE FROM docs WHERE rowid=?", (meta[0],))
                cur.execute("DELETE FROM files WHERE id=?", (meta[0],))
                journal.append((now, "deleted", path, None, meta[1],
                                int(meta[3] == "folder"), "index"))
            conn.commit()
        print("\nPruned {:,} files that no longer exist".format(len(stale)))
        kept = len(known) - len(stale)
        if kept:
            print("Kept   {:,} indexed files outside the folders scanned"
                  .format(kept))

    if journal:
        cur = conn.cursor()
        cur.execute("BEGIN")
        journal_add(cur, journal)
        conn.commit()
    pruned = journal_prune(conn)

    elapsed = time.time() - start
    set_meta(conn, "last_index", int(time.time()))
    set_meta(conn, "last_roots", "\n".join(os.path.abspath(r) for r in roots))
    walked = [os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep for r in roots]
    if all(any((r.rstrip(os.sep) + os.sep).startswith(w) for w in walked)
           for r in roots_before):
        # every remembered location was covered, so nothing old is left behind
        set_meta(conn, "extract_version", EXTRACT_VERSION)
    remember_roots(conn, all_roots, indexed=True)
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
    if journal:
        counts = {}
        for row in journal:
            counts[row[1]] = counts.get(row[1], 0) + 1
        print("  journal    " + ", ".join(
            "{:,} {}".format(counts[k], k) for k in JOURNAL_EVENTS
            if k in counts))
    if pruned:
        print("  journal    {:,} old entries pruned".format(pruned))
    conn.close()
    return 0


# ----------------------------------------------------------------------------
# Watch command - live index updates
# ----------------------------------------------------------------------------

def journal_from_events(raw):
    """Collapse one tick's raw events into journal rows.

    Editors save through a burst of modified events, and many write a
    temp file then rename it over the original; Office drops lock files
    that live for seconds. So per path: one row for the tick, the LAST
    thing that happened to it wins, an added-then-modified is just
    added, and something created and deleted inside the same tick is
    noise and dropped altogether.
    """
    last = {}                      # path -> (ts, kind, src, dest, is_dir)
    born = set()
    for ts, kind, src, dest, is_dir in raw:
        if _watch_skip(src) or (dest and _watch_skip(dest)):
            continue
        if kind == "added":
            born.add(src)
        if kind == "modified" and src in last and last[src][1] == "added":
            continue               # still just "added"
        if kind == "renamed":
            last.pop(dest, None)
        last[src] = (ts, kind, src, dest, is_dir)
    rows = []
    for ts, kind, src, dest, is_dir in last.values():
        if kind == "deleted" and src in born:
            continue               # came and went inside one tick
        size = None
        if kind != "deleted":
            try:
                size = 0 if is_dir else os.path.getsize(lp(src))
            except OSError:
                pass
        rows.append((ts, kind, src, dest, size, int(bool(is_dir)),
                     "watch"))
    rows.sort()
    return rows


def _watch_skip(path):
    """Is this path inside a folder that indexing skips (SKIP_DIRS)?"""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return True                # walk() leaves these out too
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

    wanted = list(args.roots or [])
    if not wanted:                        # none given: watch the remembered ones
        try:
            ro = open_db_ro(args.db)
            wanted = saved_roots(ro)
            ro.close()
        except Exception:                                      # noqa: BLE001
            wanted = []
    roots = [os.path.abspath(r) for r in wanted if os.path.isdir(r)]
    if not roots:
        sys.stderr.write("watch: no folders given or remembered that exist "
                         "here\n")
        return 1

    import stat as statmod
    import threading
    lock = threading.Lock()
    pending = {}          # path -> arrived-as-directory (walk it if so)
    gone = set()
    events = []           # (ts, kind, src, dest, is_dir) for the journal

    class Handler(FileSystemEventHandler):
        def on_created(self, e):
            with lock:
                pending[e.src_path] = e.is_directory
                events.append((time.time(), "added", e.src_path, None,
                               e.is_directory))

        def on_modified(self, e):
            if not e.is_directory:      # folders "modify" constantly - noise
                with lock:
                    pending[e.src_path] = False
                    events.append((time.time(), "modified", e.src_path,
                                   None, False))

        def on_moved(self, e):
            with lock:
                gone.add(e.src_path)
                pending[e.dest_path] = e.is_directory
                events.append((time.time(), "renamed", e.dest_path,
                               e.src_path, e.is_directory))

        def on_deleted(self, e):
            with lock:
                gone.add(e.src_path)
                events.append((time.time(), "deleted", e.src_path, None,
                               e.is_directory))


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
            st = os.lstat(lp(path))     # like walk(): links are not followed
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
            cur.execute(DROP_TEXT, (path,))
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
            "SELECT id FROM files WHERE path = ? OR path LIKE ? ESCAPE '!'",
            (path, like_escape(path.rstrip("\\/")) + sep + "%")).fetchall()
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
                raw = list(events)
                events.clear()
            if not todo and not dead:
                continue
            rows = journal_from_events(raw)
            updated = removed = 0
            try:
                cur = conn.cursor()
                cur.execute("BEGIN")
                for path in dead:
                    if not _watch_skip(path) and not os.path.exists(lp(path)):
                        removed += drop_gone(cur, path)
                for path, was_dir in todo.items():
                    if _watch_skip(path):
                        continue
                    if (was_dir and os.path.isdir(lp(path))
                            and not os.path.islink(lp(path))):
                        # a whole folder appeared (created or moved in): its
                        # contents may never get events of their own - walk it
                        updated += upsert_one(cur, path)
                        for tup in walk([path]):
                            updated += upsert_one(cur, tup[0])
                    else:
                        updated += upsert_one(cur, path)
                if rows:
                    journal_add(cur, rows)
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
                    events[:0] = raw
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
    return like_escape(p.rstrip("\\/")) + sep + "%"


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
        conds.append("(" + " OR ".join(["f.path LIKE ? ESCAPE '!'"]
                                        * len(q["paths"])) + ")")
        params += [_path_prefix(p) for p in q["paths"]]
    for p in q["paths_not"]:
        conds.append("f.path NOT LIKE ? ESCAPE '!'")
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
        try:    # !content:don't - broken FTS syntax gets the same fallback
                # as content: does, instead of a bare "Query error"
            conn.execute("SELECT rowid FROM docs WHERE docs MATCH ? LIMIT 1",
                         not_params).fetchone()
        except sqlite3.OperationalError:
            not_params = [_safe_content(q["content_not"])
                          or '"findex0nomatch0"']

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
    if not getattr(sys, "frozen", False):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        gui_path = os.path.join(script_dir, "findex_gui.py")
        if not os.path.exists(gui_path):
            sys.stderr.write("findex_gui.py was not found next to findex.py\n")
            return 1
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
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


def cmd_roots(args):
    """Show, add to, or trim the remembered set of locations to index."""
    conn = open_db(args.db)
    if args.add:
        remember_roots(conn, args.add)
    if args.forget:
        forget_roots(conn, args.forget)
    rows = conn.execute(
        "SELECT path, last_index FROM roots ORDER BY added, path").fetchall()
    conn.close()
    if not rows:
        print("No locations remembered yet. Add some with\n"
              "    findex roots --add <folder or drive> ...\n"
              "or just run  findex index <folder> ...  once.")
        return 0
    print("Remembered for indexing ({}):".format(len(rows)))
    for path, last in rows:
        when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
                if last else "not indexed yet")
        print("  {}   [{}]".format(path, when))
    return 0


# ----------------------------------------------------------------------------
# File tree export - the whole index as a tree, straight from the database
# ----------------------------------------------------------------------------

_PATH_ROOT = re.compile(r"^([A-Za-z]:[\\/]?|[\\/]{1,2})")
_PATH_SPLIT = re.compile(r"[\\/]+")


def _split_path(path):
    """'D:\\a\\b' -> ('D:\\', ['a', 'b']); '/a/b' -> ('/', ['a', 'b'])."""
    m = _PATH_ROOT.match(path)
    root = m.group(1) if m else ""
    parts = [x for x in _PATH_SPLIT.split(path[len(root):]) if x]
    if root in ("\\\\", "//"):             # UNC: \\\\server\\share
        root = "\\\\"
    elif root:
        root = root.rstrip("\\/") + ("\\" if ":" in root or root.startswith("\\")
                                       else "/")
    return root, parts


def _node():
    return {"dirs": {}, "files": [], "size": 0, "count": 0}


def build_tree(rows):
    """rows of (path, is_dir, size, mtime) -> {root: node}. Every folder
    carries the total size and file count of everything beneath it."""
    roots = {}
    for path, is_dir, size, mtime in rows:
        root, parts = _split_path(path)
        node = roots.setdefault(root, _node())
        if is_dir:
            for part in parts:
                node = node["dirs"].setdefault(part, _node())
            continue
        chain = [node]
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, _node())
            chain.append(node)
        node["files"].append((parts[-1] if parts else path, size or 0, mtime))
        for n in chain:
            n["size"] += size or 0
            n["count"] += 1
    return roots


def _sorted_dirs(node):
    return sorted(node["dirs"].items(), key=lambda kv: kv[0].lower())


def _sorted_files(node):
    return sorted(node["files"], key=lambda f: f[0].lower())


def write_tree_text(roots, out):
    """The classic `tree` drawing, with folder totals."""
    def label(name, node):
        return "{}  [{}, {:,} file{}]".format(
            name, human(node["size"]), node["count"],
            "" if node["count"] == 1 else "s")

    def walk(node, prefix):
        dirs, files = _sorted_dirs(node), _sorted_files(node)
        entries = [(True, n, d) for n, d in dirs] + [(False, f[0], f) for f in files]
        for i, (is_dir, name, item) in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            if is_dir:
                out.write(prefix + branch + label(name + "/", item) + "\n")
                walk(item, prefix + ("    " if last else "│   "))
            else:
                out.write(prefix + branch + name + "  ({})\n".format(human(item[1])))

    for root, node in sorted(roots.items()):
        out.write(label(root, node) + "\n")
        walk(node, "")
        out.write("\n")


def write_tree_csv(rows, out):
    import csv
    w = csv.writer(out)
    w.writerow(["path", "type", "size", "modified"])
    for path, is_dir, size, mtime in rows:
        w.writerow([path, "folder" if is_dir else "file",
                    "" if is_dir else (size or 0),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                    if mtime else ""])


def write_tree_json(roots, out):
    import json

    def conv(name, node):
        return {"name": name, "size": node["size"], "files": node["count"],
                "folders": [conv(n, d) for n, d in _sorted_dirs(node)],
                "items": [{"name": f[0], "size": f[1]}
                          for f in _sorted_files(node)]}
    json.dump([conv(r, n) for r, n in sorted(roots.items())], out,
              indent=1, ensure_ascii=False)


def export_tree(conn, out_path, fmt=None, under=None):
    """Write the index as a tree. fmt = txt / csv / json, or taken from the
    file name. Returns (folders, files) written."""
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip(".").lower()
           or "txt").lower()
    if fmt not in ("txt", "csv", "json"):
        raise ValueError("format must be txt, csv or json, not " + fmt)
    sql = "SELECT path, is_dir, size, mtime FROM files"
    params = ()
    if under:
        sep = "\\" if "\\" in under or (len(under) > 1 and under[1] == ":") \
            else "/"
        sql += " WHERE path = ? OR path LIKE ? ESCAPE '!'"
        params = (under, like_escape(under.rstrip("\\/")) + sep + "%")
    rows = conn.execute(sql + " ORDER BY path", params).fetchall()
    folders = sum(1 for r in rows if r[1])
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        if fmt == "csv":
            write_tree_csv(rows, out)
        elif fmt == "json":
            write_tree_json(build_tree(rows), out)
        else:
            write_tree_text(build_tree(rows), out)
    return folders, len(rows) - folders


def cmd_tree(args):
    """Export the whole index as a file tree."""
    conn = open_db_ro(args.db)
    out = args.out or os.path.join(
        downloads_dir(), "findex-tree-{}.{}".format(
            time.strftime("%Y-%m-%d"), args.format or "txt"))
    try:
        folders, files = export_tree(conn, out, args.format, args.under)
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    finally:
        conn.close()
    print("Wrote {}\n  {:,} folders, {:,} files".format(out, folders, files))
    return 0


def cmd_journal(args):
    """Show what has changed inside the indexed locations."""
    conn = open_db(args.db)
    if args.clear:
        journal_clear(conn)
        print("Journal cleared.")
        return 0
    if args.keep_days is not None:
        set_meta(conn, "journal_days", args.keep_days)
        print("Keeping journal entries for {} days{}.".format(
            args.keep_days, " (0 = forever)" if not args.keep_days else ""))
    try:
        since = parse_since(args.since)
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    rows = journal_rows(conn, since=since, event=args.type, text=args.text,
                        limit=args.limit, under=args.under)
    conn.close()
    if not rows:
        print("Nothing in the journal for that.")
        return 0
    for _, ts, event, path, old_path, size, is_dir, source in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        what = event + ("/" if is_dir else "")
        line = "{}  {:<9} {}".format(when, what, path)
        if old_path:
            line += "   (was {})".format(old_path)
        if size and not is_dir:
            line += "   {}".format(human(size))
        print(line)
    print("{:,} change(s){}".format(
        len(rows), " - newest first, use -n for more" if len(rows) == args.limit
        else ""))
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
    p.add_argument("roots", nargs="*", help="folders or drives to index "
                   "(none = the remembered ones, see 'roots')")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--rebuild", action="store_true",
                   help="re-extract everything, ignore mtime checks")
    p.add_argument("--include-cloud", action="store_true",
                   help="extract text from OneDrive online-only files "
                        "(forces downloads; their names are indexed either way)")
    p.add_argument("--ocr", action="store_true",
                   help="OCR scanned PDFs with the engine built into Windows/"
                        "macOS, or tesseract (slow; first {} pages of each)"
                        .format(OCR_MAX_PAGES))
    p.add_argument("--progress", action="store_true",
                   help="emit machine-readable @P progress lines (used by the GUI)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("watch", help="live updates: index changes as they "
                                     "happen (until stopped)")
    p.add_argument("roots", nargs="*", help="folders or drives to watch "
                   "(none = the remembered ones)")
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

    p = sub.add_parser("roots", help="the folders and drives remembered for "
                       "indexing (used by 'index' and 'watch' when given none)")
    p.add_argument("--add", nargs="+", metavar="PATH", help="remember these")
    p.add_argument("--forget", nargs="+", metavar="PATH", help="drop these")
    p.set_defaults(func=cmd_roots)

    p = sub.add_parser("journal", help="what changed inside the indexed "
                       "locations: files added, modified, renamed, deleted")
    p.add_argument("text", nargs="?", help="only paths containing this")
    p.add_argument("--since", help="7d, 12h, 30m, or 2026-09-01")
    p.add_argument("--type", choices=("all",) + JOURNAL_EVENTS, default="all")
    p.add_argument("--under", metavar="FOLDER", help="only beneath this folder")
    p.add_argument("-n", "--limit", type=int, default=200)
    p.add_argument("--keep-days", type=float, metavar="DAYS",
                   help="set how long entries are kept (0 = forever)")
    p.add_argument("--clear", action="store_true", help="empty the journal")
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("tree", help="export the whole index as a file tree")
    p.add_argument("-o", "--out", help="file to write (default: Downloads/"
                   "findex-tree-DATE.txt)")
    p.add_argument("-f", "--format", choices=("txt", "csv", "json"),
                   help="txt = tree drawing (default), csv = one row per "
                   "path, json = nested")
    p.add_argument("--under", metavar="FOLDER", help="only this folder")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("vacuum", help="optimise and compact the database")
    p.set_defaults(func=cmd_vacuum)

    # A file name the console/pipe encoding cannot represent must never
    # kill a run (Windows pipes default to cp1252): replace, don't raise.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass    # no stream at all (windowed build), or not a text stream

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
