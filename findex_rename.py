#!/usr/bin/env python3
"""
findex_rename - bulk renaming driven by a findex search, with a dry-run
preview first and an undo afterwards.

    findex rename "D:\\Photos ext:jpg" --find IMG_ --replace ""
    findex rename "D:\\Shared" --normalise
    findex rename "invoice ext:pdf" --date-prefix --apply
    findex rename --undo
    findex rename --history

The selection is an ordinary findex query (names, content:, C:\\ scopes,
ext:, !exclusions - see `findex find`), so anything you can search for you
can rename. Without --apply nothing is touched: the plan is printed with
each file's new name and whether it is safe. Collisions - two files that
would end up with the same name, or a name already taken - are skipped,
never overwritten. --apply renames the files on disk, updates the index
(so search is right immediately), records every rename in the journal and
in a batch that `--undo` reverses in exactly the opposite order.

Operations, applied in this order to each name:
    --find / --replace [--regex] [-i]   substring or regular-expression edit
    --case lower|upper|title|sentence   change the letter case (stem only)
    --normalise                         make the name safe everywhere: NFC
                                        Unicode, illegal characters -> _,
                                        single spaces, no leading/trailing
                                        spaces or dots, no reserved names
    --date-prefix [FORMAT]              prefix the modified date (2024-03-01 )
    --max-len N                         shorten the stem so the name fits
    --ext-lower                         .JPG -> .jpg

Folders are left alone unless --include-folders is given: renaming a folder
re-points every indexed path beneath it. Children are renamed before their
parents, and undone parents-first, so mixed selections stay consistent.
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata

import findex

RENAMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS renames (
    id       INTEGER PRIMARY KEY,
    batch    INTEGER NOT NULL,
    ts       REAL NOT NULL,
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    is_dir   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_renames_batch ON renames(batch);
"""

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} \
    | {"lpt%d" % i for i in range(1, 10)}
_WS = re.compile(r"\s+")
_SEPS = re.compile(r"[\\/]")


def ensure_schema(conn):
    conn.executescript(RENAMES_SCHEMA)


# ----------------------------------------------------------------------------
# Building new names
# ----------------------------------------------------------------------------

def _split(name, is_dir):
    """(stem, ext) - folders and dotfiles have no extension to protect."""
    if is_dir or name.startswith(".") and name.count(".") == 1:
        return name, ""
    stem, ext = os.path.splitext(name)
    return (stem, ext) if stem else (name, "")


def _title(text):
    out = []
    for word in re.split(r"(\s+|[_\-.]+)", text):
        if word and not re.fullmatch(r"(\s+|[_\-.]+)", word):
            word = word[0].upper() + word[1:].lower()
        out.append(word)
    return "".join(out)


def normalise_name(name):
    name = unicodedata.normalize("NFC", name)
    name = _ILLEGAL.sub("_", name)
    name = _WS.sub(" ", name)
    name = name.strip().rstrip(".").strip()
    stem = name.split(".")[0].lower() if "." in name else name.lower()
    if stem in _RESERVED:
        name = "_" + name
    return name


def new_name(name, ops, mtime=0, is_dir=False):
    """Apply the operations to one name. ops keys (all optional):
    find, replace, regex, ignore_case, case, normalise, date_prefix
    (a strftime format or True for %Y-%m-%d), max_len, ext_lower."""
    stem, ext = _split(name, is_dir)
    find = ops.get("find")
    if find:
        flags = re.IGNORECASE if ops.get("ignore_case") else 0
        pattern = find if ops.get("regex") else re.escape(find)
        repl = ops.get("replace") or ""
        if not ops.get("regex"):
            repl = repl.replace("\\", "\\\\")
        stem = re.sub(pattern, repl, stem, flags=flags)
    case = ops.get("case")
    if case == "lower":
        stem = stem.lower()
    elif case == "upper":
        stem = stem.upper()
    elif case == "title":
        stem = _title(stem)
    elif case == "sentence":
        stem = stem[:1].upper() + stem[1:].lower()
    if ops.get("ext_lower"):
        ext = ext.lower()
    if ops.get("date_prefix"):
        fmt = ops["date_prefix"]
        if fmt is True or not isinstance(fmt, str):
            fmt = "%Y-%m-%d"
        stamp = time.strftime(fmt, time.localtime(mtime or 0)) if mtime else ""
        if stamp and not stem.startswith(stamp):
            stem = stamp + " " + stem
    max_len = ops.get("max_len")
    if max_len and len(stem) + len(ext) > int(max_len):
        stem = stem[:max(1, int(max_len) - len(ext))].rstrip(" .")
    result = stem + ext
    if ops.get("normalise"):
        result = normalise_name(result)
    return result


def plan(rows, ops, conn=None):
    """rows: [(path, size, mtime, is_dir)] -> [(old, new, status, note)]
    status: ok / unchanged / collision / invalid / skipped.
    Children come before parents so a folder rename never invalidates a
    child's path mid-batch."""
    rows = sorted(rows, key=lambda r: (-r[0].count("\\") - r[0].count("/"),
                                       r[0]))
    out = []
    targets = {}
    have_index = conn is not None
    for path, size, mtime, is_dir in rows:
        if is_dir and not ops.get("include_folders"):
            out.append((path, path, "skipped", "folder (--include-folders)"))
            continue
        folder, name = os.path.split(path)
        try:
            new = new_name(name, ops, mtime, is_dir)
        except re.error as exc:
            out.append((path, path, "invalid", "bad pattern: {}".format(exc)))
            continue
        if new == name:
            out.append((path, path, "unchanged", ""))
            continue
        if not new or new in (".", "..") or _SEPS.search(new) \
                or (os.name == "nt" and _ILLEGAL.search(new)):
            out.append((path, path, "invalid", "not a valid name: {!r}".format(new)))
            continue
        new_path = os.path.join(folder, new)
        key = os.path.normcase(new_path)
        if key in targets:
            out.append((path, new_path, "collision",
                        "same target as " + os.path.basename(targets[key])))
            continue
        case_only = os.path.normcase(path) == key
        if not case_only:
            exists = os.path.exists(findex.lp(new_path))
            if not exists and have_index:
                exists = conn.execute("SELECT 1 FROM files WHERE path=?",
                                      (new_path,)).fetchone() is not None
            if exists:
                out.append((path, new_path, "collision", "name already taken"))
                continue
        targets[key] = path
        out.append((path, new_path, "ok", ""))
    return out


def select_rows(conn, query, exts=None, kind=None, under=None):
    """The files a findex query names: [(path, size, mtime, is_dir)]."""
    rows = findex.query_rows(conn, query or "", 0, exts=exts, kind=kind)
    out = [(r[0], r[1], r[2], bool(r[4])) for r in rows]
    if under:
        u = os.path.normcase(os.path.abspath(under)).rstrip("\\/") + os.sep
        out = [r for r in out if os.path.normcase(r[0]).startswith(u)]
    return out


# ----------------------------------------------------------------------------
# Applying and undoing
# ----------------------------------------------------------------------------

def _rename_in_index(cur, old, new, is_dir):
    name = os.path.basename(new)
    ext = "" if is_dir else os.path.splitext(name)[1].lower()
    cur.execute("UPDATE files SET path=?, name=?, ext=? WHERE path=?",
                (new, name, ext, old))
    if is_dir:
        sep = "\\" if "\\" in old or (len(old) > 1 and old[1] == ":") else "/"
        like = findex.like_escape(old.rstrip("\\/")) + sep + "%"
        cur.execute("UPDATE files SET path = ? || substr(path, ?) "
                    "WHERE path LIKE ? ESCAPE '!'",
                    (new, len(old) + 1, like))


def apply(conn, planned, log=None):
    """Rename every 'ok' row on disk and in the index, in one batch.
    Returns (batch_id, done, [(old, error)])."""
    ensure_schema(conn)
    batch = (conn.execute("SELECT COALESCE(MAX(batch),0)+1 FROM renames")
             .fetchone()[0])
    done, failed = 0, []
    cur = conn.cursor()
    cur.execute("BEGIN")
    now = time.time()
    for old, new, status, note in planned:
        if status != "ok":
            continue
        is_dir = os.path.isdir(findex.lp(old))
        try:
            if (os.path.normcase(old) != os.path.normcase(new)
                    and os.path.exists(findex.lp(new))):
                raise FileExistsError("target appeared: " + new)
            os.rename(findex.lp(old), findex.lp(new))
        except OSError as exc:
            failed.append((old, str(exc)))
            if log:
                log("rename failed: {} -> {}: {}".format(old, new, exc))
            continue
        _rename_in_index(cur, old, new, is_dir)
        cur.execute("INSERT INTO renames(batch, ts, old_path, new_path, is_dir) "
                    "VALUES (?, ?, ?, ?, ?)", (batch, now, old, new, int(is_dir)))
        size = None
        if not is_dir:
            try:
                size = os.path.getsize(findex.lp(new))
            except OSError:
                pass
        findex.journal_add(cur, [(now, "renamed", new, old, size, int(is_dir),
                                  "rename")])
        done += 1
        if done % 200 == 0:
            conn.commit()
            cur.execute("BEGIN")
    conn.commit()
    return batch, done, failed


def batches(conn):
    """[(batch, ts, count)] newest first."""
    ensure_schema(conn)
    return conn.execute(
        "SELECT batch, MIN(ts), COUNT(*) FROM renames GROUP BY batch "
        "ORDER BY batch DESC").fetchall()


def undo(conn, batch=None, log=None):
    """Reverse a batch (the latest by default) in the opposite order.
    Returns (batch, done, [(path, error)]). A file that has since moved on
    or been deleted is reported, not guessed at."""
    ensure_schema(conn)
    if batch is None:
        row = conn.execute("SELECT MAX(batch) FROM renames").fetchone()
        batch = row[0] if row else None
    if not batch:
        return None, 0, []
    rows = conn.execute("SELECT id, old_path, new_path, is_dir FROM renames "
                        "WHERE batch=? ORDER BY id DESC", (batch,)).fetchall()
    done, failed = 0, []
    cur = conn.cursor()
    cur.execute("BEGIN")
    now = time.time()
    for rid, old, new, is_dir in rows:
        try:
            if not os.path.exists(findex.lp(new)) and not os.path.lexists(
                    findex.lp(new)):
                raise FileNotFoundError("no longer at " + new)
            if (os.path.normcase(old) != os.path.normcase(new)
                    and os.path.exists(findex.lp(old))):
                raise FileExistsError("original name is taken again: " + old)
            os.rename(findex.lp(new), findex.lp(old))
        except OSError as exc:
            failed.append((new, str(exc)))
            if log:
                log("undo failed: {}: {}".format(new, exc))
            continue
        _rename_in_index(cur, new, old, is_dir)
        findex.journal_add(cur, [(now, "renamed", old, new, None, is_dir,
                                  "undo")])
        cur.execute("DELETE FROM renames WHERE id=?", (rid,))
        done += 1
    conn.commit()
    return batch, done, failed


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _ops_from_args(args):
    ops = {"find": args.find, "replace": args.replace, "regex": args.regex,
           "ignore_case": args.ignore_case, "case": args.case,
           "normalise": args.normalise, "max_len": args.max_len,
           "ext_lower": args.ext_lower,
           "include_folders": args.include_folders}
    if args.date_prefix is not None:
        ops["date_prefix"] = args.date_prefix or True
    return ops


def cmd_rename(args):
    if args.history:
        conn = findex.open_db_ro(args.db)
        try:                        # read-only: the table may not exist yet
            rows = conn.execute(
                "SELECT batch, MIN(ts), COUNT(*) FROM renames GROUP BY batch "
                "ORDER BY batch DESC").fetchall()
        except Exception:                                      # noqa: BLE001
            rows = []
        conn.close()
        if not rows:
            print("No renames recorded.")
            return 0
        for b, ts, n in rows:
            print("batch {:>4}  {}  {:,} file(s)".format(
                b, time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), n))
        print("\nfindex rename --undo [BATCH]  reverses one (default: latest)")
        return 0
    if args.undo is not None:
        conn = findex.open_db(args.db)
        batch, done, failed = undo(conn, args.undo or None, log=print)
        conn.close()
        if batch is None:
            print("Nothing to undo.")
            return 0
        print("Undid batch {}: {:,} rename(s) reversed{}".format(
            batch, done, ", {:,} failed".format(len(failed)) if failed else ""))
        return 1 if failed else 0

    ops = _ops_from_args(args)
    if not any(ops.get(k) for k in ("find", "case", "normalise", "date_prefix",
                                    "max_len", "ext_lower")):
        sys.stderr.write("Nothing to do: give --find/--replace, --case, "
                         "--normalise, --date-prefix, --max-len or "
                         "--ext-lower.\n")
        return 2
    conn = findex.open_db(args.db) if args.apply else findex.open_db_ro(args.db)
    rows = select_rows(conn, args.query, exts=args.ext, under=args.under)
    planned = plan(rows, ops, conn)
    counts = {}
    for old, new, status, note in planned:
        counts[status] = counts.get(status, 0) + 1
    shown = 0
    for old, new, status, note in planned:
        if status == "unchanged":
            continue
        if args.limit and shown >= args.limit:
            break
        shown += 1
        if status == "ok":
            print("  {}\n    -> {}".format(old, os.path.basename(new)))
        else:
            print("  {}\n    {}: {}".format(old, status.upper(), note))
    print("\n{:,} selected: {:,} to rename, {:,} unchanged, {:,} collision(s), "
          "{:,} invalid, {:,} skipped".format(
              len(planned), counts.get("ok", 0), counts.get("unchanged", 0),
              counts.get("collision", 0), counts.get("invalid", 0),
              counts.get("skipped", 0)))
    if not args.apply:
        print("Dry run - add --apply to rename. Collisions are never overwritten.")
        conn.close()
        return 0
    batch, done, failed = apply(conn, planned, log=print)
    conn.close()
    print("Renamed {:,} file(s) in batch {}{}.  Undo with:  findex rename "
          "--undo {}".format(done, batch,
                              ", {:,} failed".format(len(failed))
                              if failed else "", batch))
    return 1 if failed else 0


def add_commands(sub):
    p = sub.add_parser("rename", help="bulk rename the results of a search - "
                       "dry run by default, --apply to do it, --undo to reverse")
    p.add_argument("query", nargs="?", default="",
                   help="a findex search naming the files (see 'find')")
    p.add_argument("--under", metavar="FOLDER", help="only beneath this folder")
    p.add_argument("-e", "--ext", nargs="+", help="restrict to extensions")
    p.add_argument("--find", help="text (or --regex pattern) to replace")
    p.add_argument("--replace", default="", help="its replacement")
    p.add_argument("--regex", action="store_true",
                   help="--find is a regular expression (\\1 groups work)")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("--case", choices=("lower", "upper", "title", "sentence"))
    p.add_argument("--normalise", "--normalize", action="store_true",
                   help="safe everywhere: NFC, no illegal chars, single "
                        "spaces, no trailing dots/spaces, no reserved names")
    p.add_argument("--date-prefix", nargs="?", const="%Y-%m-%d", metavar="FMT",
                   help="prefix the modified date (default 2024-03-01 )")
    p.add_argument("--max-len", type=int, metavar="N",
                   help="shorten names longer than N characters")
    p.add_argument("--ext-lower", action="store_true", help=".JPG -> .jpg")
    p.add_argument("--include-folders", action="store_true",
                   help="rename folders in the selection too")
    p.add_argument("--apply", action="store_true",
                   help="actually rename (default is a dry run)")
    p.add_argument("-n", "--limit", type=int, default=200,
                   help="rows of the plan to print (default 200)")
    p.add_argument("--undo", nargs="?", const=0, type=int, metavar="BATCH",
                   help="reverse the latest batch (or the one given)")
    p.add_argument("--history", action="store_true", help="list batches")
    p.set_defaults(func=cmd_rename)


if __name__ == "__main__":
    sys.exit(findex.main())
