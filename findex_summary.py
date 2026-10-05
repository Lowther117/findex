#!/usr/bin/env python3
"""
findex_summary - what is in a folder, without opening anything.

    findex summarise D:\\Shared            sort one folder's files into sections
    findex summarise                       ...or everything in the index
    findex summarise D:\\Shared --show     the sections found last time
    findex summarise --section 12          the files in one section
    findex summarise D:\\Shared -o out.html   export (.html .csv .json .txt)
    findex summarise D:\\Shared --ai       also have a local AI model name and
                                           describe each section
    findex summarise --ai-status           is a local model available?
    findex summarise --ai-models           the small, fast models suggested

Two layers, both entirely on this computer:

1. The offline pass. It reads the text findex ALREADY extracted (no file on
   disk is opened) and gives every readable file a card - what kind of
   document it is, its title, its key phrases, the dates / amounts / emails
   in it and a two-sentence gist lifted from the text - then groups the files
   by what they are about into labelled sections. Files with no text are
   grouped by type and folder. Plain word statistics, no model, minutes for
   hundreds of thousands of files; cards are kept, so a second run (or a run
   on a sub-folder) only reads what changed.

2. Written summaries, on demand. A language model running locally through
   Ollama writes a proper summary of a file, or a title and description for
   each section. Seconds per file, so it is for the files or sections you
   pick - not the whole index. Nothing leaves the machine.

Everything is scoped to a folder: a run covers one folder (and what is below
it) and is stored under that folder, so different folders keep their own
sections side by side.

Search gains two filters from this:  section:insurance  /  section:#12  and
doctype:invoice .
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from array import array
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations

import findex

CARD_VERSION = 1           # bump to have every card rebuilt on the next run
SAMPLE_CHARS = 24000       # text read per file for its card
HEAD_CHARS = 4000          # ...of which this much decides the document type
GIST_CHARS = 6000          # ...and this much is searched for gist sentences
MAX_UNI = 30               # words kept per file
MAX_BI = 10                # two-word phrases kept per file
VEC_TERMS = 24             # terms in a file's vector when grouping
BATCH = 200                # files per worker job
INLINE_BELOW = 600         # fewer files than this: no worker processes
MIN_PROSE = 12             # fewer readable documents: group by type instead
SEED_SAMPLE = 60000        # documents looked at when choosing section seeds
MIN_SIM = 0.08             # a document this unlike every section is unsorted
SPLIT_MIN = 200            # a type group this big is split by sub-folder

DETAILS = {"low": (0.6, 40), "normal": (1.0, 80), "high": (1.8, 150)}

AI_URL = os.environ.get("FINDEX_AI_URL", "http://127.0.0.1:11434")
# Small, fast models worth offering (tag, download size, what it is like).
# Sizes are Ollama's own figures (checked Oct 2026). All run on a laptop
# CPU; the first is what --ai-setup fetches when nothing is installed.
AI_MODELS = (
    ("gemma3:1b", "815 MB", "quickest - fine for a few plain sentences"),
    ("qwen3.5:0.8b", "1.2 GB", "newest of the tiny models"),
    ("llama3.2:1b", "1.3 GB", "quick, plain summaries"),
    ("granite4:micro", "2.1 GB", "steadier on business documents"),
    ("gemma3:4b", "3.3 GB", "best of the small ones, about 3x slower"),
)
AI_MODEL = AI_MODELS[0][0]
AI_MODELS_TOTAL = "8.7 GB"   # all of the above together
# When several models are installed and none was chosen: smallest first.
AI_PREFERRED = tuple(m for m, _, _ in AI_MODELS) + (
    "llama3.2", "gemma3", "qwen3.5", "qwen3", "granite4", "phi4-mini",
    "qwen2.5", "phi3", "mistral")
AI_FILE_CHARS = 5000       # text sent for one file's summary
AI_TAIL_CHARS = 1000       # ...plus the end of a long one
AI_CONTEXT = 4096          # tokens of context asked for - small is fast
AI_KEEP_ALIVE = "15m"      # keep the model loaded between requests


# ----------------------------------------------------------------------------
# Document types
# ----------------------------------------------------------------------------

DOCTYPES = (
    ("invoice", "Invoice"), ("receipt", "Receipt / order"),
    ("statement", "Statement"), ("payslip", "Payslip"),
    ("contract", "Contract / agreement"), ("policy", "Policy / procedure"),
    ("letter", "Letter"), ("cv", "CV"), ("minutes", "Minutes / agenda"),
    ("report", "Report"), ("form", "Form / application"),
    ("manual", "Manual / guide"), ("certificate", "Certificate"),
    ("presentation", "Presentation"), ("spreadsheet", "Spreadsheet"),
    ("email", "Email"), ("book", "Book"), ("notes", "Notes / text"),
    ("webpage", "Web page"), ("scan", "Scanned image"),
    ("document", "Document"), ("code", "Code / script"),
    ("data", "Data / config"), ("log", "Log"), ("audio", "Audio / video"),
    ("archive", "Archive"), ("other", "Other"),
)
DOCTYPE_LABEL = dict(DOCTYPES)
# Types whose text is not prose: grouped by kind, never by topic.
NON_PROSE = frozenset(("code", "data", "log", "audio", "archive"))

# (type, file-name pattern, [(points, phrases, regex or None)]). A name
# match is worth 3; the type with the most points wins and needs 3 to count
# at all. A clue scores when any of its phrases is in the first screenful of
# text (lower-cased) - plain substring tests, which is what keeps this fast
# across hundreds of thousands of files - and, where a regex is given, that
# matches too (the phrases then only decide whether it is worth running).
_RULES = (
    ("invoice", r"\binv(oice)?[\s_\-.\d]|invoice",
     [(2, ("invoice",), r"\binvoice\b"),
      (2, ("invoice no", "invoice number", "invoice date", "invoice #"), None),
      (2, ("amount due", "total due", "balance due", "payment terms",
           "bill to"), None),
      (1, ("vat", "subtotal", "sub-total"), r"\bvat\b|sub-?total")]),
    ("receipt", r"receipt|order[\s_\-]?confirm",
     [(2, ("receipt",), r"\breceipt\b"),
      (2, ("order number", "order confirmation", "order summary",
           "thank you for your order", "thank you for your purchase",
           "thank you for your payment"), None),
      (1, ("payment received", "amount paid", "paid in full"), None)]),
    ("statement", r"statement",
     [(3, ("statement of account", "statement period", "statement date",
           "account statement", "bank statement"), None),
      (2, ("sort code", "opening balance", "closing balance",
           "balance brought forward"), None)]),
    ("payslip", r"pay[\s_\-]?slip|\bp60\b|\bp45\b",
     [(3, ("payslip", "pay slip"), None),
      (2, ("net pay", "gross pay"), None),
      (2, ("tax code", "national insurance", "employee no",
           "employee number"), None)]),
    ("contract", r"contract|agreement|tenancy|lease|\bnda\b|terms",
     [(2, ("this agreement", "tenancy agreement", "terms and conditions"),
       None),
      (2, ("the parties", "hereinafter", "in witness whereof",
           "governing law"), None),
      (1, ("hereby", "shall not", "termination"), None)]),
    ("policy", r"policy|procedure|\bsop\b",
     [(2, ("this policy", "policy statement", "this procedure"), None),
      (2, ("review date", "version control", "document control"), None),
      (1, ("scope", "purpose", "responsibilities"),
       r"\bscope\b|\bpurpose\b|responsibilities")]),
    ("cv", r"\bcv\b|resume|curriculum",
     [(4, ("curriculum vitae",), None),
      (2, ("work experience", "employment history",
           "professional experience", "career history"), None),
      (2, ("references available",), None),
      (1, ("qualifications", "skills"), r"\bqualifications\b|\bskills\b")]),
    ("minutes", r"minutes|agenda",
     [(3, ("minutes of", "meeting minutes"), None),
      (2, ("apologies", "attendees", "action points", "matters arising",
           "any other business"), None),
      (2, ("agenda",), r"\bagenda\b")]),
    ("certificate", r"certificate|\bcert\b",
     [(3, ("certificate of", "this is to certify", "certify that",
           "certifies that"), None),
      (2, ("has completed", "has successfully completed", "awarded to"),
       None)]),
    ("manual", r"manual|guide|instructions|handbook|readme",
     [(3, ("user manual", "user guide", "instruction manual", "quick start",
           "getting started"), None),
      (1, ("troubleshooting", "installation", "safety information",
           "safety instructions", "warranty"), None),
      (1, ("step ",), r"\bstep \d")]),
    ("form", r"\bform\b|application",
     [(2, ("application form", "please complete", "block capitals"), None),
      (1, ("date of birth", "tick the", "tick one", "tick all",
           "signature"), None)]),
    ("letter", r"letter",
     [(2, ("dear ",), r"(^|\n)\s*dear\s"),
      (2, ("yours sincerely", "yours faithfully", "yours truly",
           "kind regards", "best regards"), None)]),
    ("report", r"report",
     [(2, ("executive summary",), None),
      (1, ("table of contents", "findings"),
       r"table of contents|\bfindings\b"),
      (1, ("recommendations", "conclusion"),
       r"recommendations|\bconclusions?\b"),
      (1, ("introduction", "appendix"), r"\bintroduction\b|\bappendix\b")]),
)
_RULES = tuple((dt, re.compile(name_rx),
                tuple((pts, phrases, re.compile(rx) if rx else None)
                      for pts, phrases, rx in clues))
               for dt, name_rx, clues in _RULES)

_CODE_EXTS = frozenset((".py", ".js", ".ts", ".css", ".c", ".h", ".cpp",
                        ".cs", ".java", ".sql", ".ps1", ".bat", ".cmd",
                        ".sh", ".lua", ".gd"))
_DATA_EXTS = frozenset((".json", ".xml", ".yml", ".yaml", ".ini", ".cfg",
                        ".conf", ".csv", ".tsv"))
_SHEET_EXTS = frozenset((".xlsx", ".xlsm", ".xls", ".ods"))
_SLIDE_EXTS = frozenset((".pptx", ".pptm", ".ppt", ".odp"))


def doctype_of(name, ext, head):
    """What kind of document this is: the extension settles the obvious
    ones, the first screenful of text decides the rest."""
    if ext in _CODE_EXTS:
        return "code"
    if ext in _DATA_EXTS:
        return "data"
    if ext == ".log":
        return "log"
    if ext in (".zip", ".cbz"):
        return "archive"
    if ext in findex.AUDIO_EXTS:
        return "audio"
    if ext in (".eml", ".msg"):
        return "email"
    low = name.lower()
    best, best_pts = None, 0
    for dt, name_rx, clues in _RULES:
        pts = 3 if name_rx.search(low) else 0
        for worth, phrases, rx in clues:
            for ph in phrases:
                if ph in head:
                    if rx is None or rx.search(head):
                        pts += worth
                    break
        if pts > best_pts:                     # ties: the earlier rule
            best, best_pts = dt, pts
    if best_pts >= 3:
        return best
    if ext in _SLIDE_EXTS:
        return "presentation"
    if ext in _SHEET_EXTS:
        return "spreadsheet"
    if ext == ".epub":
        return "book"
    if ext in (".html", ".htm"):
        return "webpage"
    if ext in (".txt", ".md"):
        return "notes"
    if ext in findex.IMAGE_EXTS:
        return "scan"
    return "document"


# ----------------------------------------------------------------------------
# Words
# ----------------------------------------------------------------------------

STOP = frozenset("""
a about above after again against all am an and any are aren as at be because
been before being below between both but by can cannot could couldn did didn
do does doesn doing don down during each few for from further had hadn has
hasn have haven having he her here hers herself him himself his how if in
into is isn it its itself just ll me more most mustn my myself no nor not now
of off on once only or other our ours ourselves out over own re same shan she
should shouldn so some such than that the their theirs them themselves then
there these they this those through to too under until up ve very was wasn we
were weren what when where which while who whom why will with won would
wouldn you your yours yourself yourselves
also one two three four five six seven eight nine ten first second third new
may might must shall per via etc within without upon using used use please
see page pages www http https com org net html htm pdf doc docx jpg png nbsp
amp mailto tel fax email mail date dated ref number total name title file
files copy yes none null true false get got make made well much many like
including include includes however therefore thus whether able said says say
let need needs want back still even every around another since always never
often already rather quite
january february march april june july august september october november
december jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday
wednesday thursday friday saturday sunday mon tue wed thu fri sat sun
""".split())

# a word: starts with a letter, 3-24 letters/digits, whole word only (so a
# 60-character run of base64 is not chopped into "words")
_WORD = re.compile(r"\b[^\W\d_][^\W_]{2,23}\b")


def term_counts(low):
    """(top words, top two-word phrases, all word counts) of lower-cased
    text. Phrases need to occur twice to count."""
    toks = _WORD.findall(low)
    uni = Counter(toks)
    for w in STOP.intersection(uni):
        del uni[w]
    bi = []
    if len(toks) > 1:
        for (a, b), c in Counter(zip(toks, toks[1:])).most_common(400):
            if c < 2:
                break
            if a == b or a in STOP or b in STOP:
                continue
            bi.append((a + " " + b, c))
            if len(bi) >= MAX_BI:
                break
    return uni.most_common(MAX_UNI), bi, uni


def pack_terms(pairs):
    return "|".join("{}:{}".format(t, c) for t, c in pairs)


def unpack_terms(text):
    out = []
    if text:
        for part in text.split("|"):
            t, _, c = part.rpartition(":")
            if t:
                try:
                    out.append((t, int(c)))
                except ValueError:
                    pass
    return out


# ----------------------------------------------------------------------------
# One file's card
# ----------------------------------------------------------------------------

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"))}
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|"
        r"july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)")
# Every pattern here starts at a digit or a rare symbol, so the regex engine
# skips almost all of the text: a date written in words is found from its
# YEAR, then the day and month are read from the few characters before it.
_NUMDATE_RX = re.compile(
    r"\b(?:(?P<y1>(?:19|20)\d\d)-(?P<m1>\d\d)-(?P<d1>\d\d)"
    r"|(?P<d2>\d{1,2})[/.\-](?P<m2>\d{1,2})[/.\-](?P<y2>(?:19|20)?\d\d))\b")
_YEAR_RX = re.compile(r"\b(?:19|20)\d\d\b")
_BEFORE_YEAR = re.compile(
    r"(?:\b(?P<d3>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m3>" + _MON + r")\.?,?"
    r"|\b(?P<m4>" + _MON + r")\.?\s+(?P<d4>\d{1,2})(?:st|nd|rd|th)?,?)\s+$")
_SYMBOL_MONEY = re.compile(r"[£$€]\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?"
                           r"|[£$€]\s?\d+(?:\.\d{2})?")
_CODE_MONEY = re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\s?(?:GBP|USD|EUR)\b")
_EMAIL_AT = re.compile(r"[\w.+\-]{1,64}@[\w\-]+(?:\.[\w\-]+)+")
_SITE_RX = re.compile(r"(?:https?://|www\.)([\w\-]+(?:\.[\w\-]+)+)")
_POSTCODE_RX = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b")
_REF_LABELS = ("ref", "invoice", "order", "policy", "claim", "case", "quote",
               "ticket", "booking")
_REF_RX = re.compile(
    r"\b(?:ref(?:erence)?|invoice|order|policy|claim|case|quote|ticket|"
    r"booking)\s*(?:no\.?|number|num|#)?\s*[:#]?\s*"
    r"((?=[a-z0-9/\-]{0,20}\d)[a-z0-9][a-z0-9/\-]{3,20})\b")


def _iso(y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
    except (TypeError, ValueError):
        return None
    if y < 100:
        y += 2000 if y < 70 else 1900
    if not (1950 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return "{:04d}-{:02d}-{:02d}".format(y, m, d)


def _uniq(seq, limit):
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
            if len(out) >= limit:
                break
    return out


def entities_of(sample, low=None):
    """The dates, amounts, emails, sites, postcodes and reference numbers in
    a piece of text - what you would scan a document for to know which one
    it is. Day/month order is read the British way."""
    if low is None:
        low = sample.lower()
    out = {}
    dates = []
    for m in _NUMDATE_RX.finditer(low):
        g = m.groupdict()
        iso = _iso(g["y1"], g["m1"], g["d1"]) if g["y1"] \
            else _iso(g["y2"], g["m2"], g["d2"])
        if iso:
            dates.append((m.start(), iso))
            if len(dates) > 40:
                break
    for n, m in enumerate(_YEAR_RX.finditer(low)):
        if n > 60:
            break
        b = _BEFORE_YEAR.search(low, max(0, m.start() - 24), m.start())
        if b:
            g = b.groupdict()
            iso = _iso(m.group(0), _MONTHS[g["m3"][:3]], g["d3"]) if g["m3"] \
                else _iso(m.group(0), _MONTHS[g["m4"][:3]], g["d4"])
            if iso:
                dates.append((b.start(), iso))
    if dates:
        out["dates"] = _uniq((d for _, d in sorted(dates)), 5)
    money = []
    if "£" in sample or "$" in sample or "€" in sample:
        money += [m.group(0).replace(" ", "")
                  for m in _SYMBOL_MONEY.finditer(sample)]
    if "GBP" in sample or "USD" in sample or "EUR" in sample:
        money += [m.group(0) for m in _CODE_MONEY.finditer(sample)]
    if money:
        def val(s):
            try:
                return float(re.sub(r"[^\d.]", "", s))
            except ValueError:
                return 0.0
        out["amounts"] = sorted(_uniq(money, 40), key=val, reverse=True)[:4]
    if "@" in low:
        emails, at = [], low.find("@")
        while at >= 0 and len(emails) < 12:
            m = _EMAIL_AT.search(low, max(0, at - 64), at + 120)
            if m and m.start() <= at < m.end():
                emails.append(m.group(0).lstrip(".+-"))
            at = low.find("@", at + 1)
        emails = _uniq(emails, 4)
        if emails:
            out["emails"] = emails
    if "http" in low or "www." in low:
        sites = _uniq((m.group(1) for m in _SITE_RX.finditer(low)), 3)
        if sites:
            out["sites"] = sites
    codes = _uniq((re.sub(r"\s", "", m.group(0)) for m in
                   _POSTCODE_RX.finditer(sample)), 3)
    if codes:
        out["postcodes"] = [c[:-3] + " " + c[-3:] for c in codes]
    if any(label in low for label in _REF_LABELS):
        refs = _uniq((m.group(1).upper() for m in _REF_RX.finditer(low)), 3)
        if refs:
            out["refs"] = refs
    return out


ENTITY_LABELS = (("dates", "Dates"), ("amounts", "Amounts"),
                 ("refs", "References"), ("emails", "Emails"),
                 ("sites", "Sites"), ("postcodes", "Postcodes"))


def entities_text(raw, sep="\n"):
    """The stored entities as readable lines."""
    try:
        ent = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return ""
    return sep.join("{}: {}".format(label, ", ".join(ent[key]))
                    for key, label in ENTITY_LABELS if ent.get(key))


_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


_NOT_PROSE = re.compile(r"[^A-Za-z\u00c0-\u024f ]")


def _prose_line(s, lo, hi):
    """Is this a line of words (rather than a table row, a number, a URL)?"""
    if not lo <= len(s) <= hi:
        return False
    return len(_NOT_PROSE.findall(s)) <= 0.3 * len(s)


def title_of(name, ext, doctype, sample):
    """The document's own title, when it has one worth showing."""
    if doctype in ("code", "data", "log", "archive"):
        return ""
    lines = [ln.strip() for ln in sample[:3000].split("\n")]
    if doctype == "email":
        for ln in lines[:8]:
            if ln.lower().startswith("subject:"):
                return ln[8:].strip()[:140]
        return ""
    if doctype == "audio":
        tags = dict(ln.split(": ", 1) for ln in lines[:12] if ": " in ln)
        bits = [tags.get("title"), tags.get("artist") or
                tags.get("albumartist")]
        return " - ".join(b for b in bits if b)[:140]
    stem = os.path.splitext(name)[0].lower()
    seen = 0
    for ln in lines:
        if not ln:
            continue
        seen += 1
        if seen > 12:
            break
        if _prose_line(ln, 4, 110) and len(ln.split()) >= 2 \
                and not ln.lower().startswith(("page ", "http", "www.",
                                               "dear ", "hi ", "hello ")) \
                and ln.lower() != stem:
            return ln
    return ""


def gist_of(doctype, sample, uni):
    """Two sentences lifted from the text that say the most: scored by how
    many of the document's own frequent words they carry, earlier ones
    preferred. Not written - chosen."""
    if doctype in ("code", "data", "log"):
        return ""
    if doctype == "audio":
        return " | ".join(ln for ln in sample.split("\n")[:6] if ln)[:300]
    if doctype == "archive":
        names = [ln for ln in sample.split("\n") if ln and
                 not ln.endswith("/")]
        return "{:,}+ files inside: {}".format(
            len(names), ", ".join(os.path.basename(n) for n in names[:6])
        )[:300] if names else ""
    cands = []
    for s in _SENT.split(sample[:GIST_CHARS]):
        s = s.strip()
        if _prose_line(s, 40, 300):
            cands.append(s)
            if len(cands) >= 40:
                break
    if not cands:
        flat = " ".join(sample[:400].split())
        return flat[:220]
    scored = []
    for i, s in enumerate(cands):
        words = set(_WORD.findall(s.lower()))
        hit = sum(uni.get(w, 0) for w in words)
        score = hit / math.sqrt(len(words) + 3.0)
        score *= 1.0 if i < 4 else max(0.6, 1.0 - 0.03 * (i - 3))
        scored.append((score, i, s))
    best = sorted(sorted(scored, reverse=True)[:2], key=lambda x: x[1])
    out = best[0][2]
    if len(best) > 1 and len(out) + len(best[1][2]) <= 380:
        out += " " + best[1][2]
    return out


def make_card(name, ext, text):
    """(doctype, title, entities json, gist, packed terms) for one file's
    extracted text."""
    sample = text[:SAMPLE_CHARS]
    low = sample.lower()
    dt = doctype_of(name, ext, low[:HEAD_CHARS])
    if dt in NON_PROSE:
        uni, terms = {}, ""
    else:
        top, bi, uni = term_counts(low)
        terms = pack_terms(top + bi)
    ent = entities_of(sample, low) if dt not in ("code", "data", "log",
                                            "archive", "audio") else {}
    return (dt, title_of(name, ext, dt, sample),
            json.dumps(ent, ensure_ascii=False) if ent else "",
            gist_of(dt, sample, uni), terms)


def _card_batch(job):
    """Worker entry point: cards for a batch of files, text read straight
    from the index (read-only - a reader never waits behind a writer)."""
    db, rows = job
    conn = findex.open_db_ro(db)
    try:
        ids = [r[0] for r in rows]
        texts = dict(conn.execute(
            "SELECT rowid, substr(body, 1, ?) FROM docs WHERE rowid IN ({})"
            .format(",".join("?" * len(ids))), [SAMPLE_CHARS] + ids))
    finally:
        conn.close()
    out = []
    for fid, name, ext, sig in rows:
        try:
            card = make_card(name, ext, texts.get(fid) or "")
        except Exception:                                      # noqa: BLE001
            card = ("document", "", "", "", "")
        out.append((fid, sig) + card)
    return out


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS summary (
    file_id  INTEGER PRIMARY KEY,
    sig      TEXT,
    ver      INTEGER,
    doctype  TEXT,
    title    TEXT,
    keywords TEXT,
    entities TEXT,
    gist     TEXT,
    terms    TEXT,
    ai       TEXT,
    ai_model TEXT,
    updated  REAL
);
CREATE INDEX IF NOT EXISTS idx_summary_doctype ON summary(doctype);

CREATE TABLE IF NOT EXISTS summary_runs (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL UNIQUE,
    created    REAL,
    files      INTEGER,
    text_files INTEGER,
    bytes      INTEGER,
    detail     TEXT,
    seconds    REAL,
    overview   TEXT,
    ai         TEXT,
    ai_model   TEXT
);

CREATE TABLE IF NOT EXISTS summary_sections (
    id       INTEGER PRIMARY KEY,
    run_id   INTEGER NOT NULL,
    pos      INTEGER,
    kind     TEXT,
    label    TEXT,
    terms    TEXT,
    n        INTEGER,
    bytes    INTEGER,
    info     TEXT,
    ai_title TEXT,
    ai       TEXT,
    ai_model TEXT
);
CREATE INDEX IF NOT EXISTS idx_sumsec_run ON summary_sections(run_id);

CREATE TABLE IF NOT EXISTS summary_members (
    section_id INTEGER NOT NULL,
    file_id    INTEGER NOT NULL,
    PRIMARY KEY (section_id, file_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_summem_file ON summary_members(file_id);

CREATE TABLE IF NOT EXISTS summary_digests (
    scope   TEXT PRIMARY KEY,
    created REAL,
    n       INTEGER,
    names   TEXT,
    text    TEXT,
    model   TEXT
);

CREATE TRIGGER IF NOT EXISTS files_summary_ad AFTER DELETE ON files BEGIN
    DELETE FROM summary WHERE file_id = old.id;
    DELETE FROM summary_members WHERE file_id = old.id;
END;
"""

UPSERT_CARD = """
INSERT INTO summary (file_id, sig, ver, doctype, title, entities, gist, terms,
                     keywords, updated)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)
ON CONFLICT(file_id) DO UPDATE SET
    ai = CASE WHEN summary.sig IS NULL OR summary.sig = excluded.sig
              THEN summary.ai END,
    ai_model = CASE WHEN summary.sig IS NULL OR summary.sig = excluded.sig
                    THEN summary.ai_model END,
    sig = excluded.sig, ver = excluded.ver, doctype = excluded.doctype,
    title = excluded.title, entities = excluded.entities,
    gist = excluded.gist, terms = excluded.terms, updated = excluded.updated
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def have_tables(conn):
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='summary_members'").fetchone() is not None
    except Exception:                                          # noqa: BLE001
        return False


def _sep(path):
    return "\\" if (len(path) >= 2 and path[1] == ":") \
        or path.startswith("\\\\") else "/"


def norm_scope(scope):
    """A folder as the runs are keyed: no trailing separator, '' for the
    whole index. The path's own style is kept, so an index built on Windows
    can be summarised on a Mac."""
    s = str(scope or "").strip().strip('"')
    if not s:
        return ""
    if s.startswith("~"):
        s = os.path.expanduser(s)
    if not (s.startswith(("/", "\\\\")) or (len(s) >= 2 and s[1] == ":")):
        s = os.path.abspath(s)
    return s.rstrip("\\/")


def scope_like(scope):
    return findex.like_escape(scope) + _sep(scope) + "%"


def _scope_sql(scope, col="f.path"):
    if not scope:
        return "", []
    return " AND {} LIKE ? ESCAPE '!'".format(col), [scope_like(scope)]


def _base(path):
    """A file's name, whichever kind of path it is - a Windows path in an
    index opened on a Mac has no '/' for os.path.basename to split on."""
    return re.split(r"[\\/]", path)[-1] or path


def _under(path, scope):
    """Is path inside scope (or scope itself)? Case-blind for Windows
    paths, which is how the index's own LIKE scopes behave."""
    if not scope:
        return True
    if _sep(scope) == "\\":
        path, scope = path.lower(), scope.lower()
    return path == scope or path.startswith(scope + _sep(scope))


# ----------------------------------------------------------------------------
# Sections: grouping documents by what they are about
# ----------------------------------------------------------------------------

def _plural_map(df):
    """plural -> singular, only where BOTH are words of this collection, so
    'invoices' joins 'invoice' but 'james' and 'news' are left alone."""
    out = {}
    for t in df:
        if len(t) > 3 and t.endswith("s"):
            if t[:-1] in df:
                out[t] = t[:-1]
            elif t.endswith("ies") and t[:-3] + "y" in df:
                out[t] = t[:-3] + "y"
            elif t.endswith("es") and len(t) > 4 and t[:-2] in df:
                out[t] = t[:-2]
    return out


def _overlaps(a, b):
    wa, wb = a.split(), b.split()
    if set(wa) & set(wb):
        return True
    return any(x.rstrip("s") == y.rstrip("s") for x in wa for y in wb)


def label_from(terms, limit=3):
    """'Insurance · Policy · Renewal' from a section's strongest terms,
    leaving out ones that only repeat a word already used."""
    out, length = [], 0
    for t in terms:
        if any(_overlaps(t, o) for o in out):
            continue
        if out and length + len(t) > 34:
            break                       # short enough to read in a list
        out.append(t)
        length += len(t)
        if len(out) >= limit:
            break
    return " · ".join(" ".join(w[:1].upper() + w[1:] for w in t.split())
                      for t in out)


def _normalise(d, keep):
    top = sorted(d.items(), key=lambda kv: -kv[1])[:keep]
    norm = math.sqrt(sum(w * w for _, w in top)) or 1.0
    return {t: w / norm for t, w in top}


def _assign(vecs, cents, min_sim):
    """Each document to the section it is most like. An inverted index over
    the sections' terms makes this a few dozen dictionary lookups per
    document, whatever the number of sections."""
    inv = {}
    for k, c in enumerate(cents):
        if c:
            for t, w in c.items():
                inv.setdefault(t, []).append((k, w))
    labels = array("i", [-1]) * len(vecs)
    get = inv.get
    for i, (ids, ws) in enumerate(vecs):
        sc = {}
        for t, w in zip(ids, ws):
            post = get(t)
            if post:
                for k, cw in post:
                    sc[k] = sc.get(k, 0.0) + w * cw
        if sc:
            k = max(sc, key=sc.get)
            if sc[k] >= min_sim:
                labels[i] = k
    return labels


def _centroids(vecs, labels, k, min_size):
    acc = [dict() for _ in range(k)]
    size = [0] * k
    for (ids, ws), lab in zip(vecs, labels):
        if lab >= 0:
            a = acc[lab]
            size[lab] += 1
            for t, w in zip(ids, ws):
                a[t] = a.get(t, 0.0) + w
    return [(_normalise(a, 30) if size[i] >= min_size else None)
            for i, a in enumerate(acc)], size


def _merge_similar(cents, threshold=0.55):
    """Fold together sections that turned out to be about the same thing."""
    live = [i for i, c in enumerate(cents) if c]
    for x, i in enumerate(live):
        ci = cents[i]
        if not ci:
            continue
        for j in live[x + 1:]:
            cj = cents[j]
            if not cj:
                continue
            small, big = (ci, cj) if len(ci) < len(cj) else (cj, ci)
            sim = sum(w * big.get(t, 0.0) for t, w in small.items())
            if sim >= threshold:
                merged = dict(ci)
                for t, w in cj.items():
                    merged[t] = merged.get(t, 0.0) + w
                cents[i] = ci = _normalise(merged, 30)
                cents[j] = None
    return cents


def _seeds(vecs, limit, min_size):
    """Starting points for up to `limit` sections.

    A good seed is a key phrase that keeps the same company: the documents
    it heads are also headed by the same few other phrases. That is what
    separates a subject ("insurance", always beside "policy" and "premium")
    from a word that is merely frequent ("information", beside anything) -
    so candidates are ranked by how much they co-occur with their closest
    companions, not by how common they are, and a phrase's close companions
    are not seeded again. Each seed comes back as a small weighted bag: the
    phrase and the ones it travels with."""
    step = max(1, len(vecs) // SEED_SAMPLE)
    kdf, pairs = Counter(), Counter()
    for ids, _ in vecs[::step]:
        top = ids[:5]
        kdf.update(top)
        if len(top) > 1:
            pairs.update(combinations(sorted(top), 2))
    seed_min = max(2, min_size // (4 * step))
    near = {t: [] for t, c in kdf.items() if c >= seed_min}
    for (a, b), c in pairs.items():
        if c > 1:
            if a in near:
                near[a].append((c, b))
            if b in near:
                near[b].append((c, a))
    strength = {}
    for t, lst in near.items():
        lst.sort(reverse=True)
        del lst[12:]
        strength[t] = sum(c for c, _ in lst[:8])
    cents, taken = [], set()
    for t in sorted(near, key=lambda t: (-strength[t], -kdf[t])):
        if len(cents) >= limit or strength[t] < 0.25 * kdf[t]:
            break
        if t in taken:
            continue
        lst = near[t]
        taken.add(t)
        taken.update(o for c, o in lst if 2 * c >= lst[0][0])
        cent = {o: c / kdf[t] for c, o in lst}
        cent[t] = 1.0
        cents.append(_normalise(cent, 13))
    return cents


def target_sections(n, detail="normal"):
    factor, cap = DETAILS.get(detail, DETAILS["normal"])
    return int(max(3, min(cap, round(factor * 1.3 * n ** 0.33))))


def build_sections(prose, detail="normal", _depth=0):
    """Group documents by subject.

    prose: [(file id, packed terms)]. Returns (topics, keywords, leftover):
    topics = [{"terms": [...], "members": [file id...]}], biggest first;
    keywords = {file id: [its key phrases]}; leftover = the ids that fitted
    nowhere (or everything, when there are too few documents to group).

    Seeds come from which key phrases turn up together; documents are then
    assigned to the nearest section and the sections re-centred a few times
    (k-means over TF-IDF vectors, with each section kept to its 30 strongest
    terms so assignment stays fast in plain Python).
    """
    n = len(prose)
    if n == 0:
        return [], {}, []
    df = Counter()
    for _, packed in prose:
        df.update(t for t, _ in unpack_terms(packed))
    merge = _plural_map(df)
    for plural, single in merge.items():
        df[single] = min(n, df[single] + df[plural])
        del df[plural]
    lo = 2 if n >= 10 else 1
    hi = max(2.0, 0.5 * n) if n >= 10 else n
    idf = {t: math.log(1.0 + n / c) for t, c in df.items() if lo <= c <= hi}
    vocab = {t: i for i, t in enumerate(idf)}
    words = list(idf)
    idf_id = [idf[t] for t in words]

    vecs, fids, keywords = [], [], {}
    for fid, packed in prose:
        d = {}
        for t, tf in unpack_terms(packed):
            t = merge.get(t, t)
            i = vocab.get(t)
            if i is not None:
                w = (1.0 + math.log(tf)) * idf_id[i]
                if " " in t:
                    w *= 1.25
                d[i] = d.get(i, 0.0) + w
        top = sorted(d.items(), key=lambda kv: -kv[1])[:VEC_TERMS]
        norm = math.sqrt(sum(w * w for _, w in top)) or 1.0
        vecs.append((tuple(i for i, _ in top),
                     array("f", [w / norm for _, w in top])))
        fids.append(fid)
        if _depth == 0:
            kws, used = [], set()
            for i, _ in top:
                t = words[i]
                parts = t.split() if " " in t else (t,)
                if any(w in used or w.rstrip("s") in used for w in parts):
                    continue            # only repeats a word already shown
                kws.append(t)
                used.update(parts)
                used.update(w.rstrip("s") for w in parts)
                if len(kws) >= 8:
                    break
            keywords[fid] = kws

    if n < MIN_PROSE:
        return [], keywords, list(fids)

    # -- find sections ------------------------------------------------------
    # In passes: seed sections from the documents not yet in one, settle
    # them, and go round again on what is left - so a subject that was not
    # among the first seeds still gets its own section instead of leaving
    # its documents unsorted. Then every section competes for every
    # document.
    k_target = target_sections(n, detail)
    budget = min(DETAILS.get(detail, DETAILS["normal"])[1], 2 * k_target)
    min_size = max(3, n // (k_target * 10))
    cents = []
    pool = list(range(n))
    for _pass in range(6):
        room = budget - len(cents)
        if room < 1 or len(pool) < max(MIN_PROSE, min_size):
            break
        pv = [vecs[i] for i in pool]
        new = _seeds(pv, min(room, int(k_target * 1.5) + 1), min_size)
        if not new:
            break
        lab = _assign(pv, new, 0.02)
        for rnd in range(3):
            new, _ = _centroids(pv, lab, len(new), min_size)
            if rnd == 0:
                new = _merge_similar(new)
            lab = _assign(pv, new, MIN_SIM)
        live = {k for k in lab if k >= 0}
        if not live:
            break
        cents += [new[k] for k in sorted(live)]
        pool = [i for i, k in zip(pool, lab) if k < 0]
    if not cents:
        return [], keywords, list(fids)
    labels = _assign(vecs, cents, MIN_SIM)
    for rnd in range(2):
        cents, _ = _centroids(vecs, labels, len(cents), min_size)
        if rnd == 0:
            cents = _merge_similar(cents)
        labels = _assign(vecs, cents, MIN_SIM)

    members = {}
    for fid, lab in zip(fids, labels):
        members.setdefault(lab, []).append(fid)
    leftover = members.pop(-1, [])
    # What a section is called: the terms most of ITS documents carry and
    # few others do (share of the section x rarity) - not simply its
    # heaviest words, which favours whatever one long document repeats.
    cover = [Counter() for _ in cents]
    for (ids, _), lab in zip(vecs, labels):
        if lab >= 0:
            cover[lab].update(ids)
    topics = []
    for lab, ids in members.items():
        if len(ids) < min_size:
            leftover += ids
            continue
        size = float(len(ids))
        ranked = sorted(cover[lab].items(),
                        key=lambda kv: -(kv[1] / size) * idf_id[kv[0]]
                        * (1.15 if " " in words[kv[0]] else 1.0))
        topics.append({"terms": [words[t] for t, _ in ranked[:12]],
                       "members": ids})

    # -- one section swallowed nearly everything: look inside it -----------
    if _depth == 0:
        packed_of = None
        out = []
        for tp in topics:
            if len(tp["members"]) >= 150 and len(tp["members"]) > 0.6 * n:
                if packed_of is None:
                    packed_of = dict(prose)
                sub, _, rest = build_sections(
                    [(f, packed_of[f]) for f in tp["members"]], detail, 1)
                if len(sub) >= 2:
                    out += sub
                    if rest:
                        out.append({"terms": tp["terms"], "members": rest,
                                    "general": True})
                    continue
            out.append(tp)
        topics = out
    topics.sort(key=lambda tp: -len(tp["members"]))
    return topics, keywords, leftover


FRIENDLY = {"images": "Pictures", "videos": "Videos", "audio": "Music & audio",
            "documents": "Documents with no text",
            "compressed": "Zip & archives", "code": "Code & scripts",
            "programs": "Programs & installers",
            "emails": "Emails with no text"}
NON_PROSE_GROUP = {"code": "Code & scripts", "data": "Data & config",
                   "log": "Logs", "audio": "Music & audio",
                   "archive": "Zip & archives"}
_EXT_GROUP = {}
for _group, _exts in findex.TYPE_GROUPS.items():
    for _e in _exts:
        _EXT_GROUP.setdefault(_e, _group)
_NAME_PREFIX = re.compile(r"[^\W\d_]{3,}")


def type_group(ext):
    return FRIENDLY.get(_EXT_GROUP.get(ext or ""), "Other files")


def type_sections(items):
    """Sections for files that are not grouped by subject: by kind, and a
    big kind is split by the sub-folder (failing that, the name prefix) most
    of its files share. items: [(file id, group label, path, name)]."""
    groups = {}
    for it in items:
        groups.setdefault(it[1], []).append(it)
    out = []
    for label, rows in groups.items():
        n = len(rows)
        if n < SPLIT_MIN:
            out.append({"label": label, "members": [r[0] for r in rows]})
            continue
        need = max(50, n // 10)
        # split below the folder the whole group shares, so 480 pictures
        # all under Photos\ come apart by what is inside Photos
        dirs = [re.split(r"[\\/]", r[2])[:-1] for r in rows]
        shared = os.path.commonprefix(dirs)
        by = {}
        for r, parts in zip(rows, dirs):
            by.setdefault(parts[len(shared)] if len(parts) > len(shared)
                          else "", []).append(r)
        big = sorted(((k, v) for k, v in by.items() if k and len(v) >= need),
                     key=lambda kv: -len(kv[1]))[:12]
        how = "{} · {}"
        if not big:
            by = {}
            for r in rows:
                m = _NAME_PREFIX.match(r[3])
                by.setdefault(m.group(0).lower() if m else "", []).append(r)
            big = sorted(((k, v) for k, v in by.items()
                          if k and len(v) >= max(50, n * 15 // 100)),
                         key=lambda kv: -len(kv[1]))[:8]
            how = "{} · named {}..."
        if not big or (len(big) == 1 and len(big[0][1]) > 0.85 * n):
            out.append({"label": label, "members": [r[0] for r in rows]})
            continue
        taken = set()
        for key, part in big:
            out.append({"label": how.format(label, key),
                        "members": [r[0] for r in part]})
            taken.update(r[0] for r in part)
        rest = [r[0] for r in rows if r[0] not in taken]
        if rest:
            out.append({"label": label + " · elsewhere", "members": rest})
    out.sort(key=lambda s: -len(s["members"]))
    return out


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------

def _emit(progress, seen, total, done, t0):
    if progress:
        print("@P seen={} done={} total={} elapsed={:.1f}".format(
            seen, done, total, time.time() - t0), flush=True)


def run(db, scope="", detail="normal", workers=None, rebuild=False,
        progress=False, log=print):
    """Summarise one folder (or '' = the whole index). Returns the run id,
    or None when the index holds nothing under that folder."""
    t0 = time.time()
    scope = norm_scope(scope)
    conn = findex.open_db(db)
    try:
        ensure_schema(conn)
        ssql, sparams = _scope_sql(scope)
        files = conn.execute(
            "SELECT f.id, f.path, f.name, f.ext, f.size, f.mtime, f.chars "
            "FROM files f WHERE f.is_dir=0" + ssql, sparams).fetchall()
        if not files:
            log("Nothing in the index under {} - index it first.".format(
                scope or "any folder"))
            return None
        have = {}
        if not rebuild:
            for fid, sig, dt, terms in conn.execute(
                    "SELECT s.file_id, s.sig, s.doctype, s.terms "
                    "FROM summary s JOIN files f ON f.id = s.file_id "
                    "WHERE s.ver = ? AND f.is_dir=0" + ssql,
                    [CARD_VERSION] + sparams):
                have[fid] = (sig, dt, terms)

        doctype = {}                   # file id -> type, files with text
        packed = {}                    # file id -> packed terms, prose only
        jobs = []
        for fid, path, name, ext, size, mtime, chars in files:
            if not chars:
                continue
            sig = "{}:{}:{}".format(size or 0, int(mtime or 0), chars)
            old = have.get(fid)
            if old and old[0] == sig:
                doctype[fid] = old[1]
                if old[1] not in NON_PROSE:
                    packed[fid] = old[2] or ""
            else:
                jobs.append((fid, name, (ext or "").lower(), sig))
        with_text = len(doctype) + len(jobs)
        log("Summarising {}: {:,} files, {:,} with text ({:,} to read, "
            "{:,} already carded)".format(scope or "the whole index",
                                          len(files), with_text, len(jobs),
                                          len(doctype)))

        # -- cards ---------------------------------------------------------
        if jobs:
            batches = [(db, jobs[i:i + BATCH])
                       for i in range(0, len(jobs), BATCH)]
            now = time.time()
            done = 0
            _emit(progress, 0, len(jobs), 0, t0)

            def store(result):
                conn.executemany(UPSERT_CARD, [
                    (fid, sig, CARD_VERSION, dt, title, ent, gist, terms, now)
                    for fid, sig, dt, title, ent, gist, terms in result])
                conn.commit()
                for fid, _sig, dt, _t, _e, _g, terms in result:
                    doctype[fid] = dt
                    if dt not in NON_PROSE:
                        packed[fid] = terms

            if len(jobs) < INLINE_BELOW or workers == 1:
                for b in batches:
                    result = _card_batch(b)
                    store(result)
                    done += len(result)
                    _emit(progress, done, len(jobs), done, t0)
            else:
                n_workers = workers or min(32, os.cpu_count() or 4)
                with ProcessPoolExecutor(max_workers=n_workers) as ex:
                    for result in ex.map(_card_batch, batches):
                        store(result)
                        done += len(result)
                        _emit(progress, done, len(jobs), done, t0)
            log("  read {:,} files in {:.1f}s".format(len(jobs),
                                                      time.time() - t0))

        # -- sections ------------------------------------------------------
        t1 = time.time()
        log("  grouping {:,} documents by subject...".format(len(packed)))
        topics, keywords, leftover = build_sections(
            list(packed.items()), detail)
        if keywords:
            conn.executemany(
                "UPDATE summary SET keywords=? WHERE file_id=?",
                (("; ".join(k), fid) for fid, k in keywords.items()))
            conn.commit()

        sections = []
        for tp in topics:
            label = label_from(tp["terms"])
            if tp.get("general"):
                label += " (general)"
            sections.append({"kind": "topic", "label": label,
                             "terms": tp["terms"], "members": tp["members"]})
        by_type = {}
        for fid in leftover:
            by_type.setdefault(doctype.get(fid, "document"), []).append(fid)
        small = []
        floor = 3 if topics else 1
        for dt, ids in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            if len(ids) >= floor:
                name = DOCTYPE_LABEL.get(dt, dt.title())
                sections.append({
                    "kind": "doctype", "members": ids, "terms": [],
                    "label": ("Unsorted · " + name) if topics else name})
            else:
                small += ids
        if small:
            sections.append({"kind": "misc", "label": "Unsorted documents",
                             "terms": [], "members": small})

        items = []
        for fid, path, name, ext, size, mtime, chars in files:
            dt = doctype.get(fid)
            if dt is None:
                items.append((fid, type_group((ext or "").lower()), path,
                              name))
            elif dt in NON_PROSE:
                items.append((fid, NON_PROSE_GROUP[dt], path, name))
        for ts in type_sections(items):
            sections.append({"kind": "type", "label": ts["label"],
                             "terms": [], "members": ts["members"]})

        # -- store ---------------------------------------------------------
        meta = {f[0]: (f[4] or 0, f[5] or 0) for f in files}
        cur = conn.cursor()
        old = cur.execute("SELECT id FROM summary_runs WHERE scope=?",
                          (scope,)).fetchone()
        kept_ai = {}
        if old:
            for label, ai_title, ai, ai_model in cur.execute(
                    "SELECT label, ai_title, ai, ai_model FROM "
                    "summary_sections WHERE run_id=? AND ai IS NOT NULL",
                    (old[0],)):
                kept_ai[label] = (ai_title, ai, ai_model)
            _delete_run(cur, old[0])
        total_bytes = sum(m[0] for m in meta.values())
        overview = _overview(files, doctype, sections, keywords, meta)
        cur.execute(
            "INSERT INTO summary_runs (scope, created, files, text_files, "
            "bytes, detail, seconds, overview) VALUES (?,?,?,?,?,?,?,?)",
            (scope, time.time(), len(files), with_text, total_bytes, detail,
             time.time() - t0, json.dumps(overview, ensure_ascii=False)))
        run_id = cur.lastrowid
        for pos, s in enumerate(sections):
            ids = s["members"]
            size = sum(meta[f][0] for f in ids)
            times = [meta[f][1] for f in ids if meta[f][1]]
            kinds = Counter(doctype.get(f) for f in ids if f in doctype)
            info = {"oldest": min(times) if times else 0,
                    "newest": max(times) if times else 0,
                    "doctypes": kinds.most_common(4)}
            ai = kept_ai.get(s["label"], (None, None, None))
            cur.execute(
                "INSERT INTO summary_sections (run_id, pos, kind, label, "
                "terms, n, bytes, info, ai_title, ai, ai_model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, pos, s["kind"], s["label"], "; ".join(s["terms"]),
                 len(ids), size, json.dumps(info), ai[0], ai[1], ai[2]))
            sid = cur.lastrowid
            cur.executemany("INSERT OR IGNORE INTO summary_members "
                            "(section_id, file_id) VALUES (?, ?)",
                            ((sid, f) for f in ids))
        conn.commit()
        log("  {:,} sections in {:.1f}s (grouping {:.1f}s)".format(
            len(sections), time.time() - t0, time.time() - t1))
        return run_id
    finally:
        conn.close()


def _delete_run(cur, run_id):
    cur.execute("DELETE FROM summary_members WHERE section_id IN "
                "(SELECT id FROM summary_sections WHERE run_id=?)", (run_id,))
    cur.execute("DELETE FROM summary_sections WHERE run_id=?", (run_id,))
    cur.execute("DELETE FROM summary_runs WHERE id=?", (run_id,))


def _overview(files, doctype, sections, keywords, meta):
    kinds = Counter(doctype.values())
    exts = Counter((f[3] or "(none)").lower() for f in files)
    times = [m[1] for m in meta.values() if m[1]]
    words = Counter()
    for kws in keywords.values():
        words.update(kws[:4])
    return {
        "doctypes": [(DOCTYPE_LABEL.get(k, k), c)
                     for k, c in kinds.most_common(8)],
        "exts": exts.most_common(8),
        "oldest": min(times) if times else 0,
        "newest": max(times) if times else 0,
        "keywords": [w for w, _ in words.most_common(15)],
        "sections": [(s["label"], len(s["members"])) for s in sections[:10]],
    }


def forget(conn, scope=None):
    """Drop the sections of one folder's run, or every run and card."""
    ensure_schema(conn)
    cur = conn.cursor()
    if scope is None:
        cur.execute("DELETE FROM summary_members")
        cur.execute("DELETE FROM summary_sections")
        cur.execute("DELETE FROM summary_runs")
        cur.execute("DELETE FROM summary_digests")
        cur.execute("DELETE FROM summary")
        n = -1
    else:
        row = cur.execute("SELECT id FROM summary_runs WHERE scope=?",
                          (norm_scope(scope),)).fetchone()
        n = 0
        cur.execute("DELETE FROM summary_digests WHERE scope=?",
                    (norm_scope(scope),))
        if row:
            _delete_run(cur, row[0])
            n = 1
    conn.commit()
    return n


# ----------------------------------------------------------------------------
# Reading the results
# ----------------------------------------------------------------------------

RUN_COLS = ("id", "scope", "created", "files", "text_files", "bytes",
            "detail", "seconds", "overview", "ai", "ai_model")
SECTION_COLS = ("id", "run_id", "pos", "kind", "label", "terms", "n",
                "bytes", "info", "ai_title", "ai", "ai_model")
FILE_COLS = ("id", "path", "size", "mtime", "doctype", "title", "keywords",
             "gist", "ai", "entities")


def _run_dict(row):
    d = dict(zip(RUN_COLS, row))
    try:
        d["overview"] = json.loads(d["overview"] or "{}")
    except ValueError:
        d["overview"] = {}
    return d


def runs(conn):
    """Every folder summarised so far, newest first."""
    if not have_tables(conn):
        return []
    return [_run_dict(r) for r in conn.execute(
        "SELECT {} FROM summary_runs ORDER BY created DESC".format(
            ", ".join(RUN_COLS)))]


def find_run(conn, scope):
    """(run, exact) for a folder: its own run, or failing that the run of
    the nearest folder above it (whose sections can be narrowed to it).
    (None, False) when nothing covers it."""
    scope = norm_scope(scope)
    best = None
    for r in runs(conn):
        rs = r["scope"]
        same = rs == scope or (_sep(scope or rs) == "\\"
                               and rs.lower() == scope.lower())
        if same:
            return r, True
        if _under(scope, rs) and scope:
            if best is None or len(rs) > len(best["scope"]):
                best = r
    return best, False


def sections(conn, run_id, under=None):
    """A run's sections, biggest first. With `under`, counts and sizes are
    of the files beneath that folder only, and empty sections drop out."""
    cols = ", ".join("s." + c for c in SECTION_COLS)
    if under:
        rows = conn.execute(
            "SELECT {}, COUNT(*), SUM(f.size) FROM summary_sections s "
            "JOIN summary_members m ON m.section_id = s.id "
            "JOIN files f ON f.id = m.file_id WHERE s.run_id=? "
            "AND f.path LIKE ? ESCAPE '!' GROUP BY s.id".format(cols),
            (run_id, scope_like(norm_scope(under)))).fetchall()
    else:
        rows = conn.execute(
            "SELECT {}, s.n, s.bytes FROM summary_sections s "
            "WHERE s.run_id=?".format(cols), (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(SECTION_COLS, r))
        d["n"], d["bytes"] = r[-2], r[-1] or 0
        try:
            d["info"] = json.loads(d["info"] or "{}")
        except ValueError:
            d["info"] = {}
        out.append(d)
    order = {"topic": 0, "doctype": 1, "misc": 1, "type": 2}
    out.sort(key=lambda d: (order.get(d["kind"], 3), -d["n"]))
    return out


def section_files(conn, section_id, under=None, limit=5000):
    """The files of one section with their cards, newest first."""
    sql = ("SELECT f.id, f.path, f.size, f.mtime, c.doctype, c.title, "
           "c.keywords, c.gist, c.ai, c.entities FROM summary_members m "
           "JOIN files f ON f.id = m.file_id "
           "LEFT JOIN summary c ON c.file_id = f.id WHERE m.section_id=?")
    params = [section_id]
    if under:
        sql += " AND f.path LIKE ? ESCAPE '!'"
        params.append(scope_like(norm_scope(under)))
    sql += " ORDER BY f.mtime DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(zip(FILE_COLS, r)) for r in conn.execute(sql, params)]


def recent_files(conn, scope, limit=500):
    """The most recently modified files under a folder that have a card -
    what the Overview lists."""
    if not have_tables(conn):
        return []
    sql, params = _scope_sql(scope)
    return [dict(zip(FILE_COLS, r)) for r in conn.execute(
        "SELECT f.id, f.path, f.size, f.mtime, c.doctype, c.title, "
        "c.keywords, c.gist, c.ai, c.entities FROM summary c "
        "JOIN files f ON f.id = c.file_id WHERE f.is_dir=0" + sql
        + " ORDER BY f.mtime DESC LIMIT ?", params + [int(limit)])]


def files_by_id(conn, ids):
    """Files with their cards, in the order of `ids`."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(
                "SELECT f.id, f.path, f.size, f.mtime, c.doctype, c.title, "
                "c.keywords, c.gist, c.ai, c.entities FROM files f "
                "LEFT JOIN summary c ON c.file_id = f.id WHERE f.id IN ({})"
                .format(",".join("?" * len(part))), part):
            out[r[0]] = dict(zip(FILE_COLS, r))
    return [out[i] for i in ids if i in out]


def save_digest(conn, scope, files, text, model):
    """Keep the combined summary of a hand-picked set of files as the
    folder's latest selection summary (one per folder; a new one replaces
    it)."""
    names = [_base(f["path"]) for f in files[:6]]
    conn.execute(
        "INSERT OR REPLACE INTO summary_digests (scope, created, n, names, "
        "text, model) VALUES (?,?,?,?,?,?)",
        (norm_scope(scope), time.time(), len(files), "; ".join(names), text,
         model))
    conn.commit()


def digest_for(conn, scope):
    """The folder's latest selection summary as a dict, or None."""
    try:
        r = conn.execute(
            "SELECT scope, created, n, names, text, model FROM "
            "summary_digests WHERE scope=?", (norm_scope(scope),)).fetchone()
    except Exception:                                          # noqa: BLE001
        return None
    return dict(zip(("scope", "created", "n", "names", "text", "model"), r)) \
        if r else None


def in_short(rows, terms=""):
    """What a set of files' cards add up to, without any AI: the years
    their text mentions, the names (sites, email domains) that recur, and
    the few files most typical of the set, each with the sentences lifted
    from it. For the card of a section or a folder."""
    years, names = Counter(), Counter()
    want = set(_WORD.findall((terms or "").lower()))
    scored = []
    for f in rows:
        try:
            ent = json.loads(f["entities"]) if f.get("entities") else {}
        except ValueError:
            ent = {}
        years.update({d[:4] for d in ent.get("dates", ())})
        names.update(set(ent.get("sites", ()))
                     | {e.split("@")[-1] for e in ent.get("emails", ())})
        if f.get("gist") or f.get("ai"):
            words = set(_WORD.findall((f.get("keywords") or "").lower()))
            scored.append((len(words & want), len(scored), f))
    floor = max(1, sum(years.values()) // 50)       # ignore stray years
    solid = sorted(y for y, c in years.items() if c >= floor)
    typical, seen = [], set()
    for _, _, f in sorted(scored, key=lambda x: (-x[0], x[1])):
        said = f.get("ai") or f.get("gist")
        if said[:60] in seen:
            continue
        seen.add(said[:60])
        typical.append((f.get("title") or _base(f["path"]), said))
        if len(typical) >= 3:
            break
    return {"years": (solid[0], solid[-1]) if solid else None,
            "names": [n for n, c in names.most_common(5) if c > 1],
            "typical": typical}


def card(conn, path):
    """One file's card by path, or None."""
    if not have_tables(conn):
        return None
    r = conn.execute(
        "SELECT f.id, f.path, f.size, f.mtime, c.doctype, c.title, "
        "c.keywords, c.gist, c.ai, c.entities FROM files f "
        "JOIN summary c ON c.file_id = f.id WHERE f.path=?",
        (path,)).fetchone()
    return dict(zip(FILE_COLS, r)) if r else None


def subfolders(conn, scope):
    """[(folder path, files beneath it)] directly inside a folder - or the
    index's own roots for '' - biggest first. From the index; no disk."""
    scope = norm_scope(scope)
    if not scope:
        out = []
        for root in findex.saved_roots(conn):
            root = norm_scope(root)
            n = conn.execute(
                "SELECT COUNT(*) FROM files f WHERE f.is_dir=0"
                + _scope_sql(root)[0], _scope_sql(root)[1]).fetchone()[0]
            out.append((root, n))
        return sorted(out, key=lambda x: -x[1])
    sep = _sep(scope)
    cut = len(scope) + 1
    counts = Counter()
    for path, is_dir in conn.execute(
            "SELECT f.path, f.is_dir FROM files f WHERE 1"
            + _scope_sql(scope)[0], _scope_sql(scope)[1]):
        rel = path[cut:]
        i = rel.find(sep)
        if i < 0:
            if is_dir:
                counts[rel] += 0
        elif not is_dir:
            counts[rel[:i]] += 1
    return sorted(((scope + sep + name, n) for name, n in counts.items()
                   if name), key=lambda x: (-x[1], x[0].lower()))


def describe_section(s):
    """A line or two about a section from what the offline pass knows."""
    bits = []
    kinds = [(DOCTYPE_LABEL.get(k, k or "file"), c)
             for k, c in (s["info"].get("doctypes") or []) if k]
    if kinds and s["n"]:
        bits.append("Mostly " + ", ".join(
            "{} ({:.0f}%)".format(k.lower(), 100.0 * c / s["n"])
            for k, c in kinds[:3]))
    lo, hi = s["info"].get("oldest"), s["info"].get("newest")
    if lo and hi:
        a, b = time.strftime("%b %Y", time.localtime(lo)), \
            time.strftime("%b %Y", time.localtime(hi))
        bits.append("modified " + (a if a == b else a + " to " + b))
    out = ". ".join(bits)
    if s.get("terms"):
        out += ("\n" if out else "") + "About: " + s["terms"].replace(";", ",")
    return out


# ----------------------------------------------------------------------------
# Search filters (called by findex.query_rows)
# ----------------------------------------------------------------------------

def search_conds(conn, q, conds, params):
    """section: and doctype: terms of a parsed query -> SQL conditions on
    the files table (alias f). An index never summarised matches nothing."""
    ok = have_tables(conn)
    for key, neg in (("sections", False), ("sections_not", True)):
        for term in q.get(key, ()):
            if not ok:
                conds.append("1" if neg else "0")
                continue
            if term.startswith("#") and term[1:].isdigit():
                sub = "SELECT file_id FROM summary_members WHERE section_id=?"
                p = [int(term[1:])]
            else:
                pat = "%" + findex.like_escape(term) + "%"
                sub = ("SELECT m.file_id FROM summary_members m JOIN "
                       "summary_sections s ON s.id = m.section_id WHERE "
                       "s.label LIKE ? ESCAPE '!' OR s.ai_title LIKE ? "
                       "ESCAPE '!'")
                p = [pat, pat]
            conds.append("f.id {}IN ({})".format("NOT " if neg else "", sub))
            params += p
    for key, neg in (("doctypes", False), ("doctypes_not", True)):
        for term in q.get(key, ()):
            if not ok:
                conds.append("1" if neg else "0")
                continue
            conds.append("f.id {}IN (SELECT file_id FROM summary WHERE "
                         "doctype LIKE ? ESCAPE '!')".format(
                             "NOT " if neg else ""))
            params.append(findex.like_escape(term.lower()) + "%")


# ----------------------------------------------------------------------------
# Local AI (Ollama)
# ----------------------------------------------------------------------------

class AIError(Exception):
    pass


def _http(url, payload=None, timeout=10):
    """JSON over HTTP to the local model server - never through a proxy."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    return opener.open(req, timeout=timeout)


def ai_status(url=None, timeout=1.5):
    """{'ok', 'models', 'error'} - is a local model server answering?"""
    url = (url or AI_URL).rstrip("/")
    try:
        with _http(url + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        models = sorted(m.get("name", "") for m in data.get("models", [])
                        if m.get("name"))
        return {"ok": True, "models": models, "error": ""}
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "models": [], "error": str(exc)}


def pick_model(models, wanted=None):
    """The model to use: the one asked for if installed, else the first
    installed one from a list of small general-purpose models, else
    whatever is there. '' when nothing is installed."""
    if not models:
        return ""
    def match(name):
        for m in models:        # "gemma3:1b" is exact; "gemma3" any size
            if m == name or (":" not in name and m.split(":")[0] == name) \
                    or m == name + ":latest":
                return m
        return ""
    if wanted and match(wanted):
        return match(wanted)
    for name in AI_PREFERRED:
        if match(name):
            return match(name)
    usable = [m for m in models if "embed" not in m.lower()]
    return (usable or models)[0]


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def ai_generate(prompt, model, system=None, url=None, max_tokens=220,
                timeout=600):
    url = (url or AI_URL).rstrip("/")
    # think=False: models that can "think" first (qwen3.x, deepseek-r1)
    # otherwise spend most of their time on reasoning nobody reads.
    payload = {"model": model, "prompt": prompt, "stream": False,
               "think": False, "keep_alive": AI_KEEP_ALIVE,
               "options": {"temperature": 0.2, "num_predict": max_tokens,
                           "num_ctx": AI_CONTEXT}}
    if system:
        payload["system"] = system
    data = None
    for attempt in (0, 1):
        try:
            with _http(url + "/api/generate", payload, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception as exc:                               # noqa: BLE001
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:                                  # noqa: BLE001
                pass
            if attempt == 0 and "think" in detail.lower():
                del payload["think"]    # this model/server refuses the flag
                continue
            raise AIError("local AI request failed: {} {}".format(exc,
                                                                  detail))
    if data.get("error"):
        raise AIError(str(data["error"]))
    return _THINK.sub("", data.get("response") or "").strip()


FILE_SYSTEM = ("You write short summaries for a file index. Reply with two "
               "or three plain sentences saying what the document is, who or "
               "what it concerns, and any key dates, names or amounts. Use "
               "only what the text says. No preamble, no bullet points, no "
               "markdown.")
# Combined summaries are built in two steps so that any number of files
# fits a small model's context: notes on the files are condensed a batch at
# a time (PART), then the condensed parts are merged into one (WHOLE).
PART_SYSTEM = ("You are condensing notes about a batch of files from one "
               "collection. Write four or five plain sentences on what "
               "these files have in common: the subjects that recur, the "
               "people and organisations named, the dates or period, and "
               "any amounts that matter. Use only what the notes say. No "
               "preamble, no bullet points, no markdown.")
WHOLE_SYSTEM = ("You write one summary of a whole collection of files from "
                "notes about them. It must describe the collection as a "
                "whole - not list the files one by one. Use only what the "
                "notes say. Reply in exactly this form and nothing else:\n"
                "{title}SUMMARY: one paragraph of three to six sentences: "
                "what the collection is, what it covers, who and what it "
                "concerns, and the period.\nKEY POINTS:\n- three to six "
                "short lines, each one a specific fact or theme from the "
                "notes")
TITLE_LINE = "TITLE: a plain 2 to 5 word name for the collection\n"
AI_BATCH = 25              # files' notes per request when combining
AI_BATCH_CHARS = 6500      # ...and never more text than this
AI_SAMPLE = 120            # files read per section for its combined summary
AI_TOGETHER_MAX = 300      # files combined in one "selected files" summary


def _file_text(conn, fid, chars):
    row = conn.execute("SELECT substr(body, 1, ?) FROM docs WHERE rowid=?",
                       (AI_FILE_CHARS, fid)).fetchone()
    text = (row[0] if row else "") or ""
    if chars and chars > AI_FILE_CHARS + 2 * AI_TAIL_CHARS:
        tail = conn.execute("SELECT substr(body, ?) FROM docs WHERE rowid=?",
                            (-AI_TAIL_CHARS, fid)).fetchone()
        if tail and tail[0]:
            text += "\n[...]\n" + tail[0]
    return text


def ai_file(conn, fid, model, url=None):
    """Write (and store) one file's summary. Returns it, or '' for a file
    with no text to read."""
    row = conn.execute(
        "SELECT f.name, f.chars, c.doctype FROM files f LEFT JOIN summary c "
        "ON c.file_id = f.id WHERE f.id=?", (fid,)).fetchone()
    if not row or not row[1]:
        return ""
    name, chars, dt = row
    text = _file_text(conn, fid, chars)
    if not text.strip():
        return ""
    out = ai_generate("File name: {}\nKind: {}\n\nText:\n{}".format(
        name, DOCTYPE_LABEL.get(dt, "document"), text), model, FILE_SYSTEM,
        url, max_tokens=170)
    if out:
        conn.execute(
            "INSERT INTO summary (file_id, ai, ai_model, updated) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(file_id) DO UPDATE SET "
            "ai=excluded.ai, ai_model=excluded.ai_model",
            (fid, out, model, time.time()))
        conn.commit()
    return out


def _parse_whole(text):
    """(title, text) from a WHOLE_SYSTEM reply: the paragraph, then its key
    points one per line. Whatever a model does with the form, nothing it
    wrote is thrown away."""
    title = ""
    m = re.search(r"^\W*TITLE:\s*(.+)$", text, re.I | re.M)
    if m:
        title = m.group(1).strip().strip('"*#').strip()[:60]
        text = text[:m.start()] + text[m.end():]
    body, _, points = re.sub(r"\*\*", "", text).partition("KEY POINTS")
    body = " ".join(re.sub(r"^\W*SUMMARY:\s*", "", body.strip(),
                           flags=re.I).split())
    lines = []
    for ln in points.split("\n"):
        ln = ln.strip().lstrip(":").strip()
        ln = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", ln).strip()
        if len(ln) > 3:
            lines.append("- " + ln)
    return title, "\n".join(([body] if body else []) + lines[:8])


def note_for(f):
    """One file as a line of notes for the model: its name, kind and the
    best account of it there is - a written summary if it has one, else the
    sentences lifted from it, else its title and key phrases."""
    said = f.get("ai") or f.get("gist") or " ".join(
        x for x in (f.get("title"), f.get("keywords")) if x)
    return "- {} [{}]: {}".format(
        _base(f["path"]),
        DOCTYPE_LABEL.get(f.get("doctype"), "file"),
        " ".join((said or "").split())[:320])


def sample_files(files, terms="", cap=AI_SAMPLE):
    """Up to `cap` files to stand for a larger set: those with a written
    summary first, then the ones whose key phrases sit closest to the
    section's own, so the sample is typical rather than merely recent."""
    files = [f for f in files if f.get("ai") or f.get("gist")
             or f.get("title")]
    if len(files) <= cap:
        return files
    want = set(_WORD.findall((terms or "").lower()))

    def score(f):
        words = set(_WORD.findall((f.get("keywords") or "").lower()))
        return (1 if f.get("ai") else 0, len(words & want))
    return sorted(files, key=score, reverse=True)[:cap]


def _batches(notes):
    out, cur, size = [], [], 0
    for n in notes:
        if cur and (len(cur) >= AI_BATCH or size + len(n) > AI_BATCH_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(n) + 1
    if cur:
        out.append(cur)
    return out


def combine_calls(n_notes):
    """How many model requests ai_combine makes for this many notes."""
    if n_notes <= AI_BATCH:
        return 1
    parts = -(-n_notes // AI_BATCH)
    return parts + combine_calls(parts) if parts > AI_BATCH else parts + 1


def ai_combine(notes, about, model, url=None, want_title=False, tick=None):
    """One summary of many files. `notes` are note_for() lines (or, for a
    folder, lines about its sections); `about` says what the collection is.
    More notes than fit one request are condensed batch by batch first and
    the condensed parts merged. Returns (title, text)."""
    tick = tick or (lambda: None)
    groups = _batches(notes)
    while len(groups) > 1:
        parts = []
        for i, g in enumerate(groups):
            parts.append("Part {} of {} ({:,} items): {}".format(
                i + 1, len(groups), len(g), ai_generate(
                    "{}\nNotes on part {} of {}:\n{}".format(
                        about, i + 1, len(groups), "\n".join(g)),
                    model, PART_SYSTEM, url, max_tokens=190)))
            tick()
        groups = _batches(parts)
    out = ai_generate("{}\nNotes:\n{}".format(about, "\n".join(groups[0])),
                      model, WHOLE_SYSTEM.format(
                          title=TITLE_LINE if want_title else ""),
                      url, max_tokens=330)
    tick()
    return _parse_whole(out)


def _need_model(model, url, log):
    st = ai_start(url, log)         # installed but not running: start it
    if not st["ok"]:
        log("Local AI is not running at {} ({}). Install Ollama and start "
            "it, or run: findex summarise --ai-setup".format(
                url or AI_URL, st["error"]))
        return ""
    chosen = pick_model(st["models"], model)
    if not chosen:
        log("Ollama is running but has no model installed. Run: findex "
            "summarise --ai-setup   (or: ollama pull {})".format(AI_MODEL))
    elif model and not has_model([chosen], model):
        log("Model {} is not installed - using {}".format(model, chosen))
    return chosen


def ai_run_sections(db, scope, model=None, url=None, progress=False,
                    log=print, redo=False):
    """The folder as a whole: a combined summary of each section's files
    (with a title for it), then one of the entire folder built from those.

    A section's summary comes from its files' cards - their written
    summaries where they have them, the sentences lifted from them where
    not - so no file is re-read; a big section is represented by its
    AI_SAMPLE most typical files. Each summary is saved as it is written,
    so stopping part-way keeps what was done."""
    conn = findex.open_db(db)
    try:
        ensure_schema(conn)
        run_, exact = find_run(conn, scope)
        if not run_ or not exact:
            log("No summary for this folder yet - run the summary first.")
            return 1
        model = _need_model(model, url, log)
        if not model:
            return 2
        secs = sections(conn, run_["id"])
        todo = []
        for s in secs:
            if s["kind"] == "type" or (s["ai"] and not redo):
                continue
            files = sample_files(section_files(conn, s["id"], limit=600),
                                 s["terms"])
            if files:
                todo.append((s, files))
        total = sum(combine_calls(len(f)) for _, f in todo) + 1
        t0 = time.time()
        made = [0]

        def tick():
            made[0] += 1
            _emit(progress, made[0], total, made[0], t0)
        log("Summarising {:,} sections and the folder with {}...".format(
            len(todo), model))
        _emit(progress, 0, total, 0, t0)
        for n, (s, files) in enumerate(todo):
            before = made[0]
            try:
                about = ("A group of {:,} files from one folder{}. Its "
                         "recurring terms: {}.".format(
                             s["n"], "" if len(files) >= s["n"] else
                             " - these notes cover {:,} typical ones"
                             .format(len(files)), s["terms"] or s["label"]))
                title, text = ai_combine([note_for(f) for f in files], about,
                                         model, url, True, tick)
                conn.execute(
                    "UPDATE summary_sections SET ai_title=?, ai=?, "
                    "ai_model=? WHERE id=?", (title or None, text or None,
                                              model, s["id"]))
                conn.commit()
                s["ai_title"], s["ai"] = title, text
                log("  {}  ->  {}".format(s["label"], title or "(no title)"))
            except AIError as exc:
                log("  {}: {}".format(s["label"], exc))
                if n == 0:
                    return 2
                made[0] = before + combine_calls(len(files)) - 1
                tick()
        try:
            ai_folder(conn, run_, secs, model, url)
        except AIError as exc:
            log("  the folder as a whole: {}".format(exc))
        made[0] = total - 1
        tick()
        return 0
    finally:
        conn.close()


def ai_folder(conn, run, secs, model, url=None):
    """One summary of the whole folder, built from its sections' combined
    summaries and the run's own figures."""
    o = run["overview"]
    when = ""
    if o.get("oldest") and o.get("newest"):
        when = " Files modified {} to {}.".format(
            time.strftime("%B %Y", time.localtime(o["oldest"])),
            time.strftime("%B %Y", time.localtime(o["newest"])))
    about = ("The folder {}: {:,} files ({:,} with readable text), {}.{} "
             "Kinds of document: {}. Each note below is one section of the "
             "folder.".format(
                 run["scope"] or "(everything indexed)", run["files"],
                 run["text_files"] or 0, findex.human(run["bytes"] or 0),
                 when, ", ".join("{} x{}".format(k, c)
                                 for k, c in o.get("doctypes", [])) or "mixed"))
    notes = []
    for s in secs[:40]:
        said = (s.get("ai") or "").split("\n")[0] or \
            ("about " + s["terms"].replace(";", ",") if s["terms"]
             else "files with no readable text")
        notes.append("- {} ({:,} files): {}".format(
            s["ai_title"] or s["label"], s["n"], said[:420]))
    _, text = ai_combine(notes, about, model, url)
    conn.execute("UPDATE summary_runs SET ai=?, ai_model=? WHERE id=?",
                 (text or None, model, run["id"]))
    conn.commit()
    return text


def ai_run_files(db, ids, model=None, url=None, progress=False, log=print,
                 redo=False, scope=None, together=True):
    """Written summaries of specific files (by index id) - each one, and
    then, when there is more than one, a single combined summary of them
    all, stored as the folder's latest "selection" summary."""
    conn = findex.open_db(db)
    try:
        ensure_schema(conn)
        model = _need_model(model, url, log)
        if not model:
            return 2
        t0 = time.time()
        done = 0
        extra = combine_calls(min(len(ids), AI_TOGETHER_MAX)) \
            if together and len(ids) > 1 else 0
        total = len(ids) + extra
        _emit(progress, 0, total, 0, t0)
        for i, fid in enumerate(ids):
            row = conn.execute(
                "SELECT f.name, c.ai FROM files f LEFT JOIN summary c ON "
                "c.file_id=f.id WHERE f.id=?", (fid,)).fetchone()
            if row and (redo or not row[1]):
                try:
                    out = ai_file(conn, fid, model, url)
                    if out:
                        done += 1
                        log("{}\n    {}".format(row[0], out))
                    else:
                        log("{}\n    (no text to summarise)".format(row[0]))
                except AIError as exc:
                    log("{}: {}".format(row[0], exc))
                    if done == 0:
                        return 2
            _emit(progress, i + 1, total, done, t0)
        log("{:,} summaries written in {:.0f}s".format(done,
                                                       time.time() - t0))
        if extra:
            files = [f for f in files_by_id(conn, ids) if f["ai"]
                     or f["gist"]][:AI_TOGETHER_MAX]
            if len(files) > 1:
                made = [len(ids)]

                def tick():
                    made[0] += 1
                    _emit(progress, min(made[0], total), total, done, t0)
                try:
                    _, text = ai_combine(
                        [note_for(f) for f in files],
                        "A selection of {:,} files chosen by their owner."
                        .format(len(files)), model, url, False, tick)
                    save_digest(conn, scope, files, text, model)
                    log("\nAll {:,} together:\n{}".format(len(files), text))
                except AIError as exc:
                    log("combined summary: {}".format(exc))
        _emit(progress, total, total, done, t0)
        return 0
    finally:
        conn.close()


def find_ollama():
    """Path of the ollama program, or ''."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    home = os.path.expanduser("~")
    for cand in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                 "/Applications/Ollama.app/Contents/Resources/ollama",
                 os.path.join(home, "Applications", "Ollama.app", "Contents",
                              "Resources", "ollama"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                              "Ollama", "ollama.exe")):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def _run_logged(cmd, log):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        log("  could not run {}: {}".format(cmd[0], exc))
        return 1
    for line in proc.stdout:
        line = line.strip()
        if line:
            log("  " + line)
    return proc.wait()


def _install_ollama(log):
    """Install Ollama without admin rights: Homebrew on macOS; winget on
    Windows, and failing that its own installer run silently. Returns the
    path of the ollama program, or ''."""
    log("Ollama is not installed - installing it...")
    if sys.platform == "darwin":
        brew = shutil.which("brew") or next(
            (b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
             if os.path.isfile(b)), "")
        if brew:
            _run_logged([brew, "install", "ollama"], log)
    elif os.name == "nt":
        if shutil.which("winget"):
            _run_logged(["winget", "install", "-e", "--id", "Ollama.Ollama",
                         "--accept-source-agreements",
                         "--accept-package-agreements"], log)
        if not find_ollama():
            # winget missing or its source refused (it happens): fetch the
            # installer itself. It is per-user - no admin prompt.
            import tempfile
            import urllib.request
            setup = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
            src = "https://ollama.com/download/OllamaSetup.exe"
            log("  downloading {} ...".format(src))
            try:
                urllib.request.urlretrieve(src, setup)
                _run_logged([setup, "/VERYSILENT", "/NORESTART",
                             "/SUPPRESSMSGBOXES"], log)
            except Exception as exc:                           # noqa: BLE001
                log("  could not fetch or run the installer: {}".format(exc))
            finally:
                try:
                    os.remove(setup)
                except OSError:
                    pass
    return find_ollama()


def ai_start(url=None, log=print, install=False):
    """Make sure Ollama is answering: start it if it is installed but not
    running, and (install=True) install it first if it is missing. Returns
    the ai_status dict - check its 'ok'."""
    url = (url or AI_URL).rstrip("/")
    st = ai_status(url)
    if st["ok"]:
        return st
    exe = find_ollama()
    if not exe and install:
        exe = _install_ollama(log)
        st = ai_status(url)         # its installer may have started it
        if st["ok"]:
            return st
    if not exe:
        st["error"] = "Ollama is not installed"
        return st
    log("Starting Ollama ({})...".format(exe))
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000008       # no window, detached
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([exe, "serve"], **kwargs)
    except OSError as exc:
        st["error"] = "could not start Ollama: {}".format(exc)
        return st
    for _ in range(60):
        time.sleep(0.5)
        st = ai_status(url)
        if st["ok"]:
            break
    return st


def has_model(models, name):
    """Is `name` among the installed models? A tag ("gemma3:1b") must match
    exactly; a bare family name ("gemma3") matches any size."""
    for m in models:
        if m in (name, name + ":latest") or \
                (":" not in name and m.split(":")[0] == name):
            return True
    return False


def ai_pull(model, url=None, progress=False, log=print):
    """Download one model through the running Ollama. True on success."""
    url = (url or AI_URL).rstrip("/")
    size = dict((m, sz) for m, sz, _ in AI_MODELS).get(model)
    log("Downloading the model {}{}...".format(
        model, " ({})".format(size) if size else ""))
    t0 = time.time()
    last, tenth = "", -1
    try:
        with _http(url + "/api/pull", {"model": model, "stream": True},
                   timeout=3600) as r:
            for raw in r:
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if ev.get("error"):
                    log("  " + str(ev["error"]))
                    return False
                if ev.get("total"):
                    done, total = ev.get("completed", 0), ev["total"]
                    if progress:
                        print("@P seen={} done={} total={} elapsed={:.1f}"
                              .format(done >> 20, done >> 20, total >> 20,
                                      time.time() - t0), flush=True)
                    elif total > (50 << 20) and done * 10 // total > tenth:
                        tenth = done * 10 // total     # a build log: one
                        log("  {:>3}%  of {}".format(  # line per 10%
                            tenth * 10, findex.human(total)))
                status = ev.get("status", "")
                kind = status.split(" ")[0]
                if status and kind != last:
                    log("  " + status)
                last = kind
    except Exception as exc:                                   # noqa: BLE001
        log("  download failed: {}".format(exc))
        return False
    return True


def ai_setup(model=None, url=None, progress=False, log=print):
    """Get local AI working: install Ollama if it is missing, start it, and
    download the model(s) wanted that are not already there.

    model: one name; several separated by commas; "all" for every model in
    AI_MODELS; or None = the default small one, and only when nothing usable
    is installed yet. Returns 0 = ready; 2 = Ollama could not be installed
    or started (get it from ollama.com/download); 3 = it is running but a
    download failed."""
    url = (url or AI_URL).rstrip("/")
    st = ai_start(url, log, install=True)
    if not st["ok"]:
        log("Could not get Ollama running ({}). Download it from "
            "https://ollama.com/download , open it once, then try again."
            .format(st["error"] or "no answer at " + url))
        return 2
    if model is None:
        if pick_model(st["models"]):
            log("Local AI is ready: {}".format(", ".join(st["models"])))
            return 0
        wanted = [AI_MODEL]
    elif model.strip().lower() == "all":
        wanted = [m for m, _, _ in AI_MODELS]
    else:
        wanted = [m.strip() for m in model.split(",") if m.strip()]
    failed = []
    for name in wanted:
        if has_model(st["models"], name):
            log("{} is already installed.".format(name))
        elif not ai_pull(name, url, progress, log):
            failed.append(name)
    st = ai_status(url)
    if failed:
        log("Could not download: {}".format(", ".join(failed)))
    log("Local AI {}: {}".format(
        "is ready" if st["models"] else "has no models",
        ", ".join(st["models"]) or "-"))
    return 3 if failed else 0


# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------

def _when(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def _collect(conn, run_, under=None, per_section=0):
    secs = sections(conn, run_["id"], under)
    for s in secs:
        s["files"] = section_files(conn, s["id"], under, per_section)
    return secs


_CSS = """
body{font:14px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
margin:0;background:#f4f6f9;color:#1b1f27}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 4px}
.sub{color:#5a6475}.about{color:#5a6475;margin:2px 0 8px}
.ai{background:#fff;border-left:3px solid #3b6fd4;padding:8px 12px;
margin:8px 0;border-radius:0 6px 6px 0}
table{border-collapse:collapse;width:100%;background:#fff;
border:1px solid #d9dee7;border-radius:8px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #e8ecf2;
vertical-align:top}th{background:#eef1f6;font-weight:600}
td.n{white-space:nowrap}td.p{color:#5a6475;font-size:12px}
.toc a{color:#2456b8;text-decoration:none}.toc td{padding:4px 10px}
.more{color:#5a6475;font-size:12.5px}
@media(prefers-color-scheme:dark){body{background:#14171c;color:#e6e9ef}
table,.ai{background:#1c2027;border-color:#2c323c}th{background:#232831}
td{border-color:#2c323c}.sub,.about,td.p,.more{color:#98a2b3}
.toc a{color:#7aa7ff}}
"""


def write_html(run_, secs, out, rows_per_section=300):
    e = html.escape
    w = out.write
    scope = run_["scope"] or "Everything in the index"
    w("<!doctype html><html><head><meta charset='utf-8'><title>findex "
      "summary - {}</title><style>{}</style></head><body><main>".format(
          e(scope), _CSS))
    w("<h1>{}</h1><p class='sub'>{:,} files in {:,} sections &middot; "
      "summarised {}</p>".format(
          e(scope), sum(s["n"] for s in secs), len(secs),
          _when(run_["created"])))
    if run_.get("ai"):
        w("<div class='ai'>{}</div>".format(
            e(run_["ai"]).replace("\n", "<br>")))
    w("<table class='toc'><tr><th>Section</th><th>Files</th><th>Size</th>"
      "<th>About</th></tr>")
    for s in secs:
        w("<tr><td><a href='#s{}'>{}</a></td><td class='n'>{:,}</td>"
          "<td class='n'>{}</td><td>{}</td></tr>".format(
              s["id"], e(s["ai_title"] or s["label"]), s["n"],
              findex.human(s["bytes"]),
              e((s["ai"] or "").split("\n")[0]
                or s["terms"].replace(";", ","))))
    w("</table>")
    for s in secs:
        w("<h2 id='s{}'>{} <span class='sub'>&middot; {:,} files, {}</span>"
          "</h2>".format(s["id"], e(s["ai_title"] or s["label"]), s["n"],
                         findex.human(s["bytes"])))
        if s["ai_title"]:
            w("<p class='about'>{}</p>".format(e(s["label"])))
        if s["ai"]:
            w("<div class='ai'>{}</div>".format(
                e(s["ai"]).replace("\n", "<br>")))
        desc = describe_section(s)
        if desc:
            w("<p class='about'>{}</p>".format(e(desc).replace("\n", "<br>")))
        w("<table><tr><th>File</th><th>Kind</th><th>Summary</th>"
          "<th>Modified</th></tr>")
        for f in s["files"][:rows_per_section]:
            text = e(f["ai"] or f["gist"] or "")
            if f["keywords"]:
                text += ("<br>" if text else "") + "<span class='sub'>" \
                    + e(f["keywords"]) + "</span>"
            w("<tr><td>{}{}<div class='p'>{}</div></td><td class='n'>{}</td>"
              "<td>{}</td><td class='n'>{}</td></tr>".format(
                  e(os.path.basename(f["path"])),
                  " &mdash; <i>{}</i>".format(e(f["title"]))
                  if f["title"] else "",
                  e(os.path.dirname(f["path"])),
                  e(DOCTYPE_LABEL.get(f["doctype"], "")), text,
                  _when(f["mtime"])))
        w("</table>")
        if s["n"] > rows_per_section:
            w("<p class='more'>{:,} more not shown - export as CSV for the "
              "full list.</p>".format(s["n"] - rows_per_section))
    w("</main></body></html>")


def write_csv(run_, secs, out):
    wr = csv.writer(out)
    wr.writerow(["section", "section_summary", "path", "name", "kind",
                 "title", "key_phrases", "summary", "ai_summary", "found",
                 "size", "modified"])
    for s in secs:
        for f in s["files"]:
            wr.writerow([s["ai_title"] or s["label"], s["ai"] or "",
                         f["path"], os.path.basename(f["path"]),
                         DOCTYPE_LABEL.get(f["doctype"], ""),
                         f["title"] or "", f["keywords"] or "",
                         f["gist"] or "", f["ai"] or "",
                         entities_text(f["entities"], " | "),
                         f["size"] or 0, _when(f["mtime"])])


def write_json(run_, secs, out):
    data = {"scope": run_["scope"], "created": run_["created"],
            "files": run_["files"], "summary": run_.get("ai"),
            "overview": run_["overview"], "sections": []}
    for s in secs:
        files = []
        for f in s["files"]:
            ent = {}
            try:
                ent = json.loads(f["entities"]) if f["entities"] else {}
            except ValueError:
                pass
            files.append({"path": f["path"], "size": f["size"],
                          "modified": f["mtime"], "kind": f["doctype"],
                          "title": f["title"],
                          "key_phrases": (f["keywords"] or "").split("; ")
                          if f["keywords"] else [],
                          "summary": f["gist"], "ai_summary": f["ai"],
                          "found": ent})
        data["sections"].append({
            "id": s["id"], "label": s["label"], "title": s["ai_title"],
            "summary": s["ai"], "terms": s["terms"], "kind": s["kind"],
            "count": s["n"], "bytes": s["bytes"], "files": files})
    json.dump(data, out, ensure_ascii=False, indent=1)


def write_text(run_, secs, out, rows_per_section=40):
    w = out.write
    w("{}\n{:,} files in {:,} sections - summarised {}\n".format(
        run_["scope"] or "Everything in the index",
        sum(s["n"] for s in secs), len(secs), _when(run_["created"])))
    if run_.get("ai"):
        w("\n" + run_["ai"] + "\n")
    for s in secs:
        w("\n== {}  ({:,} files, {}) ==\n".format(
            s["ai_title"] or s["label"], s["n"], findex.human(s["bytes"])))
        if s["ai"]:
            w(s["ai"] + "\n")
        desc = describe_section(s)
        if desc:
            w(desc + "\n")
        for f in s["files"][:rows_per_section]:
            w("  {}  [{}]\n".format(f["path"],
                                    DOCTYPE_LABEL.get(f["doctype"], "file")))
            text = f["ai"] or f["gist"]
            if text:
                w("      " + text + "\n")
        if s["n"] > rows_per_section:
            w("  ...and {:,} more\n".format(s["n"] - rows_per_section))


def export(conn, run_, out_path, fmt=None, under=None):
    fmt = (fmt or os.path.splitext(out_path)[1].lstrip(".").lower()
           or "html").lower()
    if fmt not in ("txt", "csv", "json", "html", "htm"):
        raise ValueError("format must be html, txt, csv or json, not " + fmt)
    limit = 300 if fmt in ("html", "htm") else 40 if fmt == "txt" else 0
    secs = _collect(conn, run_, under, limit)
    if under:       # a folder inside the run: headed as that folder, and
        run_ = dict(run_, scope=norm_scope(under), ai=None)   # not its text
    with open(out_path, "w", encoding="utf-8", newline="") as out:
        if fmt in ("html", "htm"):
            write_html(run_, secs, out)
        elif fmt == "csv":
            write_csv(run_, secs, out)
        elif fmt == "json":
            write_json(run_, secs, out)
        else:
            write_text(run_, secs, out)
    return fmt


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _print_run(conn, run_, exact, scope):
    secs = sections(conn, run_["id"], None if exact else scope)
    where = run_["scope"] or "the whole index"
    if not exact:
        print("(no summary of this folder itself - showing the sections of "
              "{} that reach into it)".format(where))
    print("{}: {:,} files in {:,} sections, summarised {}".format(
        scope or where, sum(s["n"] for s in secs), len(secs),
        _when(run_["created"])))
    if run_.get("ai") and exact:
        print("\n" + run_["ai"])
    print()
    for s in secs:
        print("  #{:<5} {:>8,}  {:>8}  {}".format(
            s["id"], s["n"], findex.human(s["bytes"]),
            (s["ai_title"] + "  [" + s["label"] + "]") if s["ai_title"]
            else s["label"]))
        for line in (s["ai"] or "").split("\n"):
            if line:
                print("                             " + line)


def cmd_summarise(args):
    log = print
    url = args.ai_url
    if args.ai_status:
        st = ai_status(url)
        if st["ok"]:
            print("Local AI is running at {} - models: {}".format(
                url or AI_URL, ", ".join(st["models"]) or "none installed"))
            print("Would use: " + (pick_model(st["models"], args.model)
                                   or "(nothing - run --ai-setup)"))
        else:
            print("Local AI is not running at {} ({})".format(
                url or AI_URL, st["error"]))
            print("ollama program: " + (find_ollama() or "not found"))
        return 0 if st["ok"] else 1
    if args.ai_models:
        st = ai_status(url)
        have = set(st["models"])
        print("Small, fast models for summaries (findex summarise "
              "--ai-setup --model NAME downloads one):")
        for name, size, note in AI_MODELS:
            print("  {:<16} {:>7}  {}{}".format(
                name, size, note, "   [installed]" if name in have else ""))
        other = sorted(have - {m for m, _, _ in AI_MODELS})
        if other:
            print("Also installed: " + ", ".join(other))
        return 0
    if args.ai_setup:
        return ai_setup(args.model, url, args.progress, log)
    if args.ai_ids:
        ids = [int(x) for x in re.split(r"[,\s]+", args.ai_ids) if x.isdigit()]
        return ai_run_files(args.db, ids, args.model, url, args.progress,
                            log, args.redo, args.folder, not args.each)
    if args.ai_files:
        conn = findex.open_db_ro(args.db)
        ids = []
        for p in args.ai_files:
            row = conn.execute("SELECT id FROM files WHERE path=?",
                               (os.path.abspath(p),)).fetchone() \
                or conn.execute("SELECT id FROM files WHERE path=?",
                                (p,)).fetchone()
            if row:
                ids.append(row[0])
            else:
                print("not in the index: " + p)
        conn.close()
        return ai_run_files(args.db, ids, args.model, url, args.progress,
                            log, args.redo, args.folder, not args.each)
    if args.ai_section is not None:
        conn = findex.open_db_ro(args.db)
        ids = [f["id"] for f in section_files(
            conn, args.ai_section, limit=args.limit or 0)
            if f["doctype"] and f["doctype"] not in NON_PROSE] \
            if have_tables(conn) else []
        conn.close()
        if not ids:
            print("No readable files in section #{}".format(args.ai_section))
            return 1
        return ai_run_files(args.db, ids, args.model, url, args.progress,
                            log, args.redo, args.folder, not args.each)
    if args.forget or args.forget_all:
        conn = findex.open_db(args.db)
        n = forget(conn, None if args.forget_all else args.folder or "")
        conn.close()
        print("All summaries removed." if n < 0 else
              "Summary removed." if n else "No summary of that folder.")
        return 0
    if args.ai_sections:
        return ai_run_sections(args.db, args.folder, args.model, url,
                               args.progress, log, args.redo)

    reading = args.show or args.out or args.section is not None
    if args.section is not None:
        conn = findex.open_db_ro(args.db)
        rows = section_files(conn, args.section, limit=args.limit or 200) \
            if have_tables(conn) else []
        conn.close()
        for f in rows:
            print("{:>9}  {}  {}".format(findex.human(f["size"] or 0),
                                         _when(f["mtime"]), f["path"]))
            line = " | ".join(x for x in (
                DOCTYPE_LABEL.get(f["doctype"]), f["title"],
                f["keywords"]) if x)
            if line:
                print("           " + line)
            if f["ai"] or f["gist"]:
                print("           " + (f["ai"] or f["gist"]))
        if not rows:
            print("No such section (see: findex summarise --show)")
        return 0
    if not reading:
        if run(args.db, args.folder, args.detail, args.workers, args.rebuild,
               args.progress, log) is None:
            return 1
        if args.ai:
            code = ai_run_sections(args.db, args.folder, args.model, url,
                                   args.progress, log)
            if code:
                return code
        if args.progress:
            return 0
    conn = findex.open_db_ro(args.db)
    try:
        run_, exact = find_run(conn, args.folder)
        if not run_:
            print("No summary yet for {} - run: findex summarise {}".format(
                args.folder or "the index", args.folder or ""))
            return 1
        scope = norm_scope(args.folder)
        if args.out:
            fmt = export(conn, run_, args.out, args.format,
                         None if exact else scope)
            print("Wrote {} ({})".format(args.out, fmt))
        else:
            _print_run(conn, run_, exact, scope)
    finally:
        conn.close()
    return 0


def add_commands(sub):
    p = sub.add_parser(
        "summarise", aliases=["summarize", "summary"],
        help="sort a folder's files into labelled sections by what they "
             "are about, with a card per file; --ai adds summaries written "
             "by a local model")
    p.add_argument("folder", nargs="?", default="",
                   help="the folder to summarise (none = the whole index)")
    p.add_argument("--detail", choices=tuple(DETAILS), default="normal",
                   help="fewer, broader sections or more, narrower ones")
    p.add_argument("--rebuild", action="store_true",
                   help="re-read every file's text, ignoring stored cards")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--show", action="store_true",
                   help="print the stored sections; do not run")
    p.add_argument("--section", type=int, metavar="ID",
                   help="list the files of one section")
    p.add_argument("-n", "--limit", type=int, default=0)
    p.add_argument("-o", "--out", help="export the stored summary here "
                   "(.html, .csv, .json or .txt by extension)")
    p.add_argument("-f", "--format", choices=("html", "csv", "json", "txt"))
    p.add_argument("--forget", action="store_true",
                   help="remove this folder's sections")
    p.add_argument("--forget-all", action="store_true",
                   help="remove every summary and card")
    p.add_argument("--ai", action="store_true",
                   help="after the run, have the local model write a "
                        "combined summary of each section's files and one "
                        "of the whole folder")
    p.add_argument("--ai-sections", action="store_true",
                   help="only that - on the summary already stored")
    p.add_argument("--ai-files", nargs="+", metavar="PATH",
                   help="write a summary of each of these files with the "
                        "local model, then one of them all together")
    p.add_argument("--ai-ids", metavar="IDS", help=argparse.SUPPRESS)
    p.add_argument("--ai-section", type=int, metavar="ID",
                   help="write summaries of the readable files in a section "
                        "(-n caps how many)")
    p.add_argument("--each", action="store_true",
                   help="with --ai-files / --ai-section: only the summary "
                        "of each file, not the combined one of them all")
    p.add_argument("--redo", action="store_true",
                   help="rewrite AI summaries that already exist")
    p.add_argument("--model", help="Ollama model to use (default: the "
                   "first suitable one installed). With --ai-setup: the "
                   "model(s) to download - a name, several separated by "
                   "commas, or 'all' for every suggested one")
    p.add_argument("--ai-url", default=None,
                   help="Ollama address (default {})".format(AI_URL))
    p.add_argument("--ai-status", action="store_true",
                   help="is a local model available?")
    p.add_argument("--ai-setup", action="store_true",
                   help="install/start Ollama and download a model "
                        "(--model picks which; default {})".format(AI_MODEL))
    p.add_argument("--ai-models", action="store_true",
                   help="list the small, fast models findex suggests")
    p.add_argument("--progress", action="store_true",
                   help="emit @P progress lines (used by the desktop app)")
    p.set_defaults(func=cmd_summarise)


if __name__ == "__main__":
    sys.exit(findex.main())
