#!/usr/bin/env python3
"""
findex_organise - sort the files under a folder into subfolders by rules,
with a preview of the resulting tree, suggestions for the rules, a check
of the rules for mistakes, and an undo.

    findex organise D:\\Downloads --suggest              propose rules
    findex organise D:\\Downloads --rules tidy.rules     preview (dry run)
    findex organise D:\\Downloads --rules tidy.rules -o plan.html
    findex organise D:\\Downloads --rules tidy.rules --apply [--copy]
    findex organise --undo

RULES - one per line, first match wins:

    pattern [pattern ...] -> destination
    # comments and blank lines are ignored

  A pattern is one of (several on one line must ALL match):
    Invoice*             glob on the file name, case-insensitive; quote it
    "Board minutes*"     when it has spaces
    re:^([A-Z]{3})-\\d+   regular expression on the name (rei: ignores case);
                         groups come back as {1} {2} ... in the destination;
                         quote it - re:"^Board (minutes|agenda)" - if it
                         contains a space
    ext:pdf;docx         extension(s)
    type:images          a type group: images videos audio documents
                         compressed code programs emails
    year:2019-2021       modified in these years (or one year)
    older:3y  newer:30d  age (d/w/m/y)
    *                    everything (a sweep - put it last)

  The destination is a folder path relative to the folder being organised
  (an absolute path works too) and may use tokens:
    {1}..{9}   regex groups        {name} {stem} {ext}   the file's own
    {type}     its type group      {first}  first word of the name
    {year} {month} {day} {date} {yyyymm}   from the modified date
    {parent}   the folder it came from
    {1|upper} {first|title} {1|lower}      case filters on any token

Everything under the folder is considered, subfolders included, and
re-sorted against the rules (a file already where a rule sends it is left
alone). Files no rule matches stay where they are unless a sweep is on.
Nothing is overwritten: a different file already at the target is a
collision and is skipped; an identical one (by content hash) counts as
done when --skip-identical is set. --apply moves (or --copy copies) as one
batch that `findex rename --undo` / `findex organise --undo` reverses,
recorded in the journal; --remove-empty removes folders left empty by the
moves. Rule sets can be saved as templates in the index and re-run by name.
"""

from __future__ import annotations

import csv
import fnmatch
import html
import json
import os
import re
import shutil
import sys
import time

import findex
import findex_rename

try:
    import findex_hash
except ImportError:
    findex_hash = None

TEMPLATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS organise_templates (
    name    TEXT PRIMARY KEY,
    rules   TEXT NOT NULL,
    options TEXT,
    saved   REAL
);
"""

ACTIONS = ("move", "copy", "unchanged", "identical", "collision", "unmatched",
           "sweep", "error")
_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_TOKEN = re.compile(r"\{([A-Za-z0-9_-]+)(?:\|(upper|lower|title))?\}")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _sep_for(path):
    return "\\" if "\\" in path or (len(path) > 1 and path[1] == ":") else "/"


def _split(path):
    i = max(path.rfind("\\"), path.rfind("/"))
    return (path[:i] or path[:i + 1], path[i + 1:]) if i >= 0 else ("", path)


def type_of(ext):
    for group, exts in findex.TYPE_GROUPS.items():
        if ext in exts:
            return group
    return "other"


# ----------------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------------

class Rule:
    __slots__ = ("line", "raw", "preds", "dest", "error", "regex", "matched",
                 "shadowed", "enabled")

    def __init__(self, line, raw):
        self.line, self.raw = line, raw
        self.preds, self.dest, self.error, self.regex = [], "", None, None
        self.matched = 0            # files this rule decided
        self.shadowed = 0           # files it matched but an earlier rule took
        self.enabled = True


def _tokens(text):
    """Split the pattern side on spaces, honouring double quotes."""
    out, cur, q = [], "", False
    for ch in text:
        if ch == '"':
            q = not q
        elif ch.isspace() and not q:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _age_seconds(spec):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dwmy])", spec.lower())
    if not m:
        raise ValueError("age must look like 30d, 6w, 3m or 2y")
    n, unit = float(m.group(1)), m.group(2)
    return n * {"d": 86400, "w": 604800, "m": 2629800, "y": 31557600}[unit]


def parse_rules(text):
    """-> [Rule]; rules with a problem carry .error and are skipped when
    planning (but reported by lint)."""
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rule = Rule(n, raw)
        rules.append(rule)
        sep = "->" if "->" in line else ("=>" if "=>" in line else None)
        if sep is None:
            rule.error = "missing '->' between pattern and destination"
            continue
        left, _, right = line.partition(sep)
        rule.dest = right.strip().strip('"')
        if not rule.dest:
            rule.error = "no destination after '->'"
            continue
        toks = _tokens(left.strip())
        if not toks:
            rule.error = "no pattern before '->'"
            continue
        for tok in toks:
            low = tok.lower()
            try:
                if low.startswith(("re:", "rei:")):
                    pat = tok.split(":", 1)[1]
                    flags = re.IGNORECASE if low.startswith("rei:") else 0
                    rx = re.compile(pat, flags)
                    rule.preds.append(("re", rx))
                    if rule.regex is None:
                        rule.regex = rx
                elif low.startswith("ext:"):
                    exts = {"." + e.strip().lstrip(".").lower()
                            for e in tok[4:].replace(",", ";").split(";")
                            if e.strip()}
                    if not exts:
                        raise ValueError("ext: needs at least one extension")
                    rule.preds.append(("ext", exts))
                elif low.startswith("type:"):
                    groups = [g.strip().lower() for g in
                              tok[5:].replace(",", ";").split(";") if g.strip()]
                    exts = set()
                    for g in groups:
                        if g not in findex.TYPE_GROUPS and g != "other":
                            raise ValueError("unknown type group {!r} (use {})"
                                             .format(g, ", ".join(
                                                 findex.TYPE_GROUPS)))
                        exts |= set(findex.TYPE_GROUPS.get(g, ()))
                    rule.preds.append(("type", (set(groups), exts)))
                elif low.startswith("year:"):
                    spec = tok[5:]
                    if "-" in spec:
                        a, b = spec.split("-", 1)
                        years = range(int(a), int(b) + 1)
                    else:
                        years = range(int(spec), int(spec) + 1)
                    rule.preds.append(("year", set(years)))
                elif low.startswith("older:"):
                    rule.preds.append(("older", _age_seconds(tok[6:])))
                elif low.startswith("newer:"):
                    rule.preds.append(("newer", _age_seconds(tok[6:])))
                else:
                    rule.preds.append(("glob", tok))
            except (re.error, ValueError) as exc:
                rule.error = "{}: {}".format(tok, exc)
                break
    return rules


def _match(rule, name, ext, mtime, now):
    """The regex match object (or True) when every predicate holds."""
    hit = True
    for kind, val in rule.preds:
        if kind == "glob":
            if val != "*" and not fnmatch.fnmatch(name.lower(), val.lower()):
                return None
        elif kind == "re":
            m = val.search(name)
            if not m:
                return None
            if val is rule.regex:
                hit = m
        elif kind == "ext":
            if ext not in val:
                return None
        elif kind == "type":
            groups, exts = val
            if ext not in exts and not ("other" in groups
                                        and type_of(ext) == "other"):
                return None
        elif kind == "year":
            if int(time.strftime("%Y", time.localtime(mtime or 0))) not in val:
                return None
        elif kind == "older":
            if not mtime or now - mtime < val:
                return None
        elif kind == "newer":
            if not mtime or now - mtime > val:
                return None
    return hit


def expand_dest(dest, name, ext, mtime, m, parent):
    """Fill the destination's tokens for one file. Raises KeyError with
    the token name when one cannot be filled."""
    stem = name[:-len(ext)] if ext and name.lower().endswith(ext) else name
    lt = time.localtime(mtime or 0) if mtime else None

    def value(tok):
        if tok.isdigit():
            if m is None or m is True:
                raise KeyError("{" + tok + "} needs a re: pattern with groups")
            i = int(tok)
            if i > (m.re.groups or 0) or m.group(i) is None:
                raise KeyError("{" + tok + "}: the pattern has no group " + tok)
            return m.group(i)
        if tok == "name":
            return name
        if tok == "stem":
            return stem
        if tok == "ext":
            return ext.lstrip(".")
        if tok == "type":
            return type_of(ext)
        if tok == "first":
            w = _WORD.search(stem)
            return w.group(0) if w else stem
        if tok == "parent":
            return _split(parent)[1] or parent
        if tok in ("year", "month", "day", "date", "yyyymm"):
            if lt is None:
                raise KeyError("{" + tok + "}: the file has no date")
            return {"year": "%Y", "month": "%m", "day": "%d",
                    "date": "%Y-%m-%d", "yyyymm": "%Y-%m"}[tok] and \
                time.strftime({"year": "%Y", "month": "%m", "day": "%d",
                               "date": "%Y-%m-%d", "yyyymm": "%Y-%m"}[tok], lt)
        raise KeyError("{" + tok + "} is not a known token")

    def sub(mo):
        v = value(mo.group(1))
        f = mo.group(2)
        if f == "upper":
            v = v.upper()
        elif f == "lower":
            v = v.lower()
        elif f == "title":
            v = v.title()
        return _ILLEGAL.sub("_", v.strip())
    out = _TOKEN.sub(sub, dest)
    return out


def _dest_segments(dest):
    """Clean, relative path segments for a destination (or None when the
    destination is absolute - returned as-is by the caller)."""
    d = dest.replace("\\", "/").strip()
    segs = [s.strip().rstrip(".") for s in d.split("/") if s.strip() not in
            ("", ".")]
    return segs


def _is_abs(dest):
    return bool(re.match(r"^([A-Za-z]:[\\/]|\\\\|/)", dest))


# ----------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------

def _node():
    return {"folders": {}, "files": [], "n": 0, "b": 0}


class Plan:
    def __init__(self, root, options):
        self.root = root
        self.options = options
        self.rows = []          # (path, size, mtime, target, action, rule, note)
        self.counts = {a: 0 for a in ACTIONS}
        self.bytes = {a: 0 for a in ACTIONS}
        self.by_dest = {}       # rel dest folder -> [files, bytes]
        self.tree = _node()     # nested {"folders": {name: node}, "files",
                                #         "n" (files placed here), "b" (bytes)}
        self.empty_after = []   # folders that would be left empty (moves)
        self.issues = []        # from lint
        self.rules = []
        self.total = 0

    def summary_lines(self):
        o = self.options
        verb = "copy" if o.get("copy") else "move"
        out = ["organise {}  ({})".format(self.root, verb)]
        out.append("  {:,} files considered".format(self.total))
        n = self.counts["move"] + self.counts["copy"] + self.counts["sweep"]
        b = self.bytes["move"] + self.bytes["copy"] + self.bytes["sweep"]
        out.append("  {:,} to {}  ({})".format(n, verb, findex.human(b)))
        if self.counts["sweep"]:
            out.append("    of which {:,} swept into {}".format(
                self.counts["sweep"], o.get("sweep") or "_Unsorted"))
        out.append("  {:,} already in place".format(self.counts["unchanged"]))
        if self.counts["identical"]:
            out.append("  {:,} identical copies already at the target (skipped)"
                       .format(self.counts["identical"]))
        if self.counts["collision"]:
            out.append("  {:,} COLLISIONS - a different file is already there "
                       "(skipped)".format(self.counts["collision"]))
        if self.counts["error"]:
            out.append("  {:,} could not be placed (see the plan)".format(
                self.counts["error"]))
        out.append("  {:,} unmatched, left where they are".format(
            self.counts["unmatched"]))
        out.append("  {:,} destination folder(s){}".format(
            len(self.by_dest), ", {:,} folder(s) would be left empty".format(
                len(self.empty_after)) if self.empty_after else ""))
        errs = [i for i in self.issues if i[0] == "error"]
        warns = [i for i in self.issues if i[0] == "warning"]
        if errs or warns:
            out.append("  rules: {:,} error(s), {:,} warning(s)".format(
                len(errs), len(warns)))
        return out


def files_under(conn, root):
    """[(path, size, mtime, fhash, ext)] for every FILE beneath root, and
    the set of folder paths beneath it."""
    sep = _sep_for(root)
    like = findex.like_escape(root.rstrip("\\/")) + sep + "%"
    have = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    hcol = "fhash" if "fhash" in have else "NULL"
    files, folders = [], set()
    for path, size, mtime, fh, ext, is_dir in conn.execute(
            "SELECT path, size, mtime, {}, ext, is_dir FROM files WHERE path "
            "LIKE ? ESCAPE '!'".format(hcol), (like,)):
        if is_dir:
            folders.add(path)
        else:
            files.append((path, size or 0, mtime or 0, fh, ext or ""))
    return files, folders


def build_plan(conn, root, rules_text, options=None, files=None,
               folders=None):
    """options: copy (bool), sweep (folder name or None), skip_identical
    (bool), remove_empty (bool). Read-only against conn."""
    options = dict(options or {})
    root = root.rstrip("\\/") or root
    sep = _sep_for(root)
    plan = Plan(root, options)
    rules = parse_rules(rules_text)
    plan.rules = rules
    live = [r for r in rules if not r.error]
    if files is None:
        files, folders = files_under(conn, root)
    plan.total = len(files)
    now = time.time()
    sweep = options.get("sweep")
    copy = bool(options.get("copy"))
    skip_identical = bool(options.get("skip_identical"))

    # what is already at every path under root (index) - for collisions
    existing = {}
    for path, size, mtime, fh, ext in files:
        existing[os.path.normcase(path)] = (size, fh)
    folder_keys = {os.path.normcase(f) for f in (folders or ())}

    targets = {}                     # normcase target -> source path
    remaining = {}                   # folder -> files staying (for empties)
    moved_from = {}
    for path, size, mtime, fh, ext in files:
        parent, name = _split(path)
        remaining.setdefault(parent, 0)
        chosen, m, note = None, None, ""
        for rule in live:
            hit = _match(rule, name, ext, mtime, now)
            if hit is None:
                continue
            if chosen is None:
                chosen, m = rule, hit
                rule.matched += 1
            else:
                rule.shadowed += 1
        action = "unmatched"
        target = ""
        if chosen is None and sweep:
            dest_rel = sweep
            action = "sweep"
        elif chosen is None:
            plan.rows.append((path, size, mtime, "", "unmatched", None, ""))
            plan.counts["unmatched"] += 1
            plan.bytes["unmatched"] += size
            remaining[parent] += 1
            continue
        else:
            try:
                dest_rel = expand_dest(chosen.dest, name, ext, mtime, m, parent)
            except KeyError as exc:
                plan.rows.append((path, size, mtime, "", "error", chosen.line,
                                  str(exc).strip("'")))
                plan.counts["error"] += 1
                remaining[parent] += 1
                continue
            action = "copy" if copy else "move"
        if _is_abs(dest_rel):
            dest_dir = dest_rel.rstrip("\\/")
            rel_key = dest_dir
        else:
            segs = _dest_segments(dest_rel)
            if not segs:
                dest_dir = root
                rel_key = "."
            else:
                bad = [s for s in segs if _ILLEGAL.search(s) or s in ("..",)]
                if bad:
                    plan.rows.append((path, size, mtime, "", "error",
                                      chosen.line if chosen else None,
                                      "destination has an invalid folder name "
                                      "{!r}".format(bad[0])))
                    plan.counts["error"] += 1
                    remaining[parent] += 1
                    continue
                dest_dir = root + sep + sep.join(segs)
                rel_key = "/".join(segs)
        target = dest_dir + sep + name
        if os.path.normcase(dest_dir) in existing:
            plan.rows.append((path, size, mtime, target, "error",
                              chosen.line if chosen else None,
                              "destination folder is an existing file"))
            plan.counts["error"] += 1
            remaining[parent] += 1
            continue
        key = os.path.normcase(target)
        if key == os.path.normcase(path):
            action = "unchanged"
            note = ""
        elif key in targets:
            opath, ofh = targets[key]
            if skip_identical and fh and ofh and fh == ofh:
                action = "identical"
                note = "identical to {} which lands there".format(opath)
            else:
                action = "collision"
                note = ("another file in this run lands on the same name "
                        "({})".format(_split(opath)[0]))
        elif key in existing:
            osize, ofh = existing[key]
            if skip_identical and fh and ofh and fh == ofh:
                action = "identical"
                note = "an identical file is already there"
            elif fh and ofh and fh == ofh:
                action = "collision"
                note = "an identical file is already there (tick 'skip " \
                       "identical' to treat as done)"
            elif osize == size:
                action = "collision"
                note = "a file of the same name and size is already there"
            else:
                action = "collision"
                note = "a different file is already there"
        elif os.path.exists(findex.lp(target)) and not copy:
            action = "collision"
            note = "already exists on disk (not in the index)"
        elif os.path.exists(findex.lp(target)):
            action = "collision"
            note = "already exists on disk"
        if action in ("move", "copy", "sweep"):
            targets[key] = (path, fh)
            plan.by_dest.setdefault(rel_key, [0, 0])
            plan.by_dest[rel_key][0] += 1
            plan.by_dest[rel_key][1] += size
            node = plan.tree
            for s in (rel_key.split("/") if rel_key != "." else []):
                node = node["folders"].setdefault(s, _node())
            node["n"] += 1
            node["b"] += size
            if len(node["files"]) < 12:
                node["files"].append(name)
            if not copy:
                moved_from[parent] = moved_from.get(parent, 0) + 1
            else:
                remaining[parent] += 1
        else:
            remaining[parent] += 1
        plan.rows.append((path, size, mtime, target, action,
                          chosen.line if chosen else None, note))
        plan.counts[action] += 1
        plan.bytes[action] += size

    # folders left empty by the moves: nothing stays in them or below, they
    # are not (an ancestor of) a destination, and something actually moves
    # out of them - a folder that was already empty is the report's business
    if not copy and folders:
        def rollup(counts):
            """folder -> files counted in it or anywhere beneath it."""
            out = {}
            stop = len(root)
            for f, c in counts.items():
                a = f
                while a and len(a) >= stop:
                    out[a] = out.get(a, 0) + c
                    a = _split(a)[0]
            return out
        rem_below, moved_below = rollup(remaining), rollup(moved_from)

        def below(rolled, folder):
            return rolled.get(folder, 0)
        dest_dirs = set()
        for k in plan.by_dest:
            d = root if k == "." else root + sep + k.replace("/", sep)
            while d and len(d) >= len(root):
                dest_dirs.add(os.path.normcase(d))
                d = _split(d)[0]
        qualifying = {f for f in folders
                      if os.path.normcase(f) not in dest_dirs
                      and below(rem_below, f) == 0}
        for f in sorted(qualifying):
            ok = below(moved_below, f) > 0
            if not ok:            # an already-empty folder inside one emptied
                a = _split(f)[0]
                while a and len(a) > len(root):
                    if a in qualifying and below(moved_below, a) > 0:
                        ok = True
                        break
                    a = _split(a)[0]
            if ok:
                plan.empty_after.append(f)
    plan.issues = lint(rules, plan, root)
    return plan


# ----------------------------------------------------------------------------
# Lint - what is wrong with the rules
# ----------------------------------------------------------------------------

def lint(rules, plan=None, root=None):
    """[(level, line, message)] - level error / warning / info."""
    out = []
    seen_pat = {}
    catch_all_line = None
    dest_case = {}
    for r in rules:
        if r.error:
            out.append(("error", r.line, r.error))
            continue
        # tokens the destination uses vs what the patterns provide
        for tok, _f in _TOKEN.findall(r.dest):
            if tok.isdigit():
                if r.regex is None:
                    out.append(("error", r.line, "{{{}}} needs a re: pattern "
                                "with groups".format(tok)))
                elif int(tok) > (r.regex.groups or 0):
                    out.append(("error", r.line, "{{{}}} but the pattern has "
                                "only {} group(s)".format(tok, r.regex.groups)))
            elif tok not in ("name", "stem", "ext", "type", "first", "parent",
                             "year", "month", "day", "date", "yyyymm"):
                out.append(("error", r.line, "unknown token {{{}}}".format(tok)))
        if not _is_abs(r.dest):
            segs = _dest_segments(r.dest)
            if not segs:
                out.append(("warning", r.line, "destination is the folder "
                            "itself - matching files stay at the top level"))
            for s in segs:
                plain = _TOKEN.sub("x", s)
                if _ILLEGAL.search(plain):
                    out.append(("error", r.line, "folder name {!r} has a "
                                "character Windows refuses".format(s)))
                if s == "..":
                    out.append(("error", r.line, "'..' would climb out of the "
                                "folder being organised"))
            low = r.dest.replace("\\", "/").lower()
            if low in dest_case and dest_case[low] != r.dest:
                out.append(("warning", r.line, "destination {!r} differs only "
                            "by case from {!r} (line {}) - one folder on "
                            "Windows, two on a Mac".format(
                                r.dest, dest_case[low][0], dest_case[low][1])))
            dest_case.setdefault(low, (r.dest, r.line))
        else:
            out.append(("warning", r.line, "absolute destination - files "
                        "leave the folder being organised"))
        key = " ".join(sorted(str(v.pattern if hasattr(v, "pattern") else v)
                              for k, v in r.preds)).lower()
        if key in seen_pat:
            out.append(("warning", r.line, "same pattern as line {} - this "
                        "rule can never match".format(seen_pat[key])))
        seen_pat.setdefault(key, r.line)
        if catch_all_line is not None:
            out.append(("warning", r.line, "comes after the catch-all on line "
                        "{} and can never be reached".format(catch_all_line)))
        if all(k == "glob" and v == "*" for k, v in r.preds) and \
                catch_all_line is None:
            catch_all_line = r.line
        if any(k == "glob" and v not in ("*",) and "*" not in v and "?" not in v
               and "[" not in v for k, v in r.preds):
            out.append(("info", r.line, "a glob without * or ? matches only "
                        "that exact name - did you mean {}*?".format(
                            next(v for k, v in r.preds if k == "glob"))))
    if plan is not None:
        for r in rules:
            if r.error:
                continue
            if r.matched == 0 and r.shadowed == 0:
                out.append(("warning", r.line, "matches no file under {}"
                            .format(root or "the folder")))
            elif r.matched == 0:
                out.append(("warning", r.line, "every file it matches ({:,}) "
                            "is taken by an earlier rule - never used".format(
                                r.shadowed)))
            elif r.shadowed:
                out.append(("info", r.line, "{:,} file(s) it also matches go "
                            "to an earlier rule".format(r.shadowed)))
        if plan.counts["collision"]:
            out.append(("warning", None, "{:,} collision(s): a different file "
                        "is already at the target - those are skipped".format(
                            plan.counts["collision"])))
        if plan.counts["error"]:
            out.append(("error", None, "{:,} file(s) could not be placed - "
                        "see the plan's notes".format(plan.counts["error"])))
        if plan.total and plan.counts["unmatched"] * 2 > plan.total \
                and not plan.options.get("sweep"):
            out.append(("info", None, "more than half the files match no rule "
                        "and stay put - add rules, or turn on the sweep"))
    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda i: (order[i[0]], i[1] or 0))
    return out


# ----------------------------------------------------------------------------
# Suggestions - a draft rule set from the names
# ----------------------------------------------------------------------------

KNOWN_PREFIX = {
    "img": "Photos", "dsc": "Photos", "dscn": "Photos", "dcim": "Photos",
    "pxl": "Photos", "mvimg": "Photos", "photo": "Photos", "picture": "Photos",
    "screenshot": "Screenshots", "screen shot": "Screenshots",
    "capture": "Screenshots", "snip": "Screenshots",
    "whatsapp": "WhatsApp", "signal": "Signal",
    "invoice": "Invoices", "inv": "Invoices", "receipt": "Receipts",
    "statement": "Statements", "payslip": "Payslips", "quote": "Quotes",
    "quotation": "Quotes", "estimate": "Quotes", "po": "Purchase orders",
    "minutes": "Minutes", "agenda": "Agendas", "report": "Reports",
    "cv": "CVs", "resume": "CVs", "contract": "Contracts",
    "agreement": "Contracts", "policy": "Policies", "procedure": "Procedures",
    "scan": "Scans", "scanned": "Scans", "letter": "Letters",
    "presentation": "Presentations", "budget": "Budgets",
    "timesheet": "Timesheets", "audit": "Audits", "certificate": "Certificates",
    "cert": "Certificates", "form": "Forms", "template": "Templates",
    "draft": "Drafts", "final": "Final", "backup": "Backups", "export": "Exports",
    "download": "Downloads", "setup": "Installers", "install": "Installers",
    "installer": "Installers", "manual": "Manuals", "guide": "Guides",
    "meeting": "Meetings", "notes": "Notes", "note": "Notes", "plan": "Plans",
    "proposal": "Proposals", "tender": "Tenders", "bid": "Tenders",
    "spec": "Specifications", "specification": "Specifications",
    "recording": "Recordings", "rec": "Recordings", "voice": "Recordings",
    "zoom": "Recordings", "teams": "Recordings",
}
TYPE_FOLDER = {"images": "Photos", "videos": "Videos", "audio": "Audio",
               "documents": "Documents", "compressed": "Archives",
               "code": "Code", "programs": "Software", "emails": "Emails"}
STOP = {"the", "a", "an", "of", "and", "to", "for", "in", "on", "new", "copy",
        "file", "document", "doc", "untitled", "my", "v", "ver", "version",
        "final", "draft", "old", "misc", "other", "temp", "tmp", "test",
        "image", "video", "audio", "data", "info"}
_CODE = re.compile(r"^([A-Z]{2,5})[-_ ]?(\d{2,})(?![A-Za-z])")
_DATE_NAME = re.compile(r"^(20\d\d|19\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?"
                        r"(0[1-9]|[12]\d|3[01])")
_YEAR_IN = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")


def _stem(name, ext):
    return name[:-len(ext)] if ext and name.lower().endswith(ext) else name


def _words(stem):
    # split camelCase too: "BoardMinutes2023" -> Board Minutes 2023
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    return [w for w in _WORD.findall(s)]


def _title(word):
    return word if word.isupper() and len(word) <= 5 else word.capitalize()


def suggest(files, min_count=None):
    """files: [(path, size, mtime, fhash, ext)] under the root.
    -> {"rules": [(rule_text, count, reason, samples)], "notes": [...]}
    Ordered most-specific first, type-group rules last. The caller decides
    whether to add a sweep."""
    n = len(files)
    if not n:
        return {"rules": [], "notes": ["no files under the folder"]}
    min_count = min_count or max(4, int(n * 0.02))
    notes = []
    names = [(_split(p)[1], ext, mtime, size) for p, size, mtime, fh, ext
             in files]
    taken = [False] * n
    rules = []

    def claim(pred):
        """Mark files pred() accepts (index) and return them."""
        got = []
        for i, (name, ext, mtime, size) in enumerate(names):
            if not taken[i] and pred(name, ext):
                got.append(i)
        return got

    def years_of(idxs):
        return {time.strftime("%Y", time.localtime(names[i][2] or 0))
                for i in idxs}

    # 1. code prefixes like ACM-0042, INV20231
    codes = {}
    for i, (name, ext, mtime, size) in enumerate(names):
        m = _CODE.match(_stem(name, ext))
        if m and m.group(1).lower() not in KNOWN_PREFIX:
            codes.setdefault(m.group(1), []).append(i)
    big = {k: v for k, v in codes.items() if len(v) >= min_count}
    if len(big) >= 3:
        idxs = [i for v in big.values() for i in v]
        skip = sorted(k.upper() for k in KNOWN_PREFIX
                      if k.isalpha() and 2 <= len(k) <= 5)
        text = 're:"^(?!(?:{})[-_ ]?\\d)([A-Z]{{2,5}})[-_ ]?\\d{{2,}}" -> {{1}}'.format(
            "|".join(skip))
        for i in idxs:
            taken[i] = True
        rules.append((text, len(idxs), "{} reference prefixes ({}) - one "
                      "folder per prefix".format(
                          len(big), ", ".join(sorted(big)[:6])),
                      [names[i][0] for i in idxs[:4]]))
    else:
        for k, v in sorted(big.items(), key=lambda kv: -len(kv[1])):
            for i in v:
                taken[i] = True
            rules.append(('re:"^{}[-_ ]?\\d{{2,}}" -> {}'.format(k, k), len(v),
                          "files referenced {}-nnn".format(k),
                          [names[i][0] for i in v[:4]]))

    # 2. names that begin with a date: by year / month
    dated = [i for i, (name, ext, mtime, size) in enumerate(names)
             if not taken[i] and _DATE_NAME.match(_stem(name, ext))]
    if len(dated) >= min_count:
        for i in dated:
            taken[i] = True
        rules.append(("re:^(20\\d\\d|19\\d\\d)[-_.]?(0[1-9]|1[0-2])[-_.]?"
                      "(0[1-9]|[12]\\d|3[01]) -> Dated/{1}/{1}-{2}", len(dated),
                      "names begin with a date - by year, then month",
                      [names[i][0] for i in dated[:4]]))

    # 3. leading words. Two-word prefixes first (more specific); when one
    #    first word heads several two-word groups they nest under it
    #    (Board/Minutes, Board/Agendas), otherwise the pair is one folder.
    two = {}
    one = {}
    for i, (name, ext, mtime, size) in enumerate(names):
        if taken[i]:
            continue
        w = _words(_stem(name, ext))
        if not w or w[0].isdigit():
            continue
        one.setdefault(w[0].lower(), []).append(i)
        if len(w) > 1 and not w[1].isdigit():
            two.setdefault((w[0].lower(), w[1].lower()), []).append(i)
    heads = {}
    for (a, b), idxs in two.items():
        if len(idxs) >= min_count and a not in STOP:
            heads.setdefault(a, []).append(b)
    for (a, b), idxs in sorted(two.items(), key=lambda kv: -len(kv[1])):
        idxs = [i for i in idxs if not taken[i]]
        if len(idxs) < min_count or a in STOP:
            continue
        if len(heads.get(a, [])) >= 2:
            folder = "{}/{}".format(KNOWN_PREFIX.get(a, _title(a)),
                                    KNOWN_PREFIX.get(b, _title(b)))
        elif a in KNOWN_PREFIX:
            folder = "{}/{}".format(KNOWN_PREFIX[a], _title(b))
        else:
            folder = KNOWN_PREFIX.get(a + " " + b) or "{} {}".format(
                _title(a), _title(b))
        pat = '"{} {}*"'.format(a, b)
        for i in idxs:
            taken[i] = True
        yrs = years_of(idxs)
        dest = folder + ("/{year}" if len(yrs) >= 3 and len(idxs) >= 3 * min_count
                         else "")
        rules.append(("{} -> {}".format(pat, dest), len(idxs),
                      "{:,} files start with '{} {}'".format(len(idxs), a, b),
                      [names[i][0] for i in idxs[:4]]))
    for a, idxs in sorted(one.items(), key=lambda kv: -len(kv[1])):
        idxs = [i for i in idxs if not taken[i]]
        if len(idxs) < min_count or a in STOP or len(a) < 2:
            continue
        folder = KNOWN_PREFIX.get(a, _title(a))
        for i in idxs:
            taken[i] = True
        yrs = years_of(idxs)
        dest = folder + ("/{year}" if len(yrs) >= 3 and len(idxs) >= 3 * min_count
                         else "")
        pat = '"{}*"'.format(a) if " " in a else "{}*".format(a)
        rules.append(("{} -> {}".format(pat, dest), len(idxs),
                      "{:,} files start with '{}'{}".format(
                          len(idxs), a, " (known kind)" if a in KNOWN_PREFIX
                          else ""),
                      [names[i][0] for i in idxs[:4]]))

    # 4. what is left, by type group (with a year split when the files span
    #    several years)
    by_group = {}
    for i, (name, ext, mtime, size) in enumerate(names):
        if taken[i]:
            continue
        by_group.setdefault(type_of(ext), []).append(i)
    for group, idxs in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        if group == "other" or len(idxs) < min_count:
            continue
        yrs = years_of(idxs)
        folder = TYPE_FOLDER[group]
        dest = folder + ("/{year}" if len(yrs) >= 3 and len(idxs) >= 3 * min_count
                         else "")
        for i in idxs:
            taken[i] = True
        rules.append(("type:{} -> {}".format(group, dest), len(idxs),
                      "{:,} {} files with no better home{}".format(
                          len(idxs), group,
                          " across {} years".format(len(yrs))
                          if "{year}" in dest else ""),
                      [names[i][0] for i in idxs[:4]]))
    left = sum(1 for t in taken if not t)
    other = by_group.get("other", [])
    if other and len(other) >= min_count:
        exts = {}
        for i in other:
            exts[names[i][1] or "(none)"] = exts.get(names[i][1] or "(none)", 0) + 1
        top = sorted(exts.items(), key=lambda kv: -kv[1])[:5]
        notes.append("{:,} files of types outside the groups ({}) - add an "
                     "ext: rule or a sweep".format(
                         len(other), ", ".join("{} x{}".format(e, c)
                                               for e, c in top)))
    if left:
        notes.append("{:,} of {:,} files would match none of these - a final "
                     "'* -> _Unsorted' sweeps them up, or leave them".format(
                         left, n))
    else:
        notes.append("every file is covered")
    return {"rules": rules, "notes": notes}


def suggestion_text(sug, header=True):
    lines = []
    if header:
        lines.append("# suggested by findex organise - edit freely; first "
                     "match wins")
    for text, count, reason, samples in sug["rules"]:
        lines.append("# {:,} files: {}   e.g. {}".format(
            count, reason, ", ".join(samples[:2])))
        lines.append(text)
    for note in sug["notes"]:
        lines.append("# note: " + note)
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Output: the plan and the resulting tree
# ----------------------------------------------------------------------------

def _count(node):
    return node["n"] + sum(_count(v) for v in node["folders"].values())


def _bytes(node):
    return node["b"] + sum(_bytes(v) for v in node["folders"].values())


def tree_lines(tree, prefix="", root_label="."):
    out = []
    if prefix == "":
        out.append("{}   ({:,} files placed, {})".format(
            root_label, _count(tree), findex.human(_bytes(tree))))
        for f in tree["files"][:8]:
            out.append("|-- " + f)
        if tree["n"] > 8:
            out.append("|-- ... {:,} more".format(tree["n"] - 8))
    names = sorted(tree["folders"])
    for i, k in enumerate(names):
        last = i == len(names) - 1
        node = tree["folders"][k]
        out.append("{}{} {}/   ({:,} files, {})".format(
            prefix, "`--" if last else "|--", k, _count(node),
            findex.human(_bytes(node))))
        sub = prefix + ("    " if last else "|   ")
        files = node["files"]
        for f in files[:8]:
            out.append("{}|-- {}".format(sub, f))
        if node["n"] > 8:
            out.append("{}|-- ... {:,} more".format(sub, node["n"] - 8))
        out.extend(tree_lines(node, sub))
    return out


def write_text(plan, out):
    out.write("\n".join(plan.summary_lines()) + "\n")
    if plan.issues:
        out.write("\nRule check\n")
        for level, line, msg in plan.issues:
            out.write("  {:<7} {}{}\n".format(
                level, "line {}: ".format(line) if line else "", msg))
    out.write("\nResulting tree\n")
    out.write("\n".join(tree_lines(plan.tree, root_label=plan.root)) + "\n")
    if plan.empty_after:
        out.write("\nFolders left empty ({:,})\n".format(len(plan.empty_after)))
        for f in plan.empty_after[:200]:
            out.write("  " + f + "\n")
    out.write("\nPlan\n")
    for path, size, mtime, target, action, rule, note in plan.rows:
        if action == "unmatched":
            continue
        line = "  {:<10} {}".format(action, path)
        if target and action not in ("unchanged",):
            line += "\n             -> " + target
        if note:
            line += "   [{}]".format(note)
        out.write(line + "\n")


def write_csv(plan, out):
    w = csv.writer(out)
    w.writerow(["action", "path", "target", "size", "rule line", "note"])
    for path, size, mtime, target, action, rule, note in plan.rows:
        w.writerow([action, path, target, size, rule or "", note])


def write_json(plan, out):
    json.dump({"root": plan.root, "options": plan.options,
               "counts": plan.counts, "bytes": plan.bytes,
               "by_dest": plan.by_dest, "empty_after": plan.empty_after,
               "issues": plan.issues, "tree": plan.tree,
               "rows": [{"path": p, "size": s, "target": t, "action": a,
                         "rule": r, "note": n}
                        for p, s, m, t, a, r, n in plan.rows]},
              out, indent=1, ensure_ascii=False)


def write_html(plan, out, cap=3000):
    e = html.escape
    w = out.write
    w("<!doctype html><html><head><meta charset='utf-8'><title>findex organise"
      " plan</title><style>body{font:14px/1.45 -apple-system,Segoe UI,"
      "Helvetica,Arial,sans-serif;margin:0;background:#f4f6f9;color:#1b1f27}"
      "main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}h1{font-size"
      ":22px;margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px}.sub{color:"
      "#5a6475}pre{background:#fff;border:1px solid #d9dee7;border-radius:8px;"
      "padding:12px;overflow:auto;font-size:12.5px}table{border-collapse:"
      "collapse;width:100%;background:#fff;border:1px solid #d9dee7;font-size:"
      "13px}td,th{padding:6px 10px;text-align:left;border-bottom:1px solid "
      "#eef1f5;vertical-align:top}th{background:#eef1f5}td.p{font-family:"
      "Consolas,Menlo,monospace;font-size:12px;word-break:break-all}.bad{color"
      ":#b42318}.warn{color:#8a5a00}@media (prefers-color-scheme:dark){body{"
      "background:#14161b;color:#e9ebf0}pre,table{background:#1c1f27;border-"
      "color:#333b49}th{background:#232834}td,th{border-color:#262b36}.sub{"
      "color:#98a2b3}}</style></head><body><main>")
    lines = plan.summary_lines()
    w("<h1>Organise: {}</h1><div class='sub'>{} &middot; {}</div>".format(
        e(plan.root), e(lines[0].split("(")[-1].rstrip(")")),
        time.strftime("%d %b %Y %H:%M")))
    w("<pre>{}</pre>".format(e("\n".join(lines[1:]))))
    if plan.issues:
        w("<h2>Rule check</h2><table><tr><th>Level</th><th>Line</th><th>"
          "Message</th></tr>")
        for level, line, msg in plan.issues:
            w("<tr class='{}'><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                "bad" if level == "error" else "warn" if level == "warning"
                else "", level, line or "", e(msg)))
        w("</table>")
    w("<h2>Resulting tree</h2><pre>{}</pre>".format(
        e("\n".join(tree_lines(plan.tree, root_label=plan.root)))))
    w("<h2>Destinations</h2><table><tr><th>Folder</th><th>Files</th><th>Size"
      "</th></tr>")
    for k, (n, b) in sorted(plan.by_dest.items()):
        w("<tr><td class='p'>{}</td><td>{:,}</td><td>{}</td></tr>".format(
            e(k), n, findex.human(b)))
    w("</table>")
    if plan.empty_after:
        w("<h2>Folders left empty ({:,})</h2><pre>{}</pre>".format(
            len(plan.empty_after), e("\n".join(plan.empty_after[:500]))))
    w("<h2>Plan</h2><table><tr><th>Action</th><th>File</th><th>Target</th>"
      "<th>Note</th></tr>")
    shown = 0
    for path, size, mtime, target, action, rule, note in plan.rows:
        if action == "unmatched":
            continue
        if shown >= cap:
            break
        shown += 1
        w("<tr class='{}'><td>{}</td><td class='p'>{}</td><td class='p'>{}</td>"
          "<td>{}</td></tr>".format(
              "bad" if action in ("collision", "error") else "",
              action, e(path), e(target), e(note)))
    w("</table></main></body></html>")


def export(plan, out_path, fmt=None):
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip(".").lower()
           or "txt").lower()
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        if fmt in ("html", "htm"):
            write_html(plan, out)
        elif fmt == "csv":
            write_csv(plan, out)
        elif fmt == "json":
            write_json(plan, out)
        else:
            write_text(plan, out)
    return fmt


# ----------------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------------

def apply(conn, plan, log=None, progress=None):
    """Carry the plan out. Returns dict(batch, moved, copied, removed,
    failed=[(path, error)])."""
    findex_rename.ensure_schema(conn)
    copy = bool(plan.options.get("copy"))
    batch = conn.execute("SELECT COALESCE(MAX(batch),0)+1 FROM renames"
                         ).fetchone()[0]
    res = {"batch": batch, "moved": 0, "copied": 0, "removed": 0, "failed": []}
    todo = [r for r in plan.rows if r[4] in ("move", "copy", "sweep")]
    cur = conn.cursor()
    cur.execute("BEGIN")
    now = time.time()
    made_dirs = set()
    base = plan.root.rstrip("\\/")

    def ensure_dir(tdir):
        """Create the target folder chain; record each NEW folder in the
        batch (op mkdir) and in the index, before anything moves into it."""
        if tdir in made_dirs:
            return
        sep = _sep_for(tdir)
        parts = tdir.rstrip(sep).split(sep)
        for k in range(len(parts)):
            p = sep.join(parts[:k + 1])
            if len(p) <= len(base) or not p.startswith(base) or p in made_dirs:
                continue
            existed = os.path.isdir(findex.lp(p))
            if not existed:
                os.mkdir(findex.lp(p))
                cur.execute("INSERT INTO renames(batch, ts, old_path, new_path, "
                            "is_dir, op) VALUES (?, ?, '', ?, 1, 'mkdir')",
                            (batch, now, p))
                findex.journal_add(cur, [(now, "added", p, None, 0, 1,
                                          "organise")])
            if not cur.execute("SELECT 1 FROM files WHERE path=?",
                               (p,)).fetchone():
                try:
                    st = os.stat(findex.lp(p))
                    cur.execute(findex.UPSERT, (p, _split(p)[1], "", 0,
                                                st.st_mtime, now, 0, "folder",
                                                None, 1))
                except OSError:
                    pass
            made_dirs.add(p)
        made_dirs.add(tdir)

    for i, (path, size, mtime, target, action, rule, note) in enumerate(todo):
        if progress and i % 50 == 0:
            progress(i, len(todo))
        tdir = _split(target)[0]
        try:
            ensure_dir(tdir)
            if os.path.exists(findex.lp(target)):
                raise FileExistsError("target appeared: " + target)
            if copy:
                shutil.copy2(findex.lp(path), findex.lp(target))
            else:
                try:
                    os.rename(findex.lp(path), findex.lp(target))
                except OSError:
                    shutil.move(findex.lp(path), findex.lp(target))
        except OSError as exc:
            res["failed"].append((path, str(exc)))
            if log:
                log("organise failed: {}: {}".format(path, exc))
            continue
        if copy:
            # a new row that knows what its source knew - text and hashes too
            cur.execute(
                "INSERT OR IGNORE INTO files(path, name, ext, size, mtime, "
                "indexed, chars, status, error, is_dir, phash, fhash, kind, "
                "simhash) SELECT ?, name, ext, size, mtime, ?, chars, status, "
                "error, 0, phash, fhash, kind, simhash FROM files WHERE path=?",
                (target, now, path))
            row = cur.execute("SELECT id FROM files WHERE path=?",
                              (target,)).fetchone()
            src = cur.execute("SELECT id FROM files WHERE path=?",
                              (path,)).fetchone()
            if row and src:
                cur.execute("INSERT OR IGNORE INTO docs(rowid, body) SELECT ?, "
                            "body FROM docs WHERE rowid=?", (row[0], src[0]))
            cur.execute("INSERT INTO renames(batch, ts, old_path, new_path, "
                        "is_dir, op) VALUES (?, ?, ?, ?, 0, 'copy')",
                        (batch, now, path, target))
            findex.journal_add(cur, [(now, "added", target, None, size, 0,
                                      "organise")])
            res["copied"] += 1
        else:
            findex_rename._rename_in_index(cur, path, target, False)
            cur.execute("INSERT INTO renames(batch, ts, old_path, new_path, "
                        "is_dir, op) VALUES (?, ?, ?, ?, 0, 'move')",
                        (batch, now, path, target))
            findex.journal_add(cur, [(now, "renamed", target, path, size, 0,
                                      "organise")])
            res["moved"] += 1
        if (res["moved"] + res["copied"]) % 200 == 0:
            conn.commit()
            cur.execute("BEGIN")
    conn.commit()
    if not copy and plan.options.get("remove_empty"):
        cur.execute("BEGIN")
        for folder in sorted(plan.empty_after, key=len, reverse=True):
            try:
                if not os.listdir(findex.lp(folder)):
                    os.rmdir(findex.lp(folder))
                    cur.execute("DELETE FROM files WHERE path=?", (folder,))
                    cur.execute("INSERT INTO renames(batch, ts, old_path, "
                                "new_path, is_dir, op) VALUES (?, ?, ?, '', 1, "
                                "'rmdir')", (batch, now, folder))
                    findex.journal_add(cur, [(now, "deleted", folder, None, 0,
                                              1, "organise")])
                    res["removed"] += 1
            except OSError as exc:
                if log:
                    log("could not remove {}: {}".format(folder, exc))
        conn.commit()
    if progress:
        progress(len(todo), len(todo))
    return res


# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------

def templates(conn):
    try:
        return conn.execute("SELECT name, rules, options, saved FROM "
                            "organise_templates ORDER BY name").fetchall()
    except Exception:                                          # noqa: BLE001
        return []


def save_template(conn, name, rules, options):
    conn.executescript(TEMPLATES_SCHEMA)
    conn.execute("INSERT INTO organise_templates(name, rules, options, saved) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                 "rules=excluded.rules, options=excluded.options, "
                 "saved=excluded.saved",
                 (name, rules, json.dumps(options or {}), time.time()))
    conn.commit()


def delete_template(conn, name):
    conn.executescript(TEMPLATES_SCHEMA)
    conn.execute("DELETE FROM organise_templates WHERE name=?", (name,))
    conn.commit()


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_organise(args):
    if args.undo is not None:
        conn = findex.open_db(args.db)
        batch, done, failed = findex_rename.undo(conn, args.undo or None,
                                                 log=print)
        conn.close()
        print("Undid batch {}: {:,} reversed{}".format(
            batch, done, ", {:,} failed".format(len(failed)) if failed else "")
            if batch else "Nothing to undo.")
        return 1 if failed else 0
    if args.list_templates:
        conn = findex.open_db_ro(args.db)
        rows = templates(conn)
        conn.close()
        for name, rules, options, saved in rows:
            print("{:<24} {} rule(s)   saved {}".format(
                name, len([l for l in rules.splitlines()
                           if l.strip() and not l.startswith("#")]),
                time.strftime("%Y-%m-%d", time.localtime(saved or 0))))
        if not rows:
            print("No templates saved.")
        return 0
    if not args.root:
        sys.stderr.write("Give the folder to organise.\n")
        return 2
    root = args.root
    if not _is_abs(root):
        root = os.path.abspath(root)
    conn = findex.open_db_ro(args.db)
    files, folders = files_under(conn, root)
    if args.suggest:
        sug = suggest(files)
        text = suggestion_text(sug)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print("Wrote {} ({} rule(s))".format(args.out, len(sug["rules"])))
        else:
            print(text)
        conn.close()
        return 0
    rules_text = None
    if args.rules:
        with open(args.rules, encoding="utf-8") as fh:
            rules_text = fh.read()
    elif args.template:
        row = conn.execute("SELECT rules FROM organise_templates WHERE name=?",
                           (args.template,)).fetchone()
        if not row:
            sys.stderr.write("No template called {!r}.\n".format(args.template))
            return 2
        rules_text = row[0]
    elif args.rules_text:
        rules_text = args.rules_text.replace("\\n", "\n")
    if rules_text is None:
        sys.stderr.write("Give --rules FILE, --rules-text, --template NAME, "
                         "or --suggest.\n")
        return 2
    options = {"copy": args.copy, "sweep": args.sweep,
               "skip_identical": args.skip_identical,
               "remove_empty": args.remove_empty}
    plan = build_plan(conn, root, rules_text, options, files, folders)
    conn.close()
    if args.save_template:
        c = findex.open_db(args.db)
        save_template(c, args.save_template, rules_text, options)
        c.close()
        print("Saved template {!r}".format(args.save_template))
    if args.out:
        fmt = export(plan, args.out, args.format)
        print("Wrote {} ({})".format(args.out, fmt))
    print("\n".join(plan.summary_lines()))
    if plan.issues:
        print("\nRule check:")
        for level, line, msg in plan.issues:
            print("  {:<7} {}{}".format(level, "line {}: ".format(line)
                                        if line else "", msg))
    if not args.out:
        print("\nResulting tree:")
        print("\n".join(tree_lines(plan.tree, root_label=root)[:args.limit]))
    if not args.apply:
        print("\nDry run - add --apply to carry it out.")
        return 0
    if any(i[0] == "error" for i in plan.issues) and not args.force:
        print("\nNot applied: the rules have errors (fix them, or --force to "
              "apply the rows that can be placed).")
        return 1
    conn = findex.open_db(args.db)
    res = apply(conn, plan, log=print)
    conn.close()
    print("\nBatch {}: {:,} moved, {:,} copied, {:,} empty folder(s) removed"
          "{}.  Undo with:  findex organise --undo {}".format(
              res["batch"], res["moved"], res["copied"], res["removed"],
              ", {:,} failed".format(len(res["failed"])) if res["failed"]
              else "", res["batch"]))
    return 1 if res["failed"] else 0


def add_commands(sub):
    p = sub.add_parser("organise", aliases=["organize"],
                       help="sort the files under a folder into subfolders by "
                            "rules - suggest, preview, apply, undo")
    p.add_argument("root", nargs="?", help="the folder to organise")
    p.add_argument("--rules", metavar="FILE", help="rules file (one per line)")
    p.add_argument("--rules-text", metavar="TEXT",
                   help="rules inline, \\n between them")
    p.add_argument("--template", metavar="NAME", help="a saved rule set")
    p.add_argument("--suggest", action="store_true",
                   help="propose rules from the names (write with -o)")
    p.add_argument("--copy", action="store_true",
                   help="copy files into place instead of moving them")
    p.add_argument("--sweep", nargs="?", const="_Unsorted", metavar="FOLDER",
                   help="files no rule matches go here (default _Unsorted)")
    p.add_argument("--skip-identical", action="store_true",
                   help="an identical file already at the target counts as "
                        "done (needs hashes: findex hash)")
    p.add_argument("--remove-empty", action="store_true",
                   help="remove folders the moves leave empty")
    p.add_argument("--apply", action="store_true",
                   help="carry the plan out (default: dry run)")
    p.add_argument("--force", action="store_true",
                   help="apply even when the rule check reports errors")
    p.add_argument("--save-template", metavar="NAME",
                   help="save these rules and options under a name")
    p.add_argument("--list-templates", action="store_true")
    p.add_argument("-o", "--out", help="write the plan / suggestions to a file "
                   "(.html/.txt/.csv/.json)")
    p.add_argument("-f", "--format", choices=("html", "txt", "csv", "json"))
    p.add_argument("-n", "--limit", type=int, default=120,
                   help="tree lines to print (default 120)")
    p.add_argument("--undo", nargs="?", const=0, type=int, metavar="BATCH",
                   help="reverse the latest batch (or the one given)")
    p.set_defaults(func=cmd_organise)


if __name__ == "__main__":
    sys.exit(findex.main())
