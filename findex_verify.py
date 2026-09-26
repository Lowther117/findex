#!/usr/bin/env python3
"""
findex_verify - snapshots of a tree, and proof that a copy or a later state
matches it.

    findex snapshot -o before.fxsnap --under D:\\Projects [--hash]
        Write a manifest of everything the index holds under a folder:
        relative path, type, size, timestamp and (when known) content hash.
        --hash reads every file first so every entry carries a full hash.

    findex verify before.fxsnap
        Compare the snapshot with what the index holds NOW at the same
        place: what was added, what is missing, what changed.

    findex verify before.fxsnap --folder E:\\Projects-copy
        Compare it with a COPY at another location, using the index's rows
        under that folder. Add --disk to walk (and hash) the copy directly
        instead - for a USB drive or a share that is not indexed.

    findex verify before.fxsnap --against after.fxsnap
        Two snapshots against each other.

Moved or renamed files are recognised by content hash when both sides
have one ("moved"), otherwise by name + size + timestamp ("likely moved").
A file whose hash changed is "changed"; one whose bytes are identical but
whose timestamp differs is "touched". Exit status is 0 when nothing
differs, 1 when something does - handy at the end of a migration script.

Snapshot files are gzip-compressed text: one JSON header line, then one
tab-separated line per entry with the path JSON-encoded (so any character
a filesystem allows survives). A .tsv or .txt name writes them uncompressed.
"""

from __future__ import annotations

import csv
import gzip
import re
import html
import json
import os
import socket
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import findex

try:
    import findex_hash
except ImportError:
    findex_hash = None

FORMAT = 1
MTIME_TOLERANCE = 2.0       # FAT/exFAT keep 2-second timestamps


# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

def _sep_for(path):
    return "\\" if "\\" in path or (len(path) > 1 and path[1] == ":") else "/"


_ABS = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/)")


def _absish(path):
    """abspath, unless the path is already absolute in Windows OR POSIX
    form - os.path.abspath on a Mac would otherwise 'fix' D:\\Shared into
    /Users/.../D:\\Shared."""
    return path if _ABS.match(path) else os.path.abspath(path)


def _norm(rel):
    """Relative paths are compared with '/' separators whatever OS made
    them, so a Windows snapshot verifies against a Mac copy."""
    return rel.replace("\\", "/").strip("/")


def _relative(path, root):
    if not root:
        return path
    r = root.rstrip("\\/")
    if len(path) > len(r) and path[:len(r)] == r and path[len(r)] in "\\/":
        return _norm(path[len(r) + 1:])
    if path == r:
        return ""
    return None


# ----------------------------------------------------------------------------
# Snapshot
# ----------------------------------------------------------------------------

def _open_out(path, mode):
    if path.lower().endswith((".tsv", ".txt")):
        return open(path, mode + "t", encoding="utf-8", newline="")
    return gzip.open(path, mode + "t", encoding="utf-8", newline="")


def _open_in(path):
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def write_snapshot(conn, out_path, under=None):
    """Write the index's rows (under a folder, or all) as a snapshot.
    Returns (files, folders, hashed)."""
    where, params = "", []
    if under:
        under = _absish(under)
        where = " WHERE path = ? OR path LIKE ? ESCAPE '!'"
        params = [under, findex.like_escape(under.rstrip("\\/"))
                  + _sep_for(under) + "%"]
    have = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    hcol = "fhash" if "fhash" in have else "NULL"
    files = folders = hashed = 0
    with _open_out(out_path, "w") as out:
        head = {"findex-snapshot": FORMAT, "created": time.time(),
                "under": under, "host": socket.gethostname(),
                "index": os.path.abspath(findex.DEFAULT_DB)}
        out.write(json.dumps(head, ensure_ascii=False) + "\n")
        for path, is_dir, size, mtime, fh in conn.execute(
                "SELECT path, is_dir, size, mtime, {} FROM files{} "
                "ORDER BY path".format(hcol, where), params):
            rel = _relative(path, under) if under else path
            if rel is None or rel == "":
                continue
            if is_dir:
                folders += 1
            else:
                files += 1
                if fh:
                    hashed += 1
            out.write("{}\t{}\t{}\t{}\t{}\n".format(
                json.dumps(rel, ensure_ascii=False), 1 if is_dir else 0,
                size or 0, "{:.3f}".format(mtime or 0), fh or ""))
    return files, folders, hashed


def read_snapshot(path):
    """-> (header dict, {rel_or_abs_path: (is_dir, size, mtime, fhash)})"""
    entries = {}
    with _open_in(path) as fh:
        first = fh.readline()
        try:
            head = json.loads(first)
            if "findex-snapshot" not in head:
                raise ValueError
        except ValueError:
            raise ValueError("{} is not a findex snapshot".format(path))
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            p, is_dir, size, mtime, fhash = line.split("\t")
            entries[json.loads(p)] = (int(is_dir), int(size), float(mtime),
                                      fhash or None)
    return head, entries


# ----------------------------------------------------------------------------
# The other side of the comparison
# ----------------------------------------------------------------------------

def entries_from_index(conn, under=None):
    where, params = "", []
    if under:
        under = _absish(under)
        where = " WHERE path LIKE ? ESCAPE '!'"
        params = [findex.like_escape(under.rstrip("\\/")) + _sep_for(under) + "%"]
    have = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    hcol = "fhash" if "fhash" in have else "NULL"
    out = {}
    for path, is_dir, size, mtime, fh in conn.execute(
            "SELECT path, is_dir, size, mtime, {} FROM files{}".format(
                hcol, where), params):
        key = _relative(path, under) if under else path
        if key:
            out[key] = (is_dir, size or 0, mtime or 0, fh)
    return out


def entries_from_disk(folder, want_hash=False, workers=None, progress=None):
    """Walk a folder right now (no index involved) - for a copy on a drive
    that is not indexed. want_hash reads every file to hash it."""
    folder = os.path.abspath(folder)
    out = {}
    jobs = []
    n = 0
    for path, name, ext, size, mtime, cloud, is_dir in findex.walk([folder]):
        rel = _relative(path, folder)
        if not rel:
            continue
        out[rel] = (int(is_dir), size, mtime, None)
        n += 1
        if progress and n % 5000 == 0:
            progress("walking", n, 0)
        if want_hash and not is_dir and size > 0:
            jobs.append((rel, path, size, True, True))
    if jobs and findex_hash is not None:
        workers = workers or min(8, max(2, (os.cpu_count() or 4) // 2))
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for rel, phash, fhash, kind, err in ex.map(
                    findex_hash.hash_file, jobs, chunksize=16):
                done += 1
                if not err and fhash:
                    d, s, m, _ = out[rel]
                    out[rel] = (d, s, m, fhash)
                if progress and done % 500 == 0:
                    progress("hashing", done, len(jobs))
    return out


# ----------------------------------------------------------------------------
# Compare
# ----------------------------------------------------------------------------

def compare(a, b):
    """a = the snapshot (what was), b = what is. Returns a dict of lists:
        missing   [(path, size)]           in a, not in b
        added     [(path, size)]           in b, not in a
        changed   [(path, size_a, size_b, why)]
        touched   [(path,)]                same bytes, different timestamp
        moved     [(old, new, size)]       identical content, new place
        likely    [(old, new, size)]       same name+size+time, new place
        same      int
    Folders take part in missing/added only."""
    res = {"missing": [], "added": [], "changed": [], "touched": [],
           "moved": [], "likely": [], "same": 0, "folders_missing": [],
           "folders_added": []}
    for key, (d, size, mtime, fh) in a.items():
        other = b.get(key)
        if other is None:
            (res["folders_missing"] if d else res["missing"]).append(
                (key, size) if not d else (key,))
            continue
        d2, size2, mtime2, fh2 = other
        if d or d2:
            if d != d2:
                res["changed"].append((key, size, size2,
                                       "folder became file" if d
                                       else "file became folder"))
            continue
        if size == 0 and size2 == 0:       # two empty files ARE identical
            if abs(mtime - mtime2) > MTIME_TOLERANCE:
                res["touched"].append((key,))
            else:
                res["same"] += 1
        elif fh and fh2:
            if fh != fh2:
                res["changed"].append((key, size, size2, "content differs"))
            elif abs(mtime - mtime2) > MTIME_TOLERANCE:
                res["touched"].append((key,))
            else:
                res["same"] += 1
        elif size != size2:
            res["changed"].append((key, size, size2, "size differs"))
        elif abs(mtime - mtime2) > MTIME_TOLERANCE:
            res["changed"].append((key, size, size2, "timestamp differs "
                                   "(no hashes to compare content)"))
        else:
            res["same"] += 1
    for key, (d, size, mtime, fh) in b.items():
        if key not in a:
            (res["folders_added"] if d else res["added"]).append(
                (key, size) if not d else (key,))

    # Moves: pair up missing and added entries.
    by_hash = {}
    for key, size in res["added"]:
        fh = b[key][3]
        if fh:
            by_hash.setdefault(fh, []).append(key)
    by_sig = {}
    for key, size in res["added"]:
        d, s, m, fh = b[key]
        by_sig.setdefault((os.path.basename(key.replace("\\", "/")), s,
                           round(m)), []).append(key)
    still_missing, taken = [], set()
    for key, size in res["missing"]:
        d, s, m, fh = a[key]
        cands = by_hash.get(fh, []) if fh else []
        cands = [c for c in cands if c not in taken]
        if cands:
            new = cands.pop(0)
            taken.add(new)
            res["moved"].append((key, new, size))
            continue
        sig = (os.path.basename(key.replace("\\", "/")), s, round(m))
        cands = [c for c in by_sig.get(sig, []) if c not in taken]
        if cands:
            new = cands.pop(0)
            taken.add(new)
            res["likely"].append((key, new, size))
            continue
        still_missing.append((key, size))
    res["missing"] = still_missing
    res["added"] = [(k, s) for k, s in res["added"] if k not in taken]
    return res


def differences(res):
    return (len(res["missing"]) + len(res["added"]) + len(res["changed"])
            + len(res["moved"]) + len(res["likely"])
            + len(res["folders_missing"]) + len(res["folders_added"]))


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

SECTIONS = (("changed", "Changed"), ("missing", "Missing"), ("added", "Added"),
            ("moved", "Moved (identical content)"),
            ("likely", "Likely moved (same name, size and time)"),
            ("touched", "Touched (same bytes, new timestamp)"),
            ("folders_missing", "Folders missing"),
            ("folders_added", "Folders added"))


def summary_lines(res, label_a, label_b):
    out = ["verify: {}  ->  {}".format(label_a, label_b), ""]
    out.append("  {:<44} {:>10,}".format("unchanged files", res["same"]))
    for key, label in SECTIONS:
        out.append("  {:<44} {:>10,}".format(label.lower(), len(res[key])))
    out.append("")
    out.append("RESULT: {}".format(
        "identical" if not differences(res) else
        "{:,} difference(s)".format(differences(res))))
    return out


def _fmt_row(key, row):
    if key == "changed":
        p, s1, s2, why = row
        return "{}    [{}: {} -> {}]".format(p, why, findex.human(s1),
                                             findex.human(s2))
    if key in ("moved", "likely"):
        old, new, size = row
        return "{}  ->  {}    [{}]".format(old, new, findex.human(size))
    if key in ("missing", "added"):
        return "{}    [{}]".format(row[0], findex.human(row[1]))
    return row[0]


def write_text(res, out, label_a, label_b, cap=5000):
    out.write("\n".join(summary_lines(res, label_a, label_b)) + "\n")
    for key, label in SECTIONS:
        rows = res[key]
        if not rows:
            continue
        out.write("\n{} ({:,})\n".format(label, len(rows)))
        for row in rows[:cap]:
            out.write("  " + _fmt_row(key, row) + "\n")
        if len(rows) > cap:
            out.write("  ... {:,} more\n".format(len(rows) - cap))


def write_csv(res, out):
    w = csv.writer(out)
    w.writerow(["kind", "path", "new_path", "size_before", "size_after",
                "detail"])
    for key, label in SECTIONS:
        for row in res[key]:
            if key == "changed":
                w.writerow([key, row[0], "", row[1], row[2], row[3]])
            elif key in ("moved", "likely"):
                w.writerow([key, row[0], row[1], row[2], row[2], ""])
            elif key in ("missing", "added"):
                w.writerow([key, row[0], "", row[1], row[1], ""])
            else:
                w.writerow([key, row[0], "", "", "", ""])


def write_json(res, out, label_a, label_b):
    json.dump({"from": label_a, "to": label_b, "same": res["same"],
               "differences": differences(res),
               **{k: res[k] for k, _ in SECTIONS}}, out, indent=1,
              ensure_ascii=False)


def write_html(res, out, label_a, label_b, cap=2000):
    e = html.escape
    w = out.write
    w("<!doctype html><html><head><meta charset='utf-8'><title>findex verify"
      "</title><style>body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,"
      "Arial,sans-serif;margin:0;background:#f4f6f9;color:#1b1f27}main{max-"
      "width:1180px;margin:0 auto;padding:28px 20px 60px}h1{font-size:22px;"
      "margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px}.sub{color:#5a6475}"
      ".cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,"
      "1fr));gap:10px;margin:18px 0}.card{background:#fff;border:1px solid "
      "#d9dee7;border-radius:8px;padding:12px}.card b{display:block;font-size:"
      "22px}.card span{color:#5a6475;font-size:12px}.bad b{color:#b42318}.ok b"
      "{color:#146c2e}table{border-collapse:collapse;width:100%;background:#fff;"
      "border:1px solid #d9dee7;font-size:13px}td,th{padding:6px 10px;text-"
      "align:left;border-bottom:1px solid #eef1f5}th{background:#eef1f5}td.p{"
      "font-family:Consolas,Menlo,monospace;font-size:12px;word-break:break-"
      "all}@media (prefers-color-scheme:dark){body{background:#14161b;color:"
      "#e9ebf0}.card,table{background:#1c1f27;border-color:#333b49}th{"
      "background:#232834}td,th{border-color:#262b36}.sub,.card span{color:"
      "#98a2b3}}</style></head><body><main>")
    verdict = ("identical" if not differences(res)
               else "{:,} differences".format(differences(res)))
    w("<h1>Verification: {}</h1><div class='sub'>{} &rarr; {} &middot; {}"
      "</div>".format(e(verdict), e(label_a), e(label_b),
                      time.strftime("%d %b %Y %H:%M")))
    w("<div class='cards'><div class='card ok'><b>{:,}</b><span>unchanged"
      "</span></div>".format(res["same"]))
    for key, label in SECTIONS:
        n = len(res[key])
        cls = "bad" if n and key not in ("touched",) else ""
        w("<div class='card {}'><b>{:,}</b><span>{}</span></div>".format(
            cls, n, e(label)))
    w("</div>")
    for key, label in SECTIONS:
        rows = res[key]
        if not rows:
            continue
        w("<h2>{} <small>({:,})</small></h2><table>".format(e(label), len(rows)))
        if key == "changed":
            w("<tr><th>Path</th><th>Before</th><th>After</th><th>Why</th></tr>")
            for p, s1, s2, why in rows[:cap]:
                w("<tr><td class='p'>{}</td><td>{}</td><td>{}</td><td>{}</td>"
                  "</tr>".format(e(p), findex.human(s1), findex.human(s2), e(why)))
        elif key in ("moved", "likely"):
            w("<tr><th>Was</th><th>Now</th><th>Size</th></tr>")
            for old, new, size in rows[:cap]:
                w("<tr><td class='p'>{}</td><td class='p'>{}</td><td>{}</td>"
                  "</tr>".format(e(old), e(new), findex.human(size)))
        else:
            w("<tr><th>Path</th><th>Size</th></tr>")
            for row in rows[:cap]:
                w("<tr><td class='p'>{}</td><td>{}</td></tr>".format(
                    e(row[0]), findex.human(row[1]) if len(row) > 1 else ""))
        w("</table>")
        if len(rows) > cap:
            w("<p class='sub'>{:,} more not shown - export CSV for all.</p>"
              .format(len(rows) - cap))
    w("</main></body></html>")


def export(res, out_path, label_a, label_b, fmt=None):
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip(".").lower()
           or "txt").lower()
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        if fmt in ("html", "htm"):
            write_html(res, out, label_a, label_b)
        elif fmt == "csv":
            write_csv(res, out)
        elif fmt == "json":
            write_json(res, out, label_a, label_b)
        else:
            write_text(res, out, label_a, label_b)
    return fmt


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_snapshot(args):
    conn = findex.open_db(args.db)
    if args.hash:
        if findex_hash is None:
            sys.stderr.write("findex_hash.py is missing - cannot --hash\n")
            return 2
        print("Hashing every file{} first...".format(
            " under " + args.under if args.under else ""), flush=True)
        findex_hash.run_hash(conn, "full", under=args.under,
                             progress=args.progress)
    out = args.out or os.path.join(
        findex.downloads_dir(), "findex-snapshot-{}.fxsnap".format(
            time.strftime("%Y-%m-%d-%H%M")))
    files, folders, hashed = write_snapshot(conn, out, args.under)
    conn.close()
    print("Wrote {}\n  {:,} files ({:,} with content hashes), {:,} folders"
          .format(out, files, hashed, folders))
    if hashed < files and not args.hash:
        print("  tip: --hash gives every file a content hash, so verify can "
              "prove bytes, not just sizes and dates")
    return 0


def cmd_verify(args):
    try:
        head, a = read_snapshot(args.snapshot)
    except (OSError, ValueError) as exc:
        sys.stderr.write("{}\n".format(exc))
        return 2
    label_a = "{} ({}{})".format(
        os.path.basename(args.snapshot),
        head.get("under") or "whole index",
        ", " + time.strftime("%Y-%m-%d", time.localtime(head["created"]))
        if head.get("created") else "")
    snap_hashed = any(v[3] for v in a.values())

    if args.against:
        try:
            head2, b = read_snapshot(args.against)
        except (OSError, ValueError) as exc:
            sys.stderr.write("{}\n".format(exc))
            return 2
        label_b = "{} ({})".format(os.path.basename(args.against),
                                   head2.get("under") or "whole index")
    elif args.folder and args.disk:
        def prog(stage, n, total):
            if args.progress:
                print("@P seen={} total={} stage={}".format(n, total, stage),
                      flush=True)
            else:
                print("  {} {:,}{}".format(stage, n,
                                           "/{:,}".format(total) if total else ""),
                      flush=True)
        want_hash = snap_hashed and not args.no_hash
        print("Walking {}{}...".format(args.folder,
                                       " and hashing" if want_hash else ""),
              flush=True)
        b = entries_from_disk(args.folder, want_hash=want_hash,
                              workers=args.workers, progress=prog)
        label_b = args.folder + " (on disk)"
    else:
        if head.get("under") is None and args.folder:
            sys.stderr.write("The snapshot holds absolute paths (made without "
                             "--under); --folder cannot rebase it.\n")
            return 2
        # relative snapshot: compare with the rows under the same folder (or
        # the copy's folder); absolute snapshot: with the whole index
        base = (args.folder or head.get("under")) if head.get("under") else None
        if args.hash and snap_hashed and findex_hash is not None:
            conn = findex.open_db(args.db)
            print("Hashing files under {} first...".format(base or "the index"),
                  flush=True)
            findex_hash.run_hash(conn, "full", under=base,
                                 progress=args.progress)
            conn.close()
        conn = findex.open_db_ro(args.db)
        b = entries_from_index(conn, base)
        conn.close()
        label_b = "index now ({})".format(base or "whole index")

    res = compare(a, b)
    if args.out:
        fmt = export(res, args.out, label_a, label_b, args.format)
        print("Wrote {} ({})".format(args.out, fmt))
    print("\n".join(summary_lines(res, label_a, label_b)))
    if not args.out:
        cap = args.limit
        for key, label in SECTIONS:
            rows = res[key]
            if not rows:
                continue
            print("\n{} ({:,})".format(label, len(rows)))
            for row in rows[:cap]:
                print("  " + _fmt_row(key, row))
            if len(rows) > cap:
                print("  ... {:,} more (-n for more, or -o for a file)".format(
                    len(rows) - cap))
    return 1 if differences(res) else 0


def add_commands(sub):
    p = sub.add_parser("snapshot", help="write a manifest of the index (or "
                       "one folder of it) to verify against later")
    p.add_argument("-o", "--out", help="file to write (default Downloads/"
                   "findex-snapshot-DATE.fxsnap; .tsv/.txt = uncompressed)")
    p.add_argument("--under", metavar="FOLDER",
                   help="only this folder; paths are stored relative to it, "
                        "so a copy elsewhere can be verified against it")
    p.add_argument("--hash", action="store_true",
                   help="hash every file first so the snapshot proves content")
    p.add_argument("--progress", action="store_true")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("verify", help="compare a snapshot with the index "
                       "now, a copy elsewhere, or another snapshot")
    p.add_argument("snapshot", help="the .fxsnap to start from")
    p.add_argument("--against", metavar="SNAPSHOT",
                   help="compare with this second snapshot")
    p.add_argument("--folder", metavar="FOLDER",
                   help="compare with the copy at this folder (index rows, "
                        "or the disk itself with --disk)")
    p.add_argument("--disk", action="store_true",
                   help="walk --folder directly instead of using the index")
    p.add_argument("--hash", action="store_true",
                   help="index mode: hash the current files first, so a "
                        "hashed snapshot is compared by content")
    p.add_argument("--no-hash", action="store_true",
                   help="disk mode: do not hash the copy, compare size+time")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("-o", "--out", help="write the result (.html/.txt/.csv/.json)")
    p.add_argument("-f", "--format", choices=("html", "txt", "csv", "json"))
    p.add_argument("-n", "--limit", type=int, default=50,
                   help="rows printed per section (default 50)")
    p.add_argument("--progress", action="store_true")
    p.set_defaults(func=cmd_verify)


if __name__ == "__main__":
    sys.exit(findex.main())
