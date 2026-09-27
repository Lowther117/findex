#!/usr/bin/env python3
"""
findex_hash - content fingerprints for the findex index.

    findex hash                 fingerprint what exact-duplicate detection needs
    findex hash --all           quick fingerprint + detected type of EVERY file
    findex hash --full          full hash of every file (for snapshot/verify)
    findex hash --near          text fingerprints for near-duplicate documents
    findex hash --images        picture fingerprints for look-alike images
    findex dupes --exact        byte-identical files, whatever they are named
    findex dupes --near         documents whose text is nearly the same
    findex dupes --images       pictures that look the same (resized, re-saved)

Five things are stored per file, all in the `files` table findex.py owns:

    phash    BLAKE2b of the first 16 KB - a quick fingerprint that costs one
             short read. Two files with different phash are different files.
    fhash    BLAKE2b of the whole file. Equal fhash = identical bytes.
    kind     What the file actually IS, judged from its first bytes (pdf,
             docx, jpg, exe, text...) - as opposed to what its name claims.
    simhash  64-bit fingerprint of the extracted TEXT, so two documents
             that are the same apart from a few edits fingerprint alike.
    dhash    64-bit fingerprint of what an image LOOKS like (a difference
             hash), so the same picture saved as PNG and JPEG, shrunk for
             email or lightly edited fingerprints alike.

Hashing reads every byte of every file it covers, which on a big drive is
hours, so by default nothing is read that the question does not need: for
duplicates only files whose SIZE matches another file's are fingerprinted,
and only fingerprint-matches are hashed in full. findex.py's UPSERT drops
these columns whenever a file's size or timestamp changes, so nothing here
can go stale; a re-run only touches what is new.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import findex

PARTIAL = 16 * 1024          # bytes in the quick fingerprint (and sniffed)
WRITE_EVERY = 500            # rows per transaction while hashing
SIMHASH_MIN_CHARS = 200      # shorter texts fingerprint as noise
SIMHASH_MAX_CHARS = 100000   # more than this adds nothing to the fingerprint
NEAR_DISTANCE = 3            # simhash bits that may differ and still be "near"
IMAGE_DISTANCE = 10          # dhash bits that may differ and still "look alike"
DHASH_SIDE = 256             # images are decoded no larger than this first
DHASH_NONE = -(1 << 63)      # stored for an image that could not be decoded,
                             # so it is not tried again on every run
DHASH_FLAT_BITS = 8          # a fingerprint with this few bits set (or this
                             # few clear) is a featureless image: a solid
                             # colour, a plain gradient, a mostly-white page
                             # with a few lines of text - and looks like every
                             # other featureless image, so it is never grouped.
                             # A photo has about 32 of 64 bits set, give or
                             # take 4; fewer than 8 is not a picture of anything


# ----------------------------------------------------------------------------
# What is this file, really? (signature sniffing)
# ----------------------------------------------------------------------------

_ZIP_KINDS = (
    (b"word/", "docx"), (b"xl/", "xlsx"), (b"ppt/", "pptx"),
    (b"mimetypeapplication/epub", "epub"),
    (b"mimetypeapplication/vnd.oasis.opendocument.text", "odt"),
    (b"mimetypeapplication/vnd.oasis.opendocument.spreadsheet", "ods"),
    (b"mimetypeapplication/vnd.oasis.opendocument.presentation", "odp"),
    (b"META-INF/MANIFEST.MF", "jar"), (b"AndroidManifest.xml", "apk"),
)

_TEXT_CHARS = bytes(range(0x20, 0x7f)) + b"\t\n\r\x0b\x0c"
_NONTEXT = bytes(b for b in range(256) if b not in _TEXT_CHARS
                 and b < 0x80)


def sniff(head, size=None):
    """Name the type of a file from its first bytes. Returns a short lower-
    case label ('pdf', 'docx', 'jpg', 'exe', 'text', 'binary', 'empty').
    Only labels that are certain are returned - anything ambiguous is
    'binary' or 'text', which the mismatch report treats as unknown."""
    if not head:
        return "empty" if (size or 0) == 0 else "binary"
    h = head
    if h[:4] == b"%PDF" or b"%PDF-" in h[:1024]:
        return "pdf"
    if h[:4] == b"PK\x03\x04":
        for marker, kind in _ZIP_KINDS:
            if marker in h:
                return kind
        return "zip"
    if h[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"            # .doc/.xls/.ppt/.msg - one container for all
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if h[:3] == b"\xff\xd8\xff":
        return "jpg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if h[:4] == b"RIFF" and len(h) >= 12:
        tag = h[8:12]
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(
            tag, "riff")
    if h[:2] == b"BM" and len(h) > 14:
        return "bmp"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if h[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if h[:12].startswith(b"\x00\x00\x00") and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand.startswith(b"qt"):
            return "mov"
        if brand.startswith(b"M4A"):
            return "m4a"
        if brand.startswith(b"heic") or brand.startswith(b"heix") \
                or brand.startswith(b"mif1"):
            return "heic"
        return "mp4"
    if h[:3] == b"ID3" or (h[0] == 0xFF and h[1] in (0xFB, 0xF3, 0xF2, 0xFA)):
        return "mp3"
    if h[:4] == b"fLaC":
        return "flac"
    if h[:4] == b"OggS":
        return "ogg"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "mkv"            # also webm - same container
    if h[:7] == b"Rar!\x1a\x07\x00" or h[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if h[:2] == b"\x1f\x8b":
        return "gz"
    if h[:3] == b"BZh":
        return "bz2"
    if h[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if len(h) > 262 and h[257:262] == b"ustar":
        return "tar"
    if h[:2] == b"MZ":
        return "exe"            # exe, dll, sys, ocx - PE family
    if h[:4] == b"\x7fELF":
        return "elf"
    if h[:4] in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                 b"\xce\xfa\xed\xfe"):
        return "macho"
    if h[:16] == b"SQLite format 3\x00":
        return "sqlite"
    if h[:5] == b"{\\rtf":
        return "rtf"
    if h[:4] == b"%!PS":
        return "ps"
    if h[:4] == b"\x25\x50\x44\x46":
        return "pdf"
    if h[:8] == b"\x4d\x53\x43\x46\x00\x00\x00\x00":
        return "cab"
    if h[:4] == b"\x30\x26\xb2\x75":
        return "wma"
    if h[:4] == b"MSCF":
        return "cab"
    if h[:8] == b"!<arch>\n":
        return "ar"
    if h[:5] == b"<?xml" or h[:3] == b"\xef\xbb\xbf" and h[3:8] == b"<?xml":
        low = h[:2048].lower()
        if b"<html" in low or b"xhtml" in low:
            return "html"
        if b"<svg" in low:
            return "svg"
        return "xml"
    low = h[:1024].lstrip().lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
        return "html"
    if low.startswith(b"<svg"):
        return "svg"
    if h[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "text"           # UTF-16 with a byte-order mark
    # Text or binary? A NUL byte, or more than a small share of control
    # bytes, means binary. Non-ASCII bytes are allowed (UTF-8, Latin-1...).
    if b"\x00" in h:
        return "binary"
    ctrl = sum(1 for b in h if b < 0x20 and b not in (9, 10, 13, 11, 12, 27))
    if ctrl * 20 > len(h):
        return "binary"
    return "text"


# Extension -> the kinds a genuine file of that type sniffs as. Used by the
# health report: a file whose name and bytes disagree is either misnamed,
# a download that saved an error page, or damaged. Extensions not listed
# here are never flagged; 'text' and 'binary' are treated as unknown.
EXPECTED = {
    ".pdf": {"pdf"},
    ".docx": {"docx", "zip"}, ".docm": {"docx", "zip"},
    ".xlsx": {"xlsx", "zip"}, ".xlsm": {"xlsx", "zip"},
    ".pptx": {"pptx", "zip"}, ".pptm": {"pptx", "zip"},
    ".odt": {"odt", "zip"}, ".ods": {"ods", "zip"}, ".odp": {"odp", "zip"},
    ".epub": {"epub", "zip"}, ".zip": {"zip", "docx", "xlsx", "pptx", "jar",
                                      "epub", "odt", "ods", "odp", "apk"},
    ".jar": {"jar", "zip"}, ".apk": {"apk", "zip"}, ".cbz": {"zip"},
    ".doc": {"ole", "rtf", "docx"}, ".xls": {"ole", "xlsx", "html"},
    ".ppt": {"ole", "pptx"}, ".msg": {"ole"},
    ".rtf": {"rtf"},
    ".png": {"png"}, ".jpg": {"jpg"}, ".jpeg": {"jpg"}, ".gif": {"gif"},
    ".webp": {"webp"}, ".bmp": {"bmp"}, ".tif": {"tiff"}, ".tiff": {"tiff"},
    ".ico": {"ico"}, ".heic": {"heic"}, ".svg": {"svg", "xml"},
    ".mp4": {"mp4"}, ".m4v": {"mp4"}, ".mov": {"mov", "mp4"},
    ".m4a": {"m4a", "mp4"}, ".mp3": {"mp3"}, ".flac": {"flac"},
    ".ogg": {"ogg"}, ".opus": {"ogg"}, ".wav": {"wav"}, ".avi": {"avi"},
    ".mkv": {"mkv"}, ".webm": {"mkv"}, ".wma": {"wma"},
    ".rar": {"rar"}, ".7z": {"7z"}, ".gz": {"gz"}, ".tgz": {"gz"},
    ".bz2": {"bz2"}, ".xz": {"xz"}, ".tar": {"tar"}, ".cab": {"cab"},
    ".exe": {"exe"}, ".dll": {"exe"}, ".sys": {"exe"}, ".ocx": {"exe"},
    ".scr": {"exe"}, ".db": {"sqlite"}, ".sqlite": {"sqlite"},
    ".xml": {"xml", "text", "html", "svg"}, ".html": {"html", "text", "xml"},
    ".htm": {"html", "text", "xml"}, ".ps": {"ps"},
}


def mismatch(ext, kind):
    """True when the name says one thing and the bytes another - and both
    are known well enough to be sure."""
    want = EXPECTED.get(ext or "")
    if not want or not kind or kind in ("empty", "unreadable"):
        return False
    if kind in want:
        return False
    if kind in ("text", "binary"):
        # unknown content: only a problem for types that ALWAYS carry a
        # signature - a "PDF" or "JPEG" made of plain bytes is not one
        return ext in (".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg",
                       ".jpeg", ".gif", ".zip", ".exe", ".dll", ".mp3",
                       ".mp4", ".rar", ".7z", ".doc", ".xls", ".ppt", ".msg")
    return True


# ----------------------------------------------------------------------------
# Worker: fingerprint one file
# ----------------------------------------------------------------------------

def hash_file(job):
    """(id, path, size, want_full, allow_cloud) ->
       (id, phash, fhash, kind, error)
    phash/kind always; fhash when want_full or the file fits in one read."""
    fid, path, size, want_full, allow_cloud = job
    p = findex.lp(path)
    try:
        if os.name == "nt" and not allow_cloud:
            attrs = getattr(os.stat(p), "st_file_attributes", 0)
            if attrs & findex.CLOUD_MASK:
                return fid, None, None, None, "cloud placeholder"
        with open(p, "rb") as fh:
            head = fh.read(PARTIAL)
            kind = sniff(head, size)
            phash = hashlib.blake2b(head, digest_size=16).hexdigest()
            fhash = None
            if len(head) < PARTIAL:           # the head IS the whole file
                fhash = hashlib.blake2b(head, digest_size=32).hexdigest()
            elif want_full:
                h = hashlib.blake2b(head, digest_size=32)
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                fhash = h.hexdigest()
        return fid, phash, fhash, kind, None
    except FileNotFoundError:
        return fid, None, None, None, "gone"
    except PermissionError as exc:
        return fid, None, None, "unreadable", "permission: " + str(exc)[:120]
    except OSError as exc:
        return fid, None, None, "unreadable", str(exc)[:160]


# ----------------------------------------------------------------------------
# Text fingerprints (simhash) for near-duplicate documents
# ----------------------------------------------------------------------------

_WORD = re.compile(r"\w{2,}", re.UNICODE)


def simhash(text):
    """64-bit fingerprint of a text: documents that differ by a few edits
    get fingerprints that differ in a few bits. Word 3-grams hashed with
    BLAKE2b, the classic Charikar construction. Returns a SIGNED 64-bit int
    so SQLite stores it as an INTEGER."""
    words = _WORD.findall(text[:SIMHASH_MAX_CHARS].lower())
    if len(words) < 3:
        return None
    v = [0] * 64
    for i in range(len(words) - 2):
        gram = (words[i] + " " + words[i + 1] + " " + words[i + 2]).encode(
            "utf-8", "ignore")
        h = int.from_bytes(hashlib.blake2b(gram, digest_size=8).digest(),
                           "little")
        for bit in range(64):
            if h >> bit & 1:
                v[bit] += 1
            else:
                v[bit] -= 1
    out = 0
    for bit in range(64):
        if v[bit] > 0:
            out |= 1 << bit
    if out >= 1 << 63:
        out -= 1 << 64
    return out


def _simhash_batch(batch):
    """Worker: [(id, text)] -> [(id, simhash)]."""
    return [(fid, simhash(text)) for fid, text in batch]


def hamming(a, b):
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


# ----------------------------------------------------------------------------
# Picture fingerprints (dhash) for look-alike images
# ----------------------------------------------------------------------------

def _decode_grey(path, max_side=DHASH_SIDE):
    """Decode an image to 8-bit greyscale no larger than max_side on its
    longest side: (width, height, bytes), or None when nothing here can
    read it. PyMuPDF does the work, as everywhere else in findex; Pillow
    is tried when PyMuPDF is missing or cannot open the file (WebP, HEIC:
    formats it is not built for). Neither is a hard dependency."""
    p = findex.lp(path)
    if findex.HAVE_FITZ:
        try:
            return _decode_grey_fitz(p, max_side)
        except Exception:                                      # noqa: BLE001
            try:
                import PIL  # noqa: F401
            except ImportError:
                raise
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(p) as im:
        im = im.convert("L")
        w, h = im.size
        scale = min(1.0, float(max_side) / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        return im.width, im.height, im.tobytes()


def _decode_grey_fitz(p, max_side):
    fitz = findex.fitz
    px = None
    try:
        with open(p, "rb") as fh:
            px = findex._image_px(fh.read())
    except OSError:
        pass
    with findex._fitz_open(p) as doc:
        page = doc[0]
        w, h = page.rect.width, page.rect.height
        if w < 1 or h < 1:
            return None
        # the page is in points (pixels * 72 / dpi); render at the image's
        # own pixel size, then no larger than max_side
        native = (float(max(px)) / max(w, h)) if px else 1.0
        scale = native * min(1.0, float(max_side) / max(w * native, h * native))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                              colorspace=fitz.csGRAY, alpha=False)
        if pix.n != 1:
            return None
        return pix.width, pix.height, bytes(pix.samples)


def _grid_means(w, h, data, cols, rows):
    """Shrink a greyscale buffer to cols x rows by averaging the pixels that
    fall in each cell (a box filter). Done here rather than left to the
    decoder so every decoder gives the same fingerprint for the same
    picture; the buffer is at most DHASH_SIDE square, so it is quick."""
    out = []
    for r in range(rows):
        y0, y1 = h * r // rows, max(h * (r + 1) // rows, h * r // rows + 1)
        y1 = min(y1, h)
        for c in range(cols):
            x0, x1 = w * c // cols, max(w * (c + 1) // cols, w * c // cols + 1)
            x1 = min(x1, w)
            total = 0
            for y in range(y0, y1):
                total += sum(data[y * w + x0:y * w + x1])
            out.append(total / float((y1 - y0) * (x1 - x0)))
    return out


def dhash_grey(w, h, data):
    """The difference hash of a greyscale image: shrink to 9 wide by 8
    high, then one bit per pair of horizontal neighbours - is the left
    pixel brighter than the right? 64 bits that describe the picture's
    gradients rather than its pixels, so resizing, re-encoding and small
    edits leave most of them alone. Signed 64-bit for SQLite."""
    if w < 2 or h < 1:
        return None
    cells = _grid_means(w, h, data, 9, 8)
    out = 0
    bit = 0
    for r in range(8):
        for c in range(8):
            if cells[r * 9 + c] > cells[r * 9 + c + 1]:
                out |= 1 << bit
            bit += 1
    if out >= 1 << 63:
        out -= 1 << 64
    return out


def dhash_file(job):
    """Worker: (id, path) -> (id, dhash, error). An image nothing can
    decode gets DHASH_NONE, so it is not reopened on every run."""
    fid, path = job
    if not os.path.exists(findex.lp(path)):
        return fid, None, "gone"
    try:
        decoded = _decode_grey(path)
    except FileNotFoundError:
        return fid, None, "gone"
    except Exception as exc:                                   # noqa: BLE001
        return fid, DHASH_NONE, str(exc)[:160]
    if decoded is None:
        return fid, DHASH_NONE, "no decoder"
    h = dhash_grey(*decoded)
    return fid, (DHASH_NONE if h is None else h), None


# ----------------------------------------------------------------------------
# Choosing what to hash, and doing it
# ----------------------------------------------------------------------------

def _scope(under, exts, alias="f"):
    """WHERE fragments for --under / --ext."""
    where, params = [], []
    if under:
        sep = "\\" if "\\" in under or (len(under) > 1 and under[1] == ":") \
            else "/"
        where.append("{}.path LIKE ? ESCAPE '!'".format(alias))
        params.append(findex.like_escape(under.rstrip("\\/")) + sep + "%")
    if exts:
        norm = ["." + e.lstrip(".").lower() for e in exts]
        where.append("{}.ext IN ({})".format(alias, ",".join("?" * len(norm))))
        params += norm
    return where, params


def jobs_for(conn, mode, under=None, exts=None, allow_cloud=False):
    """The files that need reading for `mode`, as hash_file jobs.

    dupes  size-collision candidates without a phash (stage 1), then
           phash-collision candidates without an fhash (stage 2) - the
           caller runs stage 1, then asks again for stage 2
    all    every file without a phash
    full   every file without an fhash
    """
    where, params = _scope(under, exts)
    base = "f.is_dir=0 AND f.size>0"
    if where:
        base += " AND " + " AND ".join(where)
    if mode == "all":
        sql = ("SELECT f.id, f.path, f.size FROM files f WHERE {} AND "
               "f.phash IS NULL".format(base))
        full = False
    elif mode == "full":
        sql = ("SELECT f.id, f.path, f.size FROM files f WHERE {} AND "
               "f.fhash IS NULL".format(base))
        full = True
    elif mode == "dupes1":
        sql = ("SELECT f.id, f.path, f.size FROM files f WHERE {} AND "
               "f.phash IS NULL AND f.size IN (SELECT size FROM files g "
               "WHERE g.is_dir=0 AND g.size>0 GROUP BY size "
               "HAVING COUNT(*)>1)".format(base))
        full = False
    elif mode == "dupes2":
        sql = ("SELECT f.id, f.path, f.size FROM files f WHERE {} AND "
               "f.fhash IS NULL AND f.phash IS NOT NULL AND "
               "(f.size, f.phash) IN (SELECT size, phash FROM files g "
               "WHERE g.is_dir=0 AND g.phash IS NOT NULL "
               "GROUP BY size, phash HAVING COUNT(*)>1)".format(base))
        full = True
    else:
        raise ValueError(mode)
    return [(fid, path, size, full, allow_cloud)
            for fid, path, size in conn.execute(sql, params)]


def run_hash(conn, mode="dupes", under=None, exts=None, workers=None,
             allow_cloud=False, progress=False, log=print):
    """Fingerprint whatever `mode` needs. Returns a stats dict."""
    workers = workers or min(8, max(2, (os.cpu_count() or 4) // 2))
    stats = {"read": 0, "hashed": 0, "errors": 0, "gone": 0, "bytes": 0,
             "cloud": 0}
    start = time.time()
    stages = ["dupes1", "dupes2"] if mode == "dupes" else [mode]
    for stage in stages:
        jobs = jobs_for(conn, stage, under, exts, allow_cloud)
        if not jobs:
            continue
        total = len(jobs)
        label = {"dupes1": "fingerprinting size-collision candidates",
                 "dupes2": "hashing fingerprint matches in full",
                 "all": "fingerprinting every file",
                 "full": "hashing every file in full"}[stage]
        log("{}: {:,} files...".format(label, total))
        cur = conn.cursor()
        cur.execute("BEGIN")
        pending = 0
        last_emit = 0.0
        # big files first so the slow ones do not all land at the end
        jobs.sort(key=lambda j: -j[2])
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for fid, phash, fhash, kind, err in ex.map(hash_file, jobs,
                                                       chunksize=16):
                stats["read"] += 1
                if err == "gone":
                    stats["gone"] += 1
                elif err == "cloud placeholder":
                    stats["cloud"] += 1
                elif err:
                    stats["errors"] += 1
                    cur.execute("UPDATE files SET kind=? WHERE id=?",
                                ("unreadable", fid))
                else:
                    stats["hashed"] += 1
                    cur.execute(
                        "UPDATE files SET phash=?, fhash=COALESCE(?, fhash), "
                        "kind=? WHERE id=?", (phash, fhash, kind, fid))
                pending += 1
                if pending >= WRITE_EVERY:
                    conn.commit()
                    cur.execute("BEGIN")
                    pending = 0
                if progress and time.time() - last_emit > 0.4:
                    last_emit = time.time()
                    print("@P seen={} done={} total={} error={} elapsed={:.1f}"
                          .format(stats["read"], stats["hashed"], total,
                                  stats["errors"], time.time() - start),
                          flush=True)
        conn.commit()
        if progress:
            print("@P seen={} done={} total={} error={} elapsed={:.1f}"
                  .format(stats["read"], stats["hashed"], total,
                          stats["errors"], time.time() - start), flush=True)
    stats["elapsed"] = time.time() - start
    return stats


def run_simhash(conn, under=None, exts=None, workers=None, progress=False,
                log=print):
    """Text fingerprints for every document with extracted text and no
    simhash yet. Text comes from the FTS table, so no file is opened."""
    workers = workers or max(2, (os.cpu_count() or 4) - 1)
    where, params = _scope(under, exts)
    sql = ("SELECT f.id FROM files f WHERE f.is_dir=0 AND f.chars>=? AND "
           "f.simhash IS NULL" + ("".join(" AND " + w for w in where)))
    ids = [r[0] for r in conn.execute(sql, [SIMHASH_MIN_CHARS] + params)]
    stats = {"documents": len(ids), "fingerprinted": 0}
    if not ids:
        return stats
    log("text-fingerprinting {:,} documents...".format(len(ids)))
    start = time.time()
    done = 0

    def batches():
        for i in range(0, len(ids), 64):
            chunk = ids[i:i + 64]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT rowid, substr(body, 1, ?) FROM docs WHERE rowid IN ({})"
                .format(marks), [SIMHASH_MAX_CHARS] + chunk).fetchall()
            yield rows

    cur = conn.cursor()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for result in ex.map(_simhash_batch, batches()):
            cur.execute("BEGIN")
            for fid, sh in result:
                if sh is not None:
                    cur.execute("UPDATE files SET simhash=? WHERE id=?",
                                (sh, fid))
                    stats["fingerprinted"] += 1
            conn.commit()
            done += len(result)
            if progress:
                print("@P seen={} done={} total={} elapsed={:.1f}".format(
                    done, stats["fingerprinted"], len(ids),
                    time.time() - start), flush=True)
    stats["elapsed"] = time.time() - start
    return stats


def run_dhash(conn, under=None, exts=None, workers=None, progress=False,
              log=print, allow_cloud=False):
    """Picture fingerprints for every image file without one. Each image
    is opened and decoded (small - the longest side is reduced to
    DHASH_SIDE px on the way in), so this is a real read of every picture
    once; findex.py's UPSERT drops the fingerprint when the file changes."""
    workers = workers or min(8, max(2, (os.cpu_count() or 4) // 2))
    where, params = _scope(under, exts)
    image_exts = sorted(findex.IMAGE_EXTS)
    sql = ("SELECT f.id, f.path FROM files f WHERE f.is_dir=0 AND f.size>0 "
           "AND f.dhash IS NULL AND f.ext IN ({})".format(
               ",".join("?" * len(image_exts)))
           + "".join(" AND " + w for w in where))
    jobs = conn.execute(sql, image_exts + params).fetchall()
    if os.name == "nt" and not allow_cloud:
        kept = []
        for fid, path in jobs:
            try:
                attrs = getattr(os.stat(findex.lp(path)),
                                "st_file_attributes", 0)
            except OSError:
                attrs = 0
            if not attrs & findex.CLOUD_MASK:
                kept.append((fid, path))
        jobs = kept
    stats = {"images": len(jobs), "fingerprinted": 0, "errors": 0, "gone": 0}
    if not jobs:
        return stats
    log("picture-fingerprinting {:,} images...".format(len(jobs)))
    start = time.time()
    cur = conn.cursor()
    cur.execute("BEGIN")
    pending = 0
    last_emit = 0.0
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fid, dh, err in ex.map(dhash_file, jobs, chunksize=16):
            done += 1
            if err == "gone":
                stats["gone"] += 1
            else:
                if err:
                    stats["errors"] += 1
                else:
                    stats["fingerprinted"] += 1
                cur.execute("UPDATE files SET dhash=? WHERE id=?", (dh, fid))
            pending += 1
            if pending >= WRITE_EVERY:
                conn.commit()
                cur.execute("BEGIN")
                pending = 0
            if progress and time.time() - last_emit > 0.4:
                last_emit = time.time()
                print("@P seen={} done={} total={} error={} elapsed={:.1f}"
                      .format(done, stats["fingerprinted"], len(jobs),
                              stats["errors"], time.time() - start),
                      flush=True)
    conn.commit()
    if progress:
        print("@P seen={} done={} total={} error={} elapsed={:.1f}".format(
            done, stats["fingerprinted"], len(jobs), stats["errors"],
            time.time() - start), flush=True)
    stats["elapsed"] = time.time() - start
    return stats


# ----------------------------------------------------------------------------
# Duplicate queries (read-only)
# ----------------------------------------------------------------------------

def exact_dupe_rows(conn, limit=0, exts=None, under=None):
    """Byte-identical files: [(path, size, mtime, copies, fhash)] grouped,
    biggest first. Only files that HAVE a full hash take part - run
    `findex hash` (or dupes --exact without --no-hash) first."""
    where, params = _scope(under, exts)
    extra = "".join(" AND " + w for w in where)
    sql = ("SELECT f.path, f.size, f.mtime, d.n, f.fhash FROM files f JOIN "
           "(SELECT fhash, COUNT(*) AS n FROM files WHERE is_dir=0 AND "
           "fhash IS NOT NULL AND size>0 GROUP BY fhash HAVING COUNT(*)>1) d "
           "ON f.fhash = d.fhash WHERE f.is_dir=0{} "
           "ORDER BY f.size DESC, f.fhash, f.path".format(extra))
    if int(limit) > 0:
        sql += " LIMIT ?"
        params = params + [int(limit)]
    return conn.execute(sql, params).fetchall()


def exact_dupe_summary(conn, exts=None, under=None):
    """(groups, files, wasted_bytes) over byte-identical files."""
    where, params = _scope(under, exts)
    extra = "".join(" AND " + w for w in where)
    return conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM((n-1)*size),0) "
        "FROM (SELECT f.fhash, f.size, COUNT(*) AS n FROM files f WHERE "
        "f.is_dir=0 AND f.fhash IS NOT NULL AND f.size>0{} "
        "GROUP BY f.fhash HAVING COUNT(*)>1)".format(extra), params).fetchone()


def hamming_groups(hashes, distance, bands=4):
    """Group 64-bit fingerprints that lie within `distance` bits of each
    other (transitively). Returns [[index, ...], ...] for groups of two or
    more, in no particular order.

    Candidates must share at least one of `bands` equal slices of the hash
    (with 4 slices of 16 bits, a distance of up to 3 is guaranteed to leave
    one slice untouched; with 8 slices of 8 bits, up to 7), so only rows in
    a shared bucket are compared - not all n^2 pairs. Beyond that guarantee
    the odds of a genuine pair touching every band are small; a 2000-strong
    bucket is boilerplate, not nearness, and is left alone."""
    n = len(hashes)
    if n < 2:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    sh = [h & 0xFFFFFFFFFFFFFFFF for h in hashes]
    width = 64 // bands
    mask = (1 << width) - 1
    for band in range(bands):
        buckets = {}
        shift = band * width
        for i, h in enumerate(sh):
            buckets.setdefault((h >> shift) & mask, []).append(i)
        for members in buckets.values():
            if len(members) < 2 or len(members) > 2000:
                continue
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    i, j = members[x], members[y]
                    if find(i) != find(j) and hamming(sh[i], sh[j]) <= distance:
                        union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def near_dupe_groups(conn, exts=None, under=None, distance=NEAR_DISTANCE):
    """Documents whose text fingerprints are within `distance` bits.
    Returns [[(path, size, mtime, chars, fhash), ...], ...] - each inner
    list one group, largest groups first. Exact duplicates (same fhash)
    fall into the same group, naturally."""
    where, params = _scope(under, exts)
    extra = "".join(" AND " + w for w in where)
    rows = conn.execute(
        "SELECT f.id, f.path, f.size, f.mtime, f.chars, f.fhash, f.simhash "
        "FROM files f WHERE f.is_dir=0 AND f.simhash IS NOT NULL{}"
        .format(extra), params).fetchall()
    out = [sorted((rows[i][1:6] for i in g), key=lambda t: (-(t[1] or 0), t[0]))
           for g in hamming_groups([r[6] for r in rows], distance, bands=4)]
    out.sort(key=lambda g: (-len(g), -(g[0][1] or 0)))
    return out


def similar_image_groups(conn, exts=None, under=None, distance=IMAGE_DISTANCE):
    """Images whose picture fingerprints are within `distance` bits: the
    same photo as PNG and JPEG, the original and the copy shrunk for email,
    a screenshot and its crop-free re-save. Returns [[(path, size, mtime,
    dhash), ...], ...], largest groups first. 8 bands of 8 bits, so any
    pair up to 7 bits apart is found for certain and the default 10 almost
    always; images that could not be decoded (DHASH_NONE) and featureless
    ones (DHASH_FLAT_BITS) are left out."""
    where, params = _scope(under, exts)
    extra = "".join(" AND " + w for w in where)
    rows = conn.execute(
        "SELECT f.id, f.path, f.size, f.mtime, f.dhash FROM files f "
        "WHERE f.is_dir=0 AND f.dhash IS NOT NULL AND f.dhash != ?{}"
        .format(extra), [DHASH_NONE] + params).fetchall()
    # A blank page, a solid swatch and a smooth gradient all fingerprint as
    # (nearly) all-zero or all-one bits, and would be reported as one huge
    # set of "similar images" - true in a useless way. Leave them out.
    rows = [r for r in rows
            if DHASH_FLAT_BITS < bin(r[4] & 0xFFFFFFFFFFFFFFFF).count("1")
            < 64 - DHASH_FLAT_BITS]
    bands = 8 if distance > NEAR_DISTANCE else 4
    out = [sorted((rows[i][1:5] for i in g), key=lambda t: (-(t[1] or 0), t[0]))
           for g in hamming_groups([r[4] for r in rows], distance, bands)]
    out.sort(key=lambda g: (-len(g), -(g[0][1] or 0)))
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_hash(args):
    conn = findex.open_db(args.db)
    mode = "dupes"
    if args.full:
        mode = "full"
    elif args.all:
        mode = "all"
    log = (lambda *a: None) if args.quiet else print
    only = args.near_only or args.images_only
    if not only:
        stats = run_hash(conn, mode, under=args.under, exts=args.ext,
                         workers=args.workers, allow_cloud=args.include_cloud,
                         progress=args.progress, log=log)
        log("read {:,} files, fingerprinted {:,}, {:,} unreadable, {:,} gone, "
            "{:,} cloud placeholders skipped, {:.1f}s".format(
                stats["read"], stats["hashed"], stats["errors"], stats["gone"],
                stats["cloud"], stats.get("elapsed", 0)))
    if args.near or args.near_only:
        s = run_simhash(conn, under=args.under, exts=args.ext,
                        workers=args.workers, progress=args.progress, log=log)
        log("text-fingerprinted {:,} of {:,} documents, {:.1f}s".format(
            s["fingerprinted"], s["documents"], s.get("elapsed", 0)))
    if args.images or args.images_only:
        s = run_dhash(conn, under=args.under, exts=args.ext,
                      workers=args.workers, progress=args.progress, log=log,
                      allow_cloud=args.include_cloud)
        log("picture-fingerprinted {:,} of {:,} images, {:,} unreadable, "
            "{:.1f}s".format(s["fingerprinted"], s["images"], s["errors"],
                             s.get("elapsed", 0)))
    conn.close()
    return 0


def _group_records(groups, mode, start=0):
    """Duplicate sets as result records with a `set` number (continuing
    from `start`, so sets from --exact, --near and --images in one run do
    not share numbers), for --json / --csv (findex.write_results)."""
    out = []
    for n, g in enumerate(groups, start + 1):
        for row in g:
            rec = findex.result_record(row[0], row[1], row[2])
            rec["set"] = n
            rec["match"] = mode
            out.append(rec)
    return out


def _last_set(records):
    return records[-1]["set"] if records else 0


def cmd_dupes(args):
    """`findex dupes --exact` / `--near` / `--images` land here
    (findex.cmd_dupes hands them over)."""
    conn = findex.open_db(args.db)
    fmt = findex._machine_out(args)
    quiet = (lambda *a: None) if fmt else print
    if not getattr(args, "no_hash", False):
        if args.exact:
            run_hash(conn, "dupes", under=args.under, exts=args.ext, log=quiet)
        if args.near:
            run_simhash(conn, under=args.under, exts=args.ext, log=quiet)
        if getattr(args, "images", False):
            run_dhash(conn, under=args.under, exts=args.ext, log=quiet)
    distance = getattr(args, "distance", None)
    if distance is not None:
        distance = max(0, min(64, distance))
    records = []
    if args.exact:
        groups, files, wasted = exact_dupe_summary(conn, args.ext, args.under)
        rows = exact_dupe_rows(conn, args.limit, args.ext, args.under)
        if fmt:
            sets, last = [], None
            for path, size, mtime, n, fh in rows:
                if fh != last:
                    last = fh
                    sets.append([])
                sets[-1].append((path, size, mtime))
            records += _group_records(sets, "identical", _last_set(records))
        last = None
        for path, size, mtime, n, fh in rows:
            if fh != last:
                last = fh
                quiet("\n{} - {:,} identical copies:".format(
                    findex.human(size), n))
            quiet("    {}".format(path))
        if groups:
            quiet("\n{:,} set(s) of identical files, {:,} files - {} "
                  "reclaimable if each set kept one copy".format(
                      groups, files, findex.human(wasted)))
        else:
            quiet("No byte-identical duplicates found.")
    if args.near:
        groups = near_dupe_groups(conn, args.ext, args.under,
                                  distance if distance is not None
                                  else NEAR_DISTANCE)
        groups = groups[:args.limit or None]
        records += _group_records(groups, "near-text", _last_set(records))
        for g in groups:
            quiet("\n{} similar documents:".format(len(g)))
            for path, size, mtime, chars, fh in g:
                quiet("    {:>8}  {}  {}".format(
                    findex.human(size or 0),
                    time.strftime("%Y-%m-%d", time.localtime(mtime or 0)), path))
        quiet("\n{:,} group(s) of near-identical documents".format(len(groups))
              if groups else "No near-duplicate documents found (run "
              "`findex hash --near` first if the index has never been "
              "text-fingerprinted).")
    if getattr(args, "images", False):
        groups = similar_image_groups(conn, args.ext, args.under,
                                      distance if distance is not None
                                      else IMAGE_DISTANCE)
        groups = groups[:args.limit or None]
        records += _group_records(groups, "similar-image",
                                  _last_set(records))
        for g in groups:
            quiet("\n{} similar images:".format(len(g)))
            for path, size, mtime, dh in g:
                quiet("    {:>8}  {}  {}".format(
                    findex.human(size or 0),
                    time.strftime("%Y-%m-%d", time.localtime(mtime or 0)), path))
        quiet("\n{:,} group(s) of look-alike images".format(len(groups))
              if groups else "No look-alike images found (run `findex hash "
              "--images` first if the index has never been "
              "picture-fingerprinted).")
    if fmt:
        findex.write_results(records, sys.stdout, fmt, group_key="set")
    conn.close()
    return 0


def add_commands(sub):
    p = sub.add_parser("hash", help="content fingerprints: for exact "
                       "duplicates (default), --all types, --full hashes, "
                       "--near text fingerprints, --images picture "
                       "fingerprints")
    p.add_argument("--all", action="store_true",
                   help="quick fingerprint + detected type of every file "
                        "(reads 16 KB each; feeds the type-mismatch report)")
    p.add_argument("--full", action="store_true",
                   help="full hash of every file (reads everything; needed "
                        "for hashed snapshots and moved-file detection)")
    p.add_argument("--near", action="store_true",
                   help="also text-fingerprint documents for --near dupes")
    p.add_argument("--near-only", action="store_true",
                   help="only the text fingerprints - open no files")
    p.add_argument("--images", action="store_true",
                   help="also picture-fingerprint image files for --images "
                        "dupes (decodes each image once, small)")
    p.add_argument("--images-only", action="store_true",
                   help="only the picture fingerprints")
    p.add_argument("--under", metavar="FOLDER", help="only beneath this folder")
    p.add_argument("-e", "--ext", nargs="+", help="restrict to extensions")
    p.add_argument("--workers", type=int, default=None,
                   help="parallel readers (default: half the cores, max 8 - "
                        "hashing is disk-bound, not CPU-bound)")
    p.add_argument("--include-cloud", action="store_true",
                   help="also read OneDrive online-only files (downloads them)")
    p.add_argument("--progress", action="store_true",
                   help="emit @P progress lines (used by the desktop app)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_hash)


if __name__ == "__main__":
    sys.exit(findex.main())
