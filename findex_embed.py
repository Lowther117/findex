#!/usr/bin/env python3
"""
findex_embed - search by meaning.

    findex embed                      vectorise the text of every indexed file
    findex embed D:\\Shared            ...just one folder
    findex embed --status             how much is covered, with which model
    findex embed --setup              fetch the engine and the embedding model
    findex embed --forget             drop the vectors (the index is untouched)
    findex find "~boiler warranty letter"
    findex find "~the house survey from 2019 ext:pdf"

Keyword search finds the words you remember. This finds what you MEAN: a
local embedding model (Ollama's nomic-embed-text, 274 MB, nothing leaves
the computer) turns the text findex has already extracted into vectors,
and a query is answered by the files whose vectors point the same way -
"letter about the boiler warranty" finds the letter that says "Worcester
Bosch guarantee" and never uses the word boiler.

What is stored: for each file with text, pieces of about CHUNK_CHARS
characters - the whole document when it is up to CHUNKS pieces long (a few
pages, which is most files), else CHUNKS pieces spread through it - each
as 768 int8 numbers plus one scale, about 780 bytes a piece, in the same
database. Roughly two thirds of the size of the text for short files.
A re-run only touches files whose text changed, and files that have gone
are dropped. On a graphics card this takes minutes for tens of thousands
of documents; on a CPU, hours - which is why the Index tab lets it run on
its own after each index run.

In a query, ~ starts the meaning part: everything after it that is not a
filter (ext:, C:\\, !word, content:, folder:) is the description.
"about:" does the same for one quoted phrase: about:"boiler warranty".
Filters apply as usual, so  ~mortgage offer ext:pdf D:\\Docs  works. The
best MEANING_TOP matches are listed, best first, down to the point where
matches fall clearly short of the best one; the preview of a match says how
close it is.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time

import findex
import findex_summary as fs

MODEL = fs.EMBED_MODEL          # the embedding model (findex_summary knows
MODEL_SIZE = fs.EMBED_MODEL_SIZE   # about its download too)
CHUNK_CHARS = 1200              # one piece of a document (about 300 tokens)
CHUNKS = 12                     # pieces per document: all of it up to this many
SAMPLE_CHARS = 90000            # text looked at per document for the spread
BATCH_INPUTS = 32               # pieces per request to the engine
COMMIT_EVERY = 200              # files per commit
MEANING_TOP = 300               # files a meaning search lists
MIN_SCORE = 0.35                # below this a match is noise, not shown...
DROP_BELOW_BEST = 0.20          # ...and so is anything this far off the best
MAX_FAILED_BATCHES = 3          # consecutive engine failures that end a run
SNIPPET_CHARS = 220             # of the best-matching piece, for the preview
# nomic-embed-text wants to be told which side of the search a text is on;
# other models ignore the prefix.
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeds (
    file_id INTEGER NOT NULL,
    chunk   INTEGER NOT NULL,
    pos     INTEGER NOT NULL,
    len     INTEGER NOT NULL,
    scale   REAL    NOT NULL,
    vec     BLOB    NOT NULL,
    PRIMARY KEY (file_id, chunk)
);
CREATE TABLE IF NOT EXISTS embedded (
    file_id INTEGER PRIMARY KEY,
    sig     TEXT    NOT NULL,
    chunks  INTEGER NOT NULL
);
"""


class EmbedError(Exception):
    pass


def _np():
    try:
        import numpy
    except ImportError:
        raise EmbedError("search by meaning needs the numpy package - the "
                         "desktop app installs it on its next start, or: "
                         "python -m pip install numpy")
    return numpy


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def have_tables(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeds'"
    ).fetchone())


# ----------------------------------------------------------------------------
# Making vectors
# ----------------------------------------------------------------------------

def pieces(text):
    """[(pos, chunk_text)]: a text up to CHUNKS * CHUNK_CHARS long (a few
    pages - most documents) is covered completely, piece after piece; a
    longer one gets CHUNKS pieces spread evenly through it, the start and
    the end among them, so what sits between two pieces is not seen."""
    text = text or ""
    n = len(text)
    if n <= CHUNK_CHARS:
        return [(0, text)] if text.strip() else []
    out = []
    if n <= CHUNKS * CHUNK_CHARS:
        pos = 0
        while pos < n:
            end = min(n, pos + CHUNK_CHARS)
            if end < n:     # break at a space, not mid-word
                space = text.rfind(" ", pos + CHUNK_CHARS // 2, end)
                if space > 0:
                    end = space + 1
            piece = text[pos:end]
            if piece.strip():
                out.append((pos, piece))
            pos = end
        return out
    step = (n - CHUNK_CHARS) / float(CHUNKS - 1)
    for i in range(CHUNKS):
        pos = int(round(i * step))
        if i:       # start at a word, not mid-way through one
            space = text.find(" ", pos, pos + 80)
            if space > 0:
                pos = space + 1
        piece = text[pos:pos + CHUNK_CHARS]
        if piece.strip():
            out.append((pos, piece))
    return out


def embed_texts(texts, model=None, url=None, timeout=600):
    """Vectors for a list of texts from the running engine: a list of
    lists of floats (one per text, in order)."""
    url = (url or fs.AI_URL).rstrip("/")
    payload = {"model": model or MODEL, "input": texts, "truncate": True,
               "keep_alive": fs.AI_KEEP_ALIVE}
    try:
        with fs._http(url + "/api/embed", payload, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:                                   # noqa: BLE001
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:                                      # noqa: BLE001
            pass
        raise EmbedError("the AI engine did not answer: {} {}".format(
            exc, detail).strip())
    if data.get("error"):
        raise EmbedError(str(data["error"]))
    vecs = data.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        raise EmbedError("the engine returned {} vectors for {} texts".format(
            len(vecs) if isinstance(vecs, list) else "no", len(texts)))
    return vecs


def quantise(vec):
    """A float vector -> (scale, int8 bytes) of its unit-length form, so a
    dot product with a unit query vector is the cosine similarity."""
    np = _np()
    v = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    if norm > 0:
        v = v / norm
    top = float(np.abs(v).max()) if v.size else 0.0
    if top <= 0:
        return 1.0, bytes(v.size)
    scale = top / 127.0
    q = np.clip(np.rint(v / scale), -127, 127).astype(np.int8)
    return scale, q.tobytes()


def _sig(mtime, chars):
    return "{}:{}".format(int(mtime or 0), chars or 0)


def _need_engine(url, log, progress=False, install=True):
    """The engine running with the embedding model installed, or raise."""
    st = fs.ai_start(url, log, install=install, progress=progress)
    if not st["ok"]:
        raise EmbedError("the AI engine is not running ({}). On the Summary "
                         "tab, AI summaries > Set up fetches it; or: findex "
                         "embed --setup".format(st["error"]))
    if not fs.has_model(st["models"], MODEL):
        log("The embedding model {} ({}) is not installed yet - "
            "downloading it...".format(MODEL, MODEL_SIZE))
        if not fs.ai_pull(MODEL, url, progress, log):
            raise EmbedError("could not download {}".format(MODEL))
    return st


def setup(url=None, progress=False, log=print):
    """Fetch the engine (if findex has none) and the embedding model."""
    try:
        _need_engine(url, log, progress)
    except EmbedError as exc:
        log(str(exc))
        return 2
    log("Search by meaning is ready: {} is installed.".format(MODEL))
    return 0


def run(db, scope="", url=None, rebuild=False, progress=False, log=print,
        batch_inputs=BATCH_INPUTS):
    """Vectorise every file with text under `scope` (all, when '') whose
    vectors are missing or stale; drop vectors of files that have gone.
    Returns 0 ok, 1 nothing to do, 2 the engine is not available."""
    _np()
    t0 = time.time()
    conn = findex.open_db(db)
    try:
        ensure_schema(conn)
        scope = fs.norm_scope(scope)
        try:
            _need_engine(url, log, progress)
        except EmbedError as exc:
            log(str(exc))
            return 2
        stored_model = findex.get_meta(conn, "embed_model")
        if stored_model and stored_model != MODEL:
            log("The vectors were made with {} - remaking them all with {}."
                .format(stored_model, MODEL))
            rebuild = True
        if rebuild:
            conn.execute("DELETE FROM embeds")
            conn.execute("DELETE FROM embedded")
            conn.commit()
        # files that have gone, or lost their text
        gone = conn.execute(
            "SELECT e.file_id FROM embedded e LEFT JOIN files f ON f.id = "
            "e.file_id WHERE f.id IS NULL OR f.chars IS NULL OR f.chars = 0"
        ).fetchall()
        for (fid,) in gone:
            conn.execute("DELETE FROM embeds WHERE file_id=?", (fid,))
            conn.execute("DELETE FROM embedded WHERE file_id=?", (fid,))
        if gone:
            conn.commit()
        where, params = fs._scope_sql(scope)
        todo = conn.execute(
            "SELECT f.id, f.name, f.mtime, f.chars, e.sig FROM files f LEFT "
            "JOIN embedded e ON e.file_id = f.id WHERE f.chars > 0 AND "
            "f.is_dir = 0" + where, params).fetchall()
        todo = [(fid, name, mtime, chars) for fid, name, mtime, chars, sig
                in todo if sig != _sig(mtime, chars)]
        total = len(todo)
        log("{:,} file(s) to vectorise{} with {}{}".format(
            total, " under " + scope if scope else "", MODEL,
            "; {:,} dropped (file gone)".format(len(gone)) if gone else ""))
        if not total:
            fs._emit(progress, 0, 0, 0, t0)
            return 1 if not gone else 0
        where_loaded = fs.ai_warm(MODEL, url)
        if where_loaded:
            log("{} is loaded {}.".format(MODEL, where_loaded["text"]))
        fs._emit(progress, 0, total, 0, t0)
        done = failed = 0
        streak = 0              # consecutive failed batches
        pending = []            # (fid, chunk, pos, len, text)
        owners = []             # files whose pieces are all in `pending`

        def bump():
            """New vectors are on disk: searchers reload their matrix."""
            findex.set_meta(conn, "embed_version",
                            str(int(findex.get_meta(conn, "embed_version")
                                    or 0) + 1))

        def flush():
            """Embed what is pending and write it - a file's old vectors
            are replaced only once its new ones are in hand, so a failed
            request leaves everything as it was."""
            nonlocal pending, owners, done, failed, streak
            if not pending and not owners:
                return
            vecs = []
            if pending:
                texts = [t for *_, t in pending]
                for attempt in (0, 1):
                    try:
                        vecs = embed_texts(texts, MODEL, url)
                        break
                    except EmbedError as exc:
                        if attempt:
                            log("  a batch of {} pieces failed: {}".format(
                                len(texts), exc))
                            vecs = None
                        else:
                            time.sleep(2)
            if vecs is None:
                failed += len(owners)
                streak += 1
                pending, owners = [], []
                return
            streak = 0
            dim = len(vecs[0]) if vecs and vecs[0] else 0
            for fid, sig, n in owners:
                conn.execute("DELETE FROM embeds WHERE file_id=?", (fid,))
            for (fid, chunk, pos, ln, _t), v in zip(pending, vecs):
                scale, blob = quantise(v)
                conn.execute(
                    "INSERT OR REPLACE INTO embeds (file_id, chunk, pos, "
                    "len, scale, vec) VALUES (?,?,?,?,?,?)",
                    (fid, chunk, pos, ln, scale, blob))
            if dim and findex.get_meta(conn, "embed_dim") != str(dim):
                findex.set_meta(conn, "embed_dim", str(dim))
            for fid, sig, n in owners:
                conn.execute(
                    "INSERT OR REPLACE INTO embedded (file_id, sig, "
                    "chunks) VALUES (?,?,?)", (fid, sig, n))
            done += len(owners)
            pending, owners = [], []
            bump()
            conn.commit()

        last_emit = time.time()
        for i, (fid, name, mtime, chars) in enumerate(todo):
            row = conn.execute("SELECT substr(body, 1, ?) FROM docs WHERE "
                               "rowid=?", (SAMPLE_CHARS, fid)).fetchone()
            text = (row[0] if row else "") or ""
            parts = pieces(text)
            head = DOC_PREFIX + (name or "") + "\n"
            for k, (pos, piece) in enumerate(parts):
                pending.append((fid, k, pos, len(piece), head + piece))
            owners.append((fid, _sig(mtime, chars), len(parts)))
            if len(pending) >= batch_inputs or len(owners) >= COMMIT_EVERY:
                flush()
                if streak >= MAX_FAILED_BATCHES:
                    log("The AI engine keeps failing - stopping here. What "
                        "was done is kept; run again to carry on.")
                    conn.commit()
                    return 2
            if time.time() - last_emit > 0.5:
                last_emit = time.time()
                fs._emit(progress, done + failed, total, done, t0)
        flush()
        findex.set_meta(conn, "embed_model", MODEL)
        bump()
        conn.commit()
        fs._emit(progress, total, total, done, t0)
        log("Vectorised {:,} file(s) in {:.0f}s{}".format(
            done, time.time() - t0,
            " - {:,} failed (see above)".format(failed) if failed else ""))
        return 0
    finally:
        conn.close()


def forget(db, log=print):
    conn = findex.open_db(db)
    try:
        if have_tables(conn):
            conn.execute("DELETE FROM embeds")
            conn.execute("DELETE FROM embedded")
            findex.set_meta(conn, "embed_version",
                            str(int(findex.get_meta(conn, "embed_version")
                                    or 0) + 1))
            conn.commit()
            conn.execute("VACUUM")
        log("Vectors removed.")
    finally:
        conn.close()
    _CACHE.clear()


def status(conn, stale=True):
    """{'files': vectorised files, 'text_files': files with text, 'chunks',
    'model', 'bytes', 'stale'} - '' model when nothing has been made yet.
    stale=False skips the count of files still to do (a join over the
    whole files table)."""
    out = {"files": 0, "text_files": 0, "chunks": 0, "model": "",
           "bytes": 0, "stale": 0}
    try:
        out["text_files"] = conn.execute(
            "SELECT COUNT(*) FROM files WHERE chars > 0 AND is_dir = 0"
        ).fetchone()[0]
        if not have_tables(conn):
            return out
        out["files"] = conn.execute("SELECT COUNT(*) FROM embedded"
                                    ).fetchone()[0]
        out["chunks"] = conn.execute("SELECT COUNT(*) FROM embeds"
                                     ).fetchone()[0]
        out["model"] = findex.get_meta(conn, "embed_model") or ""
        dim = int(findex.get_meta(conn, "embed_dim") or 0)
        out["bytes"] = out["chunks"] * (dim + 40)
        if not stale:
            return out
        out["stale"] = conn.execute(
            "SELECT COUNT(*) FROM files f LEFT JOIN embedded e ON "
            "e.file_id = f.id WHERE f.chars > 0 AND f.is_dir = 0 AND "
            "(e.sig IS NULL OR e.sig != printf('%d:%d', "
            "CAST(f.mtime AS INTEGER), f.chars))").fetchone()[0]
    except Exception:                                          # noqa: BLE001
        pass
    return out


def status_line(conn):
    s = status(conn)
    if not s["model"] or not s["files"]:
        return "No meaning index yet{}".format(
            " - {:,} file(s) with text could be vectorised".format(
                s["text_files"]) if s["text_files"] else "")
    return "{:,} of {:,} file(s) with text vectorised ({}, {} pieces, {})" \
        "{}".format(s["files"], s["text_files"], s["model"], "{:,}".format(
            s["chunks"]), findex.human(s["bytes"]),
            " - {:,} new or changed since".format(s["stale"])
            if s["stale"] else "")


# ----------------------------------------------------------------------------
# Searching
# ----------------------------------------------------------------------------

# One matrix per database, in the process doing the searching (the desktop
# app): loaded the first time, kept while the vectors' version is unchanged.
_CACHE = {}


def _load(conn, db_key):
    np = _np()
    version = findex.get_meta(conn, "embed_version") or "0"
    hit = _CACHE.get(db_key)
    if hit and hit["version"] == version:
        return hit
    dim = int(findex.get_meta(conn, "embed_dim") or 0)
    n = conn.execute("SELECT COUNT(*) FROM embeds").fetchone()[0]
    if not n or not dim:
        data = {"version": version, "n": 0}
        _CACHE.clear()
        _CACHE[db_key] = data
        return data
    # straight from the cursor into preallocated arrays: the matrix is the
    # only big thing in memory, never a list of blobs beside it
    mat = np.empty((n, dim), dtype=np.int8)
    ids = np.empty(n, dtype=np.int64)
    pos = np.empty(n, dtype=np.int64)
    lens = np.empty(n, dtype=np.int32)
    scale = np.empty(n, dtype=np.float32)
    i = 0
    for fid, _chunk, p, ln, sc, blob in conn.execute(
            "SELECT file_id, chunk, pos, len, scale, vec FROM embeds "
            "ORDER BY file_id, chunk"):
        if i >= n:
            break               # a run added rows meanwhile; next reload
        mat[i] = np.frombuffer(blob, dtype=np.int8) if len(blob) == dim \
            else 0
        ids[i], pos[i], lens[i], scale[i] = fid, p, ln, sc
        i += 1
    if i < n:
        mat, ids, pos, lens, scale = (a[:i] for a in
                                      (mat, ids, pos, lens, scale))
        n = i
    data = {"version": version, "n": n, "mat": mat, "ids": ids, "pos": pos,
            "lens": lens, "scale": scale}
    _CACHE.clear()
    _CACHE[db_key] = data
    return data


_START_LOCK = threading.Lock()
_STARTED = []                   # urls this process has tried to start


def _start_once(url):
    """Start findex's own engine for a search - but only once per process
    and from one thread at a time: the app fires a search per keystroke,
    and each one must not launch another `ollama serve`."""
    key = (url or fs.AI_URL).rstrip("/")
    if not fs.find_ollama():
        return {"ok": False, "models": [], "error": "not installed"}
    if not _START_LOCK.acquire(blocking=False):
        raise EmbedError("the AI engine is starting - try again in a moment")
    try:
        if key in _STARTED:
            return fs.ai_status(url)
        _STARTED.append(key)
        return fs.ai_start(url, log=lambda *a: None)
    finally:
        _START_LOCK.release()


def query_vector(text, url=None):
    np = _np()
    v = np.asarray(embed_texts([QUERY_PREFIX + text], MODEL, url)[0],
                   dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


def search(conn, text, top=MEANING_TOP, url=None, db_key=None,
           min_score=MIN_SCORE, drop_below_best=DROP_BELOW_BEST):
    """The files whose text is closest in meaning to `text`:
    [(file_id, score, pos, len)] best first, one entry per file (its best
    piece). Raises EmbedError when there is nothing to search or the
    engine is not running."""
    np = _np()
    if not have_tables(conn):
        raise EmbedError("no meaning index yet - Index tab > Search by "
                         "meaning > Update now (or: findex embed)")
    data = _load(conn, db_key or "db")
    if not data["n"]:
        raise EmbedError("no meaning index yet - Index tab > Search by "
                         "meaning > Update now (or: findex embed)")
    st = fs.ai_status(url)
    if not st["ok"]:
        st = _start_once(url)
        if not st["ok"]:
            raise EmbedError("search by meaning needs the AI engine running "
                             "- Summary tab > AI summaries > Set up")
    q = query_vector(text, url)
    mat, scale = data["mat"], data["scale"]
    n = data["n"]
    scores = np.empty(n, dtype=np.float32)
    block = 4096            # rows scored at a time (keeps the float copy small)
    for s in range(0, n, block):
        e = min(n, s + block)
        scores[s:e] = (mat[s:e].astype(np.float32) @ q) * scale[s:e]
    want = min(n, max(top * 4, 64))
    cand = np.argpartition(-scores, want - 1)[:want] if want < n \
        else np.arange(n)
    cand = cand[np.argsort(-scores[cand])]
    out, seen = [], set()
    ids, pos, lens = data["ids"], data["pos"], data["lens"]
    floor = min_score
    if len(cand):
        # the engine's scores sit in a narrow band (unrelated text still
        # scores ~0.4), so the useful cut is relative to the best hit
        floor = max(floor, float(scores[cand[0]]) - drop_below_best)
    for i in cand:
        fid = int(ids[i])
        sc = float(scores[i])
        if sc < floor:
            break
        if fid in seen:
            continue
        seen.add(fid)
        out.append((fid, sc, int(pos[i]), int(lens[i])))
        if len(out) >= top:
            break
    return out


def snippet(conn, fid, pos, length, chars=SNIPPET_CHARS):
    """The start of the best-matching piece, for the preview pane."""
    row = conn.execute("SELECT substr(body, ?, ?) FROM docs WHERE rowid=?",
                       (pos + 1, min(length, chars), fid)).fetchone()
    text = " ".join(((row[0] if row else "") or "").split())
    return text + (" ..." if text and length > chars else "")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_embed(args):
    if args.status:
        conn = findex.open_db_ro(args.db)
        try:
            print(status_line(conn))
        finally:
            conn.close()
        st = fs.ai_status(args.ai_url)
        print("engine: {}; {}: {}".format(
            "running" if st["ok"] else "not running", MODEL,
            "installed" if fs.has_model(st["models"], MODEL) else
            "not installed" if st["ok"] else "unknown"))
        return 0
    if args.setup:
        return setup(args.ai_url, args.progress)
    if args.forget:
        forget(args.db)
        return 0
    if args.query:
        conn = findex.open_db_ro(args.db)
        try:
            hits = search(conn, args.query, args.limit or 20, args.ai_url)
            for fid, sc, pos, ln in hits:
                row = conn.execute("SELECT path FROM files WHERE id=?",
                                   (fid,)).fetchone()
                print("{:>4.0f}%  {}".format(sc * 100, row[0] if row else fid))
                print("       " + snippet(conn, fid, pos, ln, 140))
        except EmbedError as exc:
            print(exc)
            return 1
        finally:
            conn.close()
        return 0
    code = run(args.db, args.folder, args.ai_url, args.rebuild, args.progress)
    return 0 if code == 1 else code


def add_commands(sub):
    p = sub.add_parser(
        "embed", help="search by meaning: turn the indexed text into vectors "
                      "with a local embedding model, then  findex find "
                      "\"~what you mean\"")
    p.add_argument("folder", nargs="?", default="",
                   help="only files under this folder (none = everything)")
    p.add_argument("--rebuild", action="store_true",
                   help="remake every vector, not just missing/stale ones")
    p.add_argument("--status", action="store_true",
                   help="how much of the index is covered")
    p.add_argument("--setup", action="store_true",
                   help="fetch the AI engine (if findex has none) and the "
                        "embedding model {} ({})".format(MODEL, MODEL_SIZE))
    p.add_argument("--forget", action="store_true",
                   help="remove all the vectors")
    p.add_argument("-q", "--query", help="try a meaning search from here")
    p.add_argument("-n", "--limit", type=int, default=0)
    p.add_argument("--ai-url", default=None,
                   help="Ollama address (default {})".format(fs.AI_URL))
    p.add_argument("--progress", action="store_true",
                   help="emit @P progress lines (used by the desktop app)")
    p.set_defaults(func=cmd_embed)


if __name__ == "__main__":
    sys.exit(findex.main())
