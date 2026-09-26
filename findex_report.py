#!/usr/bin/env python3
"""
findex_report - the health of a file tree, read straight from the index.

    findex report                     print the summary
    findex report -o health.html      full report as a self-contained web page
    findex report -o health.csv       every finding, one row each
    findex report --under D:\\Shared   one part of the index
    findex report --list bad-names    print one category in full

Nothing on disk is opened: every figure comes from the `files` table, so a
report over 500,000 files takes seconds. Type mismatches and unreadable
files need a `findex hash --all` pass first (that reads 16 KB of each
file); the report says so when those columns are empty.

Categories (also the Health tab in the desktop app):

    empty-folders   folders with nothing indexed beneath them
    zero-byte       files of 0 bytes
    long-paths      paths at or past the Windows limit (260 characters)
    bad-names       names Windows refuses or mangles: < > : " | ? *, control
                    characters, trailing spaces or dots, reserved names
                    (CON, NUL, COM1...), non-normalised Unicode (macOS-style)
    case-clashes    two names in one folder differing only by case - fine on
                    Linux/macOS, a collision on Windows, OneDrive, SharePoint
    leftovers       temp and lock files: ~$doc.docx, *.tmp, Thumbs.db,
                    .DS_Store, *.crdownload, *.part, desktop.ini...
    stale           files untouched for years (default 3)
    largest         the biggest files
    deepest         the deepest paths
    unreadable      files findex could not open, or whose text extraction
                    failed - corrupt, locked, or not what their name says
    mismatch        name says one type, bytes say another (after hash --all)
    secrets         passwords, keys and tokens found in extracted text
                    (findex_secrets.py)
"""

from __future__ import annotations

import csv
import heapq
import html
import json
import os
import re
import sys
import time
import unicodedata

import findex

try:
    import findex_hash
except ImportError:                       # module removed: no type checks
    findex_hash = None

MAX_ROWS = 20000          # rows kept per category (the count is always exact)
LONG_PATH = 260           # Windows MAX_PATH, including the terminating NUL
STALE_YEARS = 3.0
TOP_N = 200               # largest / deepest kept

CATEGORIES = (
    ("empty-folders", "Empty folders"),
    ("zero-byte", "Zero-byte files"),
    ("long-paths", "Paths over the Windows limit"),
    ("bad-names", "Names Windows refuses"),
    ("case-clashes", "Case clashes"),
    ("leftovers", "Temp and lock files"),
    ("stale", "Stale files"),
    ("largest", "Largest files"),
    ("deepest", "Deepest paths"),
    ("unreadable", "Unreadable or corrupt"),
    ("mismatch", "Type does not match name"),
    ("secrets", "Possible secrets in files"),
)
LABEL = dict(CATEGORIES)

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
_RESERVED |= {"com%d" % i for i in range(1, 10)}
_RESERVED |= {"lpt%d" % i for i in range(1, 10)}
_LEFTOVER_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", "ehthumbs.db",
                   "ehthumbs_vista.db", "~$", ".~lock", "._.ds_store"}
_LEFTOVER_EXTS = {".tmp", ".temp", ".crdownload", ".part", ".partial",
                  ".download", ".swp", ".swo", ".lock", ".lck", ".~tmp"}
_LEFTOVER_RE = re.compile(r"^(~\$|\.~lock\.|~wr[ldsf]\d|\._)", re.I)


def _sep_for(path):
    return "\\" if "\\" in path or (len(path) > 1 and path[1] == ":") else "/"


def split_path(path):
    """(folder, name) whichever separator the path uses - an index built on
    Windows can be opened on a Mac and the other way round, and os.path
    would then split on the wrong character."""
    i = max(path.rfind("\\"), path.rfind("/"))
    if i < 0:
        return "", path
    return path[:i] or path[:i + 1], path[i + 1:]


def _depth(path):
    return len([p for p in re.split(r"[\\/]+", path) if p])


def _top(path):
    """The root-ish part a finding belongs to: drive or first folder."""
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    if not parts:
        return path
    if len(parts[0]) == 2 and parts[0][1] == ":":       # D:
        return parts[0] + "\\" + (parts[1] if len(parts) > 2 else "")
    return "/" + parts[0] + ("/" + parts[1] if len(parts) > 2 else "")


def name_problems(name, is_dir=False):
    """Why Windows (or OneDrive/SharePoint) would refuse or mangle this
    name. Empty list = fine."""
    out = []
    if not name:
        return out
    if _ILLEGAL.search(name):
        bad = sorted(set(_ILLEGAL.findall(name)))
        out.append("contains " + " ".join(
            repr(c) if ord(c) < 32 else c for c in bad))
    if name != name.strip():
        out.append("leading/trailing space")
    if name.endswith(".") and name.strip(".") and name != name.strip():
        pass
    elif name.endswith(".") and name.strip("."):
        out.append("trailing dot")
    if not name.strip("."):
        out.append("dots only")
    stem = name.split(".")[0].lower() if "." in name else name.lower()
    if stem in _RESERVED or name.lower() in _RESERVED:
        out.append("reserved device name")
    if unicodedata.is_normalized("NFC", name) is False:
        out.append("non-NFC Unicode (macOS form)")
    if len(name) > 255:
        out.append("name over 255 characters")
    return out


def is_leftover(name, ext):
    low = name.lower()
    if low in _LEFTOVER_NAMES or ext in _LEFTOVER_EXTS:
        return True
    return bool(_LEFTOVER_RE.match(name))


# ----------------------------------------------------------------------------
# The scan
# ----------------------------------------------------------------------------

class Health:
    """One pass over the files table. `.rows[category]` holds
    (path, size, mtime, is_dir, detail) tuples, capped at MAX_ROWS;
    `.counts[category]` is the exact total. `.overview` holds the summary
    figures the report opens with."""

    def __init__(self):
        self.rows = {k: [] for k, _ in CATEGORIES}
        self.counts = {k: 0 for k, _ in CATEGORIES}
        self.bytes = {k: 0 for k, _ in CATEGORIES}
        self.overview = {}
        self.stale_by_top = {}
        self.notes = []

    def _add(self, cat, path, size, mtime, is_dir, detail=""):
        self.counts[cat] += 1
        self.bytes[cat] += size or 0
        if len(self.rows[cat]) < MAX_ROWS:
            self.rows[cat].append((path, size, mtime, is_dir, detail))


def scan(conn, under=None, stale_years=STALE_YEARS, long_path=LONG_PATH,
         secrets=True, progress=None):
    """Compute every category. `progress(done, total)` is called now and
    then when given (the desktop app shows it)."""
    h = Health()
    where, params = "", []
    if under:
        where = " WHERE path = ? OR path LIKE ? ESCAPE '!'"
        params = [under, findex.like_escape(under.rstrip("\\/"))
                  + _sep_for(under) + "%"]
    total = conn.execute("SELECT COUNT(*) FROM files" + where,
                         params).fetchone()[0]
    have_cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    kind_col = "kind" if "kind" in have_cols else "NULL"
    hashed = 0
    if "kind" in have_cols:
        hashed = conn.execute(
            "SELECT COUNT(*) FROM files WHERE kind IS NOT NULL").fetchone()[0]

    now = time.time()
    stale_before = now - stale_years * 365.25 * 86400
    parents = set()                   # every folder that has something in it
    folders = []                      # (path, mtime)
    by_folder_lower = {}              # folder -> {lower name: name}
    clash_reported = set()
    largest = []                      # heap of (size, path, mtime)
    deepest = []                      # heap of (depth, path, mtime, is_dir)
    files_n = dirs_n = total_bytes = 0
    age = {"30d": [0, 0], "1y": [0, 0], "3y": [0, 0], "5y": [0, 0],
           "older": [0, 0]}
    done = 0
    sql = ("SELECT path, name, ext, size, mtime, is_dir, status, error, {} "
           "FROM files{}".format(kind_col, where))
    for path, name, ext, size, mtime, is_dir, status, error, kind in \
            conn.execute(sql, params):
        done += 1
        if progress and done % 20000 == 0:
            progress(done, total)
        size = size or 0
        mtime = mtime or 0
        folder = split_path(path)[0]
        parents.add(folder)
        # case clashes: names in the same folder that fold to the same string
        low = name.lower()
        seen = by_folder_lower.setdefault(folder, {})
        if low in seen:
            o_name = seen[low]
            if o_name != name:
                o_path = folder + _sep_for(path) + o_name
                if o_path not in clash_reported:   # the first of the pair
                    clash_reported.add(o_path)
                    o = conn.execute("SELECT size, mtime, is_dir FROM files "
                                     "WHERE path=?", (o_path,)).fetchone()
                    h._add("case-clashes", o_path, o[0] if o else 0,
                           o[1] if o else 0, o[2] if o else 0,
                           "clashes with " + name)
                h._add("case-clashes", path, size, mtime, int(is_dir),
                       "clashes with " + o_name)
        else:
            seen[low] = name           # one string per row: cheap at 500k
        probs = name_problems(name, is_dir)
        if probs:
            h._add("bad-names", path, size, mtime, int(is_dir), "; ".join(probs))
        if len(path) >= long_path:
            h._add("long-paths", path, size, mtime, int(is_dir),
                   "{} characters".format(len(path)))
        d = _depth(path)
        if len(deepest) < TOP_N:
            heapq.heappush(deepest, (d, path, mtime, int(is_dir)))
        elif d > deepest[0][0]:
            heapq.heapreplace(deepest, (d, path, mtime, int(is_dir)))
        if is_dir:
            dirs_n += 1
            folders.append((path, mtime))
            continue
        files_n += 1
        total_bytes += size
        if size == 0:
            h._add("zero-byte", path, 0, mtime, 0)
        if is_leftover(name, ext):
            h._add("leftovers", path, size, mtime, 0)
        if mtime and mtime < stale_before:
            h._add("stale", path, size, mtime, 0,
                   "{:.1f} years".format((now - mtime) / (365.25 * 86400)))
            t = _top(path)
            agg = h.stale_by_top.setdefault(t, [0, 0])
            agg[0] += 1
            agg[1] += size
        if mtime:
            a = now - mtime
            key = ("30d" if a < 30 * 86400 else "1y" if a < 365.25 * 86400
                   else "3y" if a < 3 * 365.25 * 86400
                   else "5y" if a < 5 * 365.25 * 86400 else "older")
            age[key][0] += 1
            age[key][1] += size
        if len(largest) < TOP_N:
            heapq.heappush(largest, (size, path, mtime))
        elif size > largest[0][0]:
            heapq.heapreplace(largest, (size, path, mtime))
        if status == "error":
            h._add("unreadable", path, size, mtime, 0,
                   "could not extract: " + (error or "")[:120])
        elif kind == "unreadable":
            h._add("unreadable", path, size, mtime, 0, "could not be opened")
        if kind and findex_hash is not None and findex_hash.mismatch(ext, kind):
            h._add("mismatch", path, size, mtime, 0,
                   "named {}, contents are {}".format(ext or "(no ext)", kind))
    if progress:
        progress(total, total)

    # Empty folders: a folder nothing else claims as its parent. A folder
    # that only holds empty folders is not itself "empty" here - its children
    # are listed, which is what you would delete.
    for path, mtime in folders:
        if path not in parents:
            h._add("empty-folders", path, 0, mtime, 1)
    for size, path, mtime in sorted(largest, reverse=True):
        h._add("largest", path, size, mtime, 0)
    for d, path, mtime, is_dir in sorted(deepest, reverse=True):
        h._add("deepest", path, 0, mtime, is_dir, "{} levels".format(d))
    for cat in ("stale", "zero-byte", "leftovers", "long-paths", "bad-names",
                "unreadable", "mismatch", "empty-folders"):
        h.rows[cat].sort(key=lambda r: (-(r[1] or 0), r[0]))
    h.rows["stale"].sort(key=lambda r: (r[2] or 0, r[0]))

    # secrets: a separate module, over the extracted text
    if secrets:
        try:
            import findex_secrets
            for path, what, snippet, n in findex_secrets.scan(conn, under=under):
                h._add("secrets", path, 0, 0, 0,
                       "{}{}: {}".format(what, " x{}".format(n) if n > 1
                                         else "", snippet))
        except ImportError:
            h.notes.append("findex_secrets.py is missing - secrets not scanned")

    # overview figures
    by_ext = conn.execute(
        "SELECT ext, COUNT(*), COALESCE(SUM(size),0) FROM files{} {} is_dir=0 "
        "GROUP BY ext ORDER BY COALESCE(SUM(size),0) DESC LIMIT 40".format(
            where, "AND" if where else "WHERE"), params).fetchall()
    by_top = {}
    for path, size, is_dir in conn.execute(
            "SELECT path, size, is_dir FROM files{} {} is_dir=0".format(
                where, "AND" if where else "WHERE"), params):
        agg = by_top.setdefault(_top(path), [0, 0])
        agg[0] += 1
        agg[1] += size or 0
    dupes = findex.dupe_summary(conn)
    exact = None
    if findex_hash is not None and "fhash" in have_cols:
        try:
            exact = findex_hash.exact_dupe_summary(conn)
        except Exception:                                      # noqa: BLE001
            exact = None
    h.overview = {
        "generated": now, "under": under, "files": files_n, "folders": dirs_n,
        "bytes": total_bytes, "by_ext": by_ext,
        "by_top": sorted(by_top.items(), key=lambda kv: -kv[1][1])[:40],
        "age": age, "stale_years": stale_years, "long_path": long_path,
        "hashed": hashed, "rows_total": total,
        "dupes_name_size": dupes, "dupes_exact": exact,
        "last_index": findex.get_meta(conn, "last_index"),
        "roots": findex.saved_roots(conn),
    }
    if not hashed:
        h.notes.append("No files have been fingerprinted yet, so 'Type does "
                       "not match name' and part of 'Unreadable' are empty. "
                       "Run  findex hash --all  (Health tab: Fingerprint "
                       "types) to fill them in.")
    elif hashed < h.overview["rows_total"] - dirs_n:
        h.notes.append("{:,} of {:,} files have been fingerprinted; type "
                       "checks cover those only.".format(hashed, files_n))
    return h


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

def _when(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (ValueError, OSError):
        return ""


def summary_lines(h):
    o = h.overview
    out = []
    out.append("findex health report - {}".format(
        time.strftime("%Y-%m-%d %H:%M", time.localtime(o["generated"]))))
    if o["under"]:
        out.append("scope: " + o["under"])
    out.append("{:,} files in {:,} folders, {} in total".format(
        o["files"], o["folders"], findex.human(o["bytes"])))
    if o["last_index"]:
        out.append("index last updated {}".format(_when(o["last_index"])))
    out.append("")
    out.append("Findings")
    for key, label in CATEGORIES:
        n = h.counts[key]
        if key == "largest" or key == "deepest":
            continue
        b = h.bytes[key]
        extra = ""
        if key in ("stale", "zero-byte", "leftovers") and b:
            extra = "  ({})".format(findex.human(b))
        out.append("  {:<32} {:>10,}{}".format(label, n, extra))
    g, f, w = o["dupes_name_size"]
    out.append("  {:<32} {:>10,}  ({} reclaimable, by name+size)".format(
        "Duplicate sets", g, findex.human(w)))
    if o["dupes_exact"] and o["dupes_exact"][0]:
        g, f, w = o["dupes_exact"]
        out.append("  {:<32} {:>10,}  ({} reclaimable, identical bytes)"
                   .format("Identical sets", g, findex.human(w)))
    out.append("")
    out.append("Age (by last modified)")
    for key, label in (("30d", "last 30 days"), ("1y", "30 days - 1 year"),
                       ("3y", "1 - 3 years"), ("5y", "3 - 5 years"),
                       ("older", "over 5 years")):
        n, b = o["age"][key]
        out.append("  {:<20} {:>10,}  {:>9}".format(label, n, findex.human(b)))
    out.append("")
    out.append("Space by type (top 15)")
    for ext, n, b in o["by_ext"][:15]:
        out.append("  {:<10} {:>10,}  {:>9}".format(ext or "(none)", n,
                                                  findex.human(b)))
    for note in h.notes:
        out.append("")
        out.append("note: " + note)
    return out


def write_text(h, out):
    out.write("\n".join(summary_lines(h)) + "\n")
    for key, label in CATEGORIES:
        rows = h.rows[key]
        if not rows:
            continue
        out.write("\n\n{} ({:,}{})\n".format(
            label, h.counts[key],
            ", first {:,} listed".format(len(rows))
            if h.counts[key] > len(rows) else ""))
        for path, size, mtime, is_dir, detail in rows[:2000]:
            line = "  {:>9}  {}  {}".format(
                "folder" if is_dir else findex.human(size or 0),
                _when(mtime) or "          ", path)
            if detail:
                line += "    [{}]".format(detail)
            out.write(line + "\n")


def write_csv(h, out):
    w = csv.writer(out)
    w.writerow(["category", "path", "size", "modified", "is_dir", "detail"])
    for key, label in CATEGORIES:
        for path, size, mtime, is_dir, detail in h.rows[key]:
            w.writerow([key, path, size or 0, _when(mtime), is_dir, detail])


def write_json(h, out):
    data = {"overview": dict(h.overview), "counts": h.counts,
            "bytes": h.bytes, "notes": h.notes,
            "findings": {k: [{"path": p, "size": s, "modified": m,
                              "is_dir": d, "detail": t}
                             for p, s, m, d, t in h.rows[k]]
                         for k, _ in CATEGORIES}}
    data["overview"]["by_top"] = [[k, v] for k, v in h.overview["by_top"]]
    json.dump(data, out, indent=1, ensure_ascii=False, default=str)


_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;
 background:#f4f6f9;color:#1b1f27}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 10px;
 padding-top:12px;border-top:1px solid #d9dee7}
.sub{color:#5a6475;margin-bottom:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
 gap:10px;margin:18px 0}
.card{background:#fff;border:1px solid #d9dee7;border-radius:8px;padding:12px}
.card b{display:block;font-size:22px}.card span{color:#5a6475;font-size:12px}
.card.warn b{color:#b42318}.card.ok b{color:#146c2e}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid
 #d9dee7;border-radius:8px;overflow:hidden;font-size:13px}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid #eef1f5;
 vertical-align:top}th{background:#eef1f5;font-weight:600}
td.n,th.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.p{font-family:Consolas,Menlo,monospace;font-size:12px;word-break:break-all}
.note{background:#fff7e0;border:1px solid #f0d78a;border-radius:8px;
 padding:10px 14px;margin:12px 0}
.more{color:#5a6475;font-size:12px;margin:6px 0 0}
details summary{cursor:pointer;color:#1f6feb}
@media (prefers-color-scheme:dark){body{background:#14161b;color:#e9ebf0}
 .card,table{background:#1c1f27;border-color:#333b49}th{background:#232834}
 td,th{border-color:#262b36}.sub,.card span,.more{color:#98a2b3}
 h2{border-color:#333b49}.note{background:#2a2410;border-color:#6b5a1c}}
"""


def write_html(h, out, rows_per_section=500):
    o = h.overview
    e = html.escape
    w = out.write
    w("<!doctype html><html><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>findex health report</title><style>{}</style></head><body>"
      "<main>".format(_CSS))
    w("<h1>File tree health</h1><div class='sub'>{}{} &middot; {:,} files in "
      "{:,} folders &middot; {}{}</div>".format(
          e(o["under"] + " &middot; ") if o["under"] else "",
          time.strftime("%d %b %Y %H:%M", time.localtime(o["generated"])),
          o["files"], o["folders"], findex.human(o["bytes"]),
          " &middot; index updated " + _when(o["last_index"])
          if o["last_index"] else ""))
    for note in h.notes:
        w("<div class='note'>{}</div>".format(e(note)))
    w("<div class='cards'>")
    for key, label in CATEGORIES:
        if key in ("largest", "deepest"):
            continue
        n = h.counts[key]
        cls = "warn" if n and key not in ("stale",) else ("ok" if not n else "")
        extra = ""
        if key in ("stale", "zero-byte", "leftovers") and h.bytes[key]:
            extra = " &middot; " + findex.human(h.bytes[key])
        w("<div class='card {}'><b>{:,}</b><span>{}{}</span></div>".format(
            cls, n, e(label), extra))
    g, f, wasted = o["dupes_name_size"]
    w("<div class='card'><b>{:,}</b><span>duplicate sets by name+size &middot; "
      "{} reclaimable</span></div>".format(g, findex.human(wasted)))
    if o["dupes_exact"] and o["dupes_exact"][0]:
        g, f, wasted = o["dupes_exact"]
        w("<div class='card'><b>{:,}</b><span>sets of identical files &middot; "
          "{} reclaimable</span></div>".format(g, findex.human(wasted)))
    w("</div>")

    w("<h2>Age, by last modified</h2><table><tr><th>Modified</th>"
      "<th class='n'>Files</th><th class='n'>Size</th></tr>")
    for key, label in (("30d", "last 30 days"), ("1y", "30 days to a year"),
                       ("3y", "1 to 3 years"), ("5y", "3 to 5 years"),
                       ("older", "over 5 years")):
        n, b = o["age"][key]
        w("<tr><td>{}</td><td class='n'>{:,}</td><td class='n'>{}</td></tr>"
          .format(label, n, findex.human(b)))
    w("</table>")

    w("<h2>Space by type</h2><table><tr><th>Type</th><th class='n'>Files</th>"
      "<th class='n'>Size</th></tr>")
    for ext, n, b in o["by_ext"]:
        w("<tr><td>{}</td><td class='n'>{:,}</td><td class='n'>{}</td></tr>"
          .format(e(ext or "(no extension)"), n, findex.human(b)))
    w("</table>")

    w("<h2>Space by location</h2><table><tr><th>Location</th>"
      "<th class='n'>Files</th><th class='n'>Size</th></tr>")
    for top, (n, b) in o["by_top"]:
        w("<tr><td class='p'>{}</td><td class='n'>{:,}</td><td class='n'>{}"
          "</td></tr>".format(e(top), n, findex.human(b)))
    w("</table>")

    if h.stale_by_top:
        w("<h2>Stale files by location (untouched {:g}+ years)</h2><table>"
          "<tr><th>Location</th><th class='n'>Files</th><th class='n'>Size"
          "</th></tr>".format(o["stale_years"]))
        for top, (n, b) in sorted(h.stale_by_top.items(),
                                  key=lambda kv: -kv[1][1])[:40]:
            w("<tr><td class='p'>{}</td><td class='n'>{:,}</td><td class='n'>"
              "{}</td></tr>".format(e(top), n, findex.human(b)))
        w("</table>")

    for key, label in CATEGORIES:
        rows = h.rows[key]
        if not rows:
            continue
        w("<h2>{} <small>({:,})</small></h2>".format(e(label), h.counts[key]))
        w("<table><tr><th>Path</th><th class='n'>Size</th><th>Modified</th>"
          "<th>Detail</th></tr>")
        for path, size, mtime, is_dir, detail in rows[:rows_per_section]:
            w("<tr><td class='p'>{}</td><td class='n'>{}</td><td>{}</td>"
              "<td>{}</td></tr>".format(
                  e(path), "folder" if is_dir else findex.human(size or 0),
                  _when(mtime), e(detail or "")))
        w("</table>")
        if h.counts[key] > rows_per_section:
            w("<p class='more'>{:,} more not shown - export as CSV for the "
              "full list.</p>".format(h.counts[key] - rows_per_section))
    w("</main></body></html>")


def export(h, out_path, fmt=None):
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip(".").lower()
           or "txt").lower()
    if fmt not in ("txt", "csv", "json", "html", "htm"):
        raise ValueError("format must be html, txt, csv or json, not " + fmt)
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        if fmt in ("html", "htm"):
            write_html(h, out)
        elif fmt == "csv":
            write_csv(h, out)
        elif fmt == "json":
            write_json(h, out)
        else:
            write_text(h, out)
    return fmt


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_report(args):
    conn = findex.open_db_ro(args.db)
    try:
        h = scan(conn, under=args.under, stale_years=args.stale_years,
                 long_path=args.long_path, secrets=not args.no_secrets,
                 progress=(lambda d, t: print("@P seen={} total={}".format(d, t),
                                              flush=True))
                 if args.progress else None)
    finally:
        conn.close()
    if args.list:
        key = args.list
        rows = h.rows[key]
        print("{} - {:,}{}".format(LABEL[key], h.counts[key],
                                   " (first {:,})".format(len(rows))
                                   if h.counts[key] > len(rows) else ""))
        for path, size, mtime, is_dir, detail in rows:
            line = "{:>9}  {}  {}".format(
                "folder" if is_dir else findex.human(size or 0),
                _when(mtime) or "          ", path)
            if detail:
                line += "    [{}]".format(detail)
            print(line)
        return 0
    if args.out:
        fmt = export(h, args.out, args.format)
        print("Wrote {} ({})".format(args.out, fmt))
        for line in summary_lines(h)[:3]:
            print(line)
        return 0
    print("\n".join(summary_lines(h)))
    return 0


def add_commands(sub):
    p = sub.add_parser("report", help="health report: empty folders, bad "
                       "names, long paths, stale/zero-byte/temp files, "
                       "corrupt files, type mismatches, secrets")
    p.add_argument("-o", "--out", help="write the report here (.html, .txt, "
                   ".csv or .json by extension); no -o prints the summary")
    p.add_argument("-f", "--format", choices=("html", "txt", "csv", "json"))
    p.add_argument("--under", metavar="FOLDER", help="only beneath this folder")
    p.add_argument("--list", choices=[k for k, _ in CATEGORIES],
                   help="print one category in full instead of the summary")
    p.add_argument("--stale-years", type=float, default=STALE_YEARS,
                   help="'stale' = not modified for this long (default 3)")
    p.add_argument("--long-path", type=int, default=LONG_PATH,
                   help="'long path' threshold in characters (default 260)")
    p.add_argument("--no-secrets", action="store_true",
                   help="skip the secrets scan (the slowest category)")
    p.add_argument("--progress", action="store_true",
                   help="emit @P progress lines (used by the desktop app)")
    p.set_defaults(func=cmd_report)


if __name__ == "__main__":
    sys.exit(findex.main())
