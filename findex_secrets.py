#!/usr/bin/env python3
"""
findex_secrets - passwords, keys and tokens sitting in indexed files.

    findex secrets                    scan every document with extracted text
    findex secrets --under D:\\Shared  one part of the index
    findex secrets -o secrets.csv     save the findings

Works on the TEXT findex already extracted, so nothing on disk is opened.
The FTS index narrows the documents worth reading (only those that mention
a password-ish word or carry a key-shaped token), then each candidate's
text is run through the patterns below. Matched values are shown MASKED -
the point is to find the file, not to copy the secret into a report.

Not a compliance tool and not exhaustive: it finds the common shapes -
cloud API keys, private key blocks, connection strings, `password = ...`
lines in configs, scripts and spreadsheets - which is where the damage is.
"""

from __future__ import annotations

import csv
import re
import sys

import findex

# (label, compiled pattern, group holding the secret value or 0)
PATTERNS = [
    ("AWS access key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
    ("AWS secret key", re.compile(
        r"(?i)aws.{0,25}?(?:secret|key)[^\n]{0,10}?[=:]\s*[\"']?"
        r"([0-9A-Za-z/+]{40})(?![0-9A-Za-z/+])"), 1),
    ("Private key block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"), 0),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255})\b"), 1),
    ("GitHub fine-grained token", re.compile(
        r"\b(github_pat_[A-Za-z0-9_]{60,})\b"), 1),
    ("Slack token", re.compile(r"\b(xox[baprs]-[0-9A-Za-z-]{10,})\b"), 1),
    ("Slack webhook", re.compile(
        r"(hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/"
        r"[A-Za-z0-9]+)"), 1),
    ("Google API key", re.compile(r"\b(AIza[0-9A-Za-z\-_]{35})\b"), 1),
    ("Stripe live key", re.compile(r"\b([sr]k_live_[0-9a-zA-Z]{20,})\b"), 1),
    ("OpenAI-style key", re.compile(r"\b(sk-(?:proj-|ant-)?[A-Za-z0-9\-_]{32,})\b"), 1),
    ("SendGrid key", re.compile(
        r"\b(SG\.[A-Za-z0-9_-]{20,24}\.[A-Za-z0-9_-]{40,45})\b"), 1),
    ("npm token", re.compile(r"\b(npm_[A-Za-z0-9]{36})\b"), 1),
    ("JSON web token", re.compile(
        r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"),
     1),
    ("Azure storage key", re.compile(
        r"AccountKey=([A-Za-z0-9+/=]{60,})"), 1),
    ("Azure SAS token", re.compile(
        r"[?&]sig=([A-Za-z0-9%+/=]{40,})"), 1),
    ("Connection string with password", re.compile(
        r"(?i)(?:server|data source|host|uid|user id)=[^;\n]+;[^\n]*?"
        r"(?:password|pwd)=([^;\s\"']{3,})"), 1),
    ("Credentials in URL", re.compile(
        r"\b[a-z][a-z0-9+.-]{1,15}://[^/\s:@]{1,64}:([^@\s/]{3,})@[^\s/]+"), 1),
    ("Password assignment", re.compile(
        r"(?i)\b(?:password|passwd|passphrase|pwd|secret|api[_-]?key|"
        r"apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"private[_-]?key|bearer)\b\s*(?:[:=]|is|:=)\s*[\"']?"
        r"([^\s\"',;<>]{6,})"), 1),
]

# Values that are obviously not a real secret.
_PLACEHOLDER = re.compile(
    r"^(?:[*x#_\-.]+|\$\{.*\}|\$\(.*\)|%.*%|<.*>|\{\{.*\}\}|\[.*\]|"
    r"password|passwd|secret|changeme|change_me|example|sample|null|none|"
    r"true|false|yes|no|required|optional|redacted|hidden|hunter2|"
    r"your[_-]?\w+|xxx+|\.{3,}|123456|12345678|admin|test|password1?)$",
    re.I)

# What to ask the FTS index for before running the patterns: any document
# that mentions a password-ish word or carries a key-shaped token.
FTS_QUERY = ('password OR passwd OR passphrase OR pwd OR secret OR token OR '
             '"private key" OR apikey OR AKIA* OR xox* OR AIza* OR sk* OR '
             'eyJ* OR AccountKey OR github OR npm* OR bearer OR sig OR '
             'hooks OR accesstoken OR pat')


def mask(value):
    """Enough to recognise the value in the file, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 6:
        return value[0] + "*" * (len(value) - 1)
    keep = 4 if len(value) > 16 else 2
    return "{}{}  ({} chars)".format(value[:keep], "*" * min(12, len(value) - keep),
                                     len(value))


def find_in_text(text):
    """[(label, masked_value_or_context, count)] for one document."""
    out = {}
    seen_values = set()
    for label, rx, grp in PATTERNS:
        n = 0
        first = None
        for m in rx.finditer(text):
            value = m.group(grp) if grp else m.group(0)
            if grp and _PLACEHOLDER.match(value):
                continue
            if grp and label == "Password assignment":
                # a "password: see the vault" sentence is not a secret;
                # require something that looks like a value, not a word -
                # and a value a more specific pattern already reported
                # (a connection string's password) is not news
                if value.isalpha() and value.islower() and len(value) < 12:
                    continue
                if value in seen_values:
                    continue
            if grp:
                seen_values.add(value)
            n += 1
            if first is None:
                first = mask(value) if grp else "present"
        if n:
            out[label] = (first, n)
    return [(label, snippet, n) for label, (snippet, n) in out.items()]


def scan(conn, under=None, limit_docs=0):
    """[(path, label, masked_snippet, count)] over every document with
    extracted text, most-findings-first. Read-only."""
    where, params = "", []
    if under:
        sep = "\\" if "\\" in under or (len(under) > 1 and under[1] == ":") \
            else "/"
        where = " AND f.path LIKE ? ESCAPE '!'"
        params = [findex.like_escape(under.rstrip("\\/")) + sep + "%"]
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT d.rowid FROM docs d JOIN files f ON f.id = d.rowid "
            "WHERE docs MATCH ?{}".format(where), [FTS_QUERY] + params)]
    except Exception:                                          # noqa: BLE001
        # FTS query syntax rejected (very old SQLite): read every document
        ids = [r[0] for r in conn.execute(
            "SELECT f.id FROM files f WHERE f.chars>0{}".format(where), params)]
    if limit_docs:
        ids = ids[:limit_docs]
    out = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        marks = ",".join("?" * len(chunk))
        for path, body in conn.execute(
                "SELECT f.path, d.body FROM docs d JOIN files f ON f.id = d.rowid "
                "WHERE d.rowid IN ({})".format(marks), chunk):
            for label, snippet, n in find_in_text(body or ""):
                out.append((path, label, snippet, n))
    out.sort(key=lambda r: (-r[3], r[0], r[1]))
    return out


def cmd_secrets(args):
    conn = findex.open_db_ro(args.db)
    try:
        rows = scan(conn, under=args.under)
    finally:
        conn.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["path", "finding", "value (masked)", "count"])
            w.writerows(rows)
        print("Wrote {} - {:,} finding(s) in {:,} file(s)".format(
            args.out, len(rows), len({r[0] for r in rows})))
        return 0
    last = None
    for path, label, snippet, n in rows:
        if path != last:
            last = path
            print("\n" + path)
        print("    {:<34} {}{}".format(label, snippet,
                                       "   x{}".format(n) if n > 1 else ""))
    print("\n{:,} finding(s) in {:,} file(s)".format(
        len(rows), len({r[0] for r in rows})) if rows
        else "Nothing that looks like a secret in the extracted text.")
    return 0


def add_commands(sub):
    p = sub.add_parser("secrets", help="passwords, API keys, tokens and "
                       "private keys sitting in indexed files (values masked)")
    p.add_argument("--under", metavar="FOLDER", help="only beneath this folder")
    p.add_argument("-o", "--out", help="write the findings to this CSV")
    p.set_defaults(func=cmd_secrets)


if __name__ == "__main__":
    sys.exit(findex.main())
