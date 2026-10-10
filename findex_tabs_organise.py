#!/usr/bin/env python3
"""
findex_tabs_organise - the Organise tab of the desktop app: sort the files
under a folder into subfolders by rules, with suggestions, a rule check,
a preview of the resulting tree, a summary, apply and undo. A mixin that
findex_tabs.ToolTabs inherits; the engine is findex_organise.py.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import re

import findex
import findex_organise
import findex_rename

RULES_HELP = """\
THE SIMPLE BUILDER (the default view)

Each rule is one row: which files, and which folder they go into.
    name starts with / contains / ends with / is exactly   + the text
    file type is        Pictures, Videos, Music, Documents, Zip, Programs...
    extension is        pdf, docx
    older than / newer than      3 years, 6 months, 30 days
    anything else       every file no earlier rule took - put it last
"go into folder" is the folder to make under the one being organised
(Finance/Invoices puts a folder inside a folder); "then a subfolder per"
splits it further - per year, per year and month, per file type...
Rules are checked top to bottom and the first that fits wins, so put
specific rules above general ones (Up / Down move them; Remove takes one
out). Press "Suggest rules" to have the rows drafted from the file names,
then adjust them. Problems with a rule appear underneath it.

THE TEXT SYNTAX (More > Advanced: edit rules as text)

One rule per line - first match wins.  Comments start with #.

    pattern [pattern ...] -> destination

Patterns (several on one line must all match):
    Invoice*              glob on the file name, case-insensitive
    "Board minutes*"      quote a glob that has spaces
    re:^([A-Z]{3})-\\d+    regular expression (rei: ignores case); quote it
                          if it has spaces: re:"^Board (minutes|agenda)"
    ext:pdf;docx          extension(s)
    type:images           images videos audio documents compressed code
                          programs emails
    year:2019-2021        modified in these years   older:3y   newer:30d
    *                     everything - the sweep; put it last

Destination: a folder path under the folder being organised, with tokens
    {1} {2}..   regex groups          {name} {stem} {ext}   the file's own
    {type}      type group            {first}  first word of the name
    {year} {month} {day} {date} {yyyymm}   from the modified date
    {parent}    the folder it came from
    {1|upper}  {first|title}  {name|lower}  case filters on any token

Examples
    re:"^(INV|Invoice)[-_ ]?(\\d{4})" -> Finance/Invoices/{2}
    "board minutes*"                 -> Governance/Minutes/{year}
    type:images                      -> Photos/{year}/{month}
    re:^([A-Z]{3})-\\d+               -> Clients/{1}
    ext:exe;msi                      -> Software
    older:5y                         -> Archive/{year}
    *                                -> _Unsorted

Nothing is ever overwritten: a different file already at the target is a
collision and is skipped. Apply moves (or copies) as one batch that Undo
reverses exactly, folders created and removed included.
"""


def _g():
    return sys.modules["findex_gui"]


# The simple builder's vocabulary. Each row is a dict:
#   cond   one of COND's keys      value  the text / type key / age
#   dest   folder                  sub    one of SUBS' keys ("" = none)
#   raw    the rule text when the builder has no shape for it (else None)
#   note   a comment shown under the row (the suggester's reason)
COND = (("starts", "name starts with"), ("contains", "name contains"),
        ("ends", "name ends with"), ("exact", "name is exactly"),
        ("type", "file type is"), ("ext", "extension is"),
        ("older", "older than"), ("newer", "newer than"),
        ("any", "anything else"))
TYPES = (("images", "Pictures"), ("videos", "Videos"),
         ("audio", "Music & audio"), ("documents", "Documents"),
         ("compressed", "Zip & archives"), ("programs", "Programs & installers"),
         ("emails", "Emails"), ("code", "Code & scripts"))
SUBS = (("", "nothing more"), ("year", "year"), ("ym", "year, then month"),
        ("type", "file type"), ("first", "first word of the name"),
        ("ext", "extension"))
_SUB_SUFFIX = {"year": "/{year}", "ym": "/{year}/{month}", "type": "/{type}",
               "first": "/{first|title}", "ext": "/{ext}"}
_AGE_UNITS = {"day": "d", "days": "d", "d": "d", "week": "w", "weeks": "w",
              "w": "w", "month": "m", "months": "m", "m": "m", "year": "y",
              "years": "y", "y": "y", "yr": "y", "yrs": "y"}


def _quote_glob(text):
    return '"{}"'.format(text) if (" " in text or not text) else text


def _age_spec(text):
    """'3 years' / '30 days' / '6m' -> '3y' / '30d' / '6m' (None if unclear)."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$", text or "")
    if not m:
        return None
    unit = _AGE_UNITS.get(m.group(2).lower() or "days")
    return (m.group(1) + unit) if unit else None


def row_to_line(row):
    """One builder row -> one rule line (or its raw text)."""
    if row.get("raw") is not None:
        return row["raw"]
    cond, value = row.get("cond", "starts"), (row.get("value") or "").strip()
    dest = (row.get("dest") or "").strip().strip("/\\")
    dest = dest + _SUB_SUFFIX.get(row.get("sub") or "", "")
    if cond == "starts":
        pat = _quote_glob(value + "*")
    elif cond == "contains":
        pat = _quote_glob("*" + value + "*")
    elif cond == "ends":
        pat = 'rei:"{}(\\.[^.]+)?$"'.format(re.escape(value))
    elif cond == "exact":
        pat = 'rei:"^{}(\\.[^.]+)?$"'.format(re.escape(value))
    elif cond == "type":
        pat = "type:" + (value or "documents")
    elif cond == "ext":
        exts = [e.strip().lstrip(".") for e in
                re.split(r"[,; ]+", value) if e.strip()]
        pat = "ext:" + ";".join(exts)
    elif cond in ("older", "newer"):
        pat = "{}:{}".format(cond, _age_spec(value) or "?")
    else:
        pat = "*"
    return "{} -> {}".format(pat, dest or "?")


def rows_to_text(rows):
    lines = []
    for row in rows:
        if row.get("note"):
            lines.append("# " + row["note"])
        lines.append(row_to_line(row))
    return "\n".join(lines)


def rows_lines(rows):
    """The rule-line number each row generates (for matching issues)."""
    out, n = [], 0
    for row in rows:
        if row.get("note"):
            n += 1
        n += 1
        out.append(n)
    return out


_ENDS = re.compile(r'^rei:"(.*)\(\\\.\[\^\.\]\+\)\?\$"$')
_EXACT = re.compile(r'^rei:"\^(.*)\(\\\.\[\^\.\]\+\)\?\$"$')


def _unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


def line_to_row(line, note=""):
    """A rule line -> a builder row; a shape the builder lacks -> raw."""
    row = {"cond": "starts", "value": "", "dest": "", "sub": "", "raw": None,
           "note": note}
    sep = "->" if "->" in line else ("=>" if "=>" in line else None)
    if sep is None:
        row["raw"] = line
        return row
    left, _, right = line.partition(sep)
    left, dest = left.strip(), right.strip().strip('"')
    for key, suffix in _SUB_SUFFIX.items():
        if dest.endswith(suffix):
            row["sub"] = key
            dest = dest[:-len(suffix)]
            break
    if "{" in dest:
        row["raw"] = line
        return row
    row["dest"] = dest
    toks = findex_organise._tokens(left)
    if len(toks) != 1:
        row["raw"] = line
        return row
    tok = toks[0]
    low = tok.lower()
    m = _ENDS.match(left) or None
    if left == "*":
        row["cond"] = "any"
    elif low.startswith("type:"):
        g = tok[5:].split(";")[0].lower()
        if g not in dict(TYPES) or ";" in tok:
            row["raw"] = line
            return row
        row["cond"], row["value"] = "type", g
    elif low.startswith("ext:"):
        row["cond"], row["value"] = "ext", ", ".join(
            e for e in tok[4:].split(";") if e)
    elif low.startswith(("older:", "newer:")):
        kind, spec = low.split(":", 1)
        mm = re.fullmatch(r"(\d+(?:\.\d+)?)([dwmy])", spec)
        if not mm:
            row["raw"] = line
            return row
        row["cond"] = kind
        row["value"] = "{} {}".format(mm.group(1), {
            "d": "days", "w": "weeks", "m": "months", "y": "years"}[mm.group(2)])
    elif _EXACT.match(left):
        row["cond"], row["value"] = "exact", _unescape(_EXACT.match(left).group(1))
    elif m:
        row["cond"], row["value"] = "ends", _unescape(m.group(1))
    elif low.startswith(("re:", "rei:", "year:")):
        row["raw"] = line
        return row
    else:
        if tok.startswith("*") and tok.endswith("*") and len(tok) > 2 \
                and "*" not in tok[1:-1] and "?" not in tok:
            row["cond"], row["value"] = "contains", tok[1:-1]
        elif tok.endswith("*") and "*" not in tok[:-1] and "?" not in tok \
                and "[" not in tok:
            row["cond"], row["value"] = "starts", tok[:-1]
        else:
            row["raw"] = line
            return row
    return row


def text_to_rows(text):
    rows, note = [], ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if body.startswith("---") or body.startswith("suggested by") \
                    or body.startswith("note:"):
                continue
            note = body
            continue
        rows.append(line_to_row(line, note))
        note = ""
    return rows


class OrganiseTab:

    def _build_organise_tab(self):
        t = self.tab_organise
        self._oplan = None
        self._opaths = {}
        self._oafter = None
        self._ogen = 0
        self.var_oroot = tk.StringVar(value="")
        self.var_ocopy = tk.BooleanVar(value=False)
        self.var_osweep = tk.BooleanVar(value=False)
        self.var_osweep_name = tk.StringVar(value="_Unsorted")
        self.var_oident = tk.BooleanVar(value=True)
        self.var_oempty = tk.BooleanVar(value=True)
        self.var_otemplate = tk.StringVar(value="")
        self.var_oadvanced = tk.BooleanVar(value=False)
        self.var_ostatus = tk.StringVar(value="Choose a folder, then press "
                                              "Suggest rules or add your own.")
        self.var_oapply = tk.StringVar(value="Move the files...")

        # ---- 1. which folder --------------------------------------------
        step1 = ttk.Frame(t)
        step1.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(step1, text="1.  Folder to tidy:").pack(side="left")
        e = ttk.Entry(step1, textvariable=self.var_oroot)
        e.pack(side="left", fill="x", expand=True, padx=(8, 4))
        self.tip(e, "Everything in this folder - subfolders included - is "
                    "sorted into subfolders of it by the rules below.",
                 popup=False)

        def browse():
            d = filedialog.askdirectory(title="Folder to tidy",
                                        initialdir=self.var_oroot.get() or None)
            if d:
                self.var_oroot.set(os.path.normpath(d))
        ttk.Button(step1, text="Browse...", width=9, command=browse).pack(side="left")
        b = ttk.Button(step1, text="Suggest rules", width=13,
                       style="Accent.TButton", command=self.organise_suggest)
        b.pack(side="left", padx=(12, 0))
        self.tip(b, "Look at the file names and draft the rules for you - "
                    "files that start with the same word, reference codes, "
                    "date-named files, then by type. Each suggested rule says "
                    "how many files it covers. Change or remove any of them.")
        self.omore = ttk.Menubutton(step1, text="More", width=6)
        self.omore.pack(side="left", padx=(6, 0))
        self.omore_menu = tk.Menu(self.omore, tearoff=0)
        self._menus.append(self.omore_menu)
        self.omore["menu"] = self.omore_menu
        self.otemplate_menu = tk.Menu(self.omore_menu, tearoff=0,
                                      postcommand=self._organise_templates)
        self._menus.append(self.otemplate_menu)
        self.omore_menu.add_cascade(label="Load saved rules", menu=self.otemplate_menu)
        self.omore_menu.add_command(label="Save these rules as...",
                                    command=self.organise_save_template)
        self.omore_menu.add_command(label="Delete saved rules...",
                                    command=self.organise_delete_template)
        self.omore_menu.add_separator()
        self.omore_menu.add_command(label="Check the rules for problems",
                                    command=lambda: (self._organise_plan(),
                                                     self.onb.select(self.otab_issues)))
        self.omore_menu.add_command(label="Export the plan...",
                                    command=self.organise_export)
        self.omore_menu.add_separator()
        self.omore_menu.add_checkbutton(label="Advanced: edit rules as text",
                                        variable=self.var_oadvanced,
                                        command=self._organise_toggle_view)
        self.omore_menu.add_command(label="Help with rules",
                                    command=self.show_organise_help)
        self.tip(self.omore, "Saved rule sets, a problem check, exporting the "
                             "plan, and the text editor for rules.")

        # ---- 2. the rules ------------------------------------------------
        step2 = ttk.Frame(t)
        step2.pack(fill="x", padx=12, pady=(8, 2))
        self._ostep2 = step2
        ttk.Label(step2, text="2.  Rules  -  read top to bottom; the first "
                              "that fits a file wins").pack(side="left")
        b = ttk.Button(step2, text="+ Add a rule", command=self._organise_add_row)
        b.pack(side="right")
        self.tip(b, "Add a blank rule: what to look for, and the folder those "
                    "files should go into.")
        b = ttk.Button(step2, text="Clear all rules", command=self._organise_clear_rows)
        b.pack(side="right", padx=(0, 6))

        self.obuilder = ttk.Frame(t)
        self.obuilder.pack(fill="x", padx=12, pady=(0, 4))
        self._orows = []                       # [dict] - the builder's model
        self._orow_widgets = []
        canvas_holder = ttk.Frame(self.obuilder)
        canvas_holder.pack(fill="x")
        self.ocanvas = tk.Canvas(canvas_holder, highlightthickness=0,
                                 borderwidth=0, height=190)
        vsb = ttk.Scrollbar(canvas_holder, orient="vertical",
                            command=self.ocanvas.yview)
        self.ocanvas.configure(yscrollcommand=vsb.set)
        self.ocanvas.pack(side="left", fill="x", expand=True)
        vsb.pack(side="right", fill="y")
        self.orows_frame = ttk.Frame(self.ocanvas)
        self._ocanvas_win = self.ocanvas.create_window(
            (0, 0), window=self.orows_frame, anchor="nw")
        self.orows_frame.bind("<Configure>", lambda e: self.ocanvas.configure(
            scrollregion=self.ocanvas.bbox("all")))
        self.ocanvas.bind("<Configure>", lambda e: self.ocanvas.itemconfigure(
            self._ocanvas_win, width=e.width))

        # advanced: the same rules as text (swapped in for the builder)
        self.otextframe = ttk.Frame(t)
        holder = ttk.Frame(self.otextframe)
        holder.pack(fill="both", expand=True)
        self.rules_text = tk.Text(holder, height=10, wrap="none", relief="flat",
                                  padx=8, pady=6, undo=True)
        vsb = ttk.Scrollbar(holder, orient="vertical",
                            command=self.rules_text.yview)
        self.rules_text.configure(yscrollcommand=vsb.set)
        self.rules_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.rules_text.bind("<<Modified>>", self._organise_text_changed)
        self.tip(self.rules_text, "The rules as text - one per line, first "
                                  "match wins. More > Help with rules has the "
                                  "syntax.", popup=False)
        self._orules = ""                      # the rules, as the engine sees them
        self._osyncing = False

        # ---- 3. what to do -----------------------------------------------
        step3 = ttk.Frame(t)
        step3.pack(fill="x", padx=12, pady=(6, 2))
        ttk.Label(step3, text="3.  Then:").pack(side="left")
        rb = ttk.Radiobutton(step3, text="Move the files", value=False,
                             variable=self.var_ocopy)
        rb.pack(side="left", padx=(8, 0))
        self.tip(rb, "Move files into their folders. Instant on the same "
                     "drive; folders left empty can be tidied away.")
        rb = ttk.Radiobutton(step3, text="Copy them (keep the originals)",
                             value=True, variable=self.var_ocopy)
        rb.pack(side="left", padx=(10, 0))
        self.tip(rb, "Copy files into their folders and leave the originals "
                     "where they are. Undo removes the copies.")
        chk = ttk.Checkbutton(step3, text="Files no rule fits go into",
                              variable=self.var_osweep)
        chk.pack(side="left", padx=(24, 0))
        e = ttk.Entry(step3, textvariable=self.var_osweep_name, width=12)
        e.pack(side="left", padx=(4, 0))
        for w in (chk, e):
            self.tip(w, "Normally a file no rule fits stays where it is. Tick "
                        "this to gather those files into one folder instead.")
        chk = ttk.Checkbutton(step3, text="Skip a file if an identical copy is "
                                          "already there",
                              variable=self.var_oident)
        chk.pack(side="left", padx=(16, 0))
        self.tip(chk, "If the destination already holds a byte-for-byte "
                      "identical file, count the file as done rather than "
                      "reporting a clash. Nothing is ever overwritten either "
                      "way.")
        chk = ttk.Checkbutton(step3, text="Tidy away folders left empty",
                              variable=self.var_oempty)
        chk.pack(side="left", padx=(16, 0))
        self.tip(chk, "After moving, remove the folders the moves emptied.")

        # ---- what will happen --------------------------------------------
        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(6, 6))
        self.onb = ttk.Notebook(body)
        self.onb.pack(fill="both", expand=True)
        self.otab_plan = ttk.Frame(self.onb)
        self.otab_tree = ttk.Frame(self.onb)
        self.otab_issues = ttk.Frame(self.onb)
        self.otab_summary = ttk.Frame(self.onb)
        self.onb.add(self.otab_plan, text=" What will happen ")
        self.onb.add(self.otab_tree, text=" Folders afterwards ")
        self.onb.add(self.otab_issues, text=" Problems ")
        self.onb.add(self.otab_summary, text=" Summary ")

        h, self.otree_plan = self._tool_tree(
            self.otab_plan, ("action", "file", "target", "note"),
            [("action", "Action", 90, "w", False),
             ("file", "File", 300, "w", True),
             ("target", "Goes to", 300, "w", True),
             ("note", "Note", 220, "w", False)])
        h.pack(fill="both", expand=True)
        self._make_ctx(self.otree_plan, "_opaths")

        h, self.otree_tree = self._tool_tree(
            self.otab_tree, ("#tree", "files", "size"),
            [("#0", "Folder / file", 420, "w", True),
             ("files", "Files", 80, "e", False),
             ("size", "Size", 90, "e", False)])
        h.pack(fill="both", expand=True)
        self.tip(self.otree_tree, "How the folder would look afterwards: "
                                  "every destination folder with its counts, "
                                  "and the first few files in each.",
                 popup=False)

        h, self.otree_issues = self._tool_tree(
            self.otab_issues, ("level", "line", "message"),
            [("level", "Level", 80, "w", False),
             ("line", "Rule", 60, "e", False),
             ("message", "What", 600, "w", True)])
        h.pack(fill="both", expand=True)
        self.otree_issues.bind("<Double-1>", lambda e: self._organise_goto_issue())
        self.tip(self.otree_issues, "Problems with the rules. The same "
                                    "messages appear under the rule itself.",
                 popup=False)

        self.osummary = tk.Text(self.otab_summary, wrap="word", relief="flat",
                                padx=10, pady=8, state="disabled")
        self.osummary.pack(fill="both", expand=True)

        foot = ttk.Frame(t)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_ostatus,
                  style="Dim.TLabel").pack(side="left", fill="x", expand=True)
        self.obtn_apply = ttk.Button(foot, textvariable=self.var_oapply,
                                     width=22, style="Accent.TButton",
                                     command=self.organise_apply)
        self.obtn_apply.pack(side="right")
        self.tip(self.obtn_apply, "Do what the plan shows, as one batch. Asks "
                                  "first. Undo reverses the whole batch.")
        b = ttk.Button(foot, text="Undo the last run...", width=18,
                       command=self.organise_undo)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Put everything back the way it was before the last run - "
                    "files, and any folders made or removed.")

        for var in (self.var_oroot, self.var_ocopy, self.var_osweep,
                    self.var_osweep_name, self.var_oident, self.var_oempty):
            var.trace_add("write", lambda *_: self._organise_debounce())
        self._organise_render_rows()

    # -- theming hook (called from _theme_tools) ---------------------------

    def _theme_organise(self, c, pal):
        for w in (getattr(self, "rules_text", None),
                  getattr(self, "osummary", None)):
            if w is None:
                continue
            w.configure(background=c["panel"], foreground=c["text"],
                        insertbackground=c["accent"],
                        selectbackground=c["sel"], selectforeground=c["text"],
                        font=(self.mono_family, self.ui_size),
                        relief="flat", highlightthickness=1,
                        highlightbackground=c["border"],
                        highlightcolor=c["accent"])
        if getattr(self, "rules_text", None) is not None:
            self.rules_text.tag_configure("errline", background=pal["hit_bg"])

    # -- rules text --------------------------------------------------------

    def _organise_rules(self):
        return self._orules

    def _organise_set_rules(self, text):
        """New rules from outside (a suggestion, a template): update the
        model, both views, and the plan."""
        self._orules = text.rstrip("\n")
        self._osyncing = True
        try:
            self.rules_text.configure(state="normal")
            self.rules_text.delete("1.0", "end")
            self.rules_text.insert("1.0", self._orules)
            try:
                self.rules_text.edit_modified(False)
            except tk.TclError:
                pass
            self._orows = text_to_rows(self._orules)
            self._organise_render_rows()
        finally:
            self._osyncing = False
        self._organise_debounce()

    def _organise_text_changed(self, _event=None):
        if self._osyncing:
            return
        try:
            if not self.rules_text.edit_modified():
                return
            self.rules_text.edit_modified(False)
        except tk.TclError:
            pass
        self._orules = self.rules_text.get("1.0", "end").rstrip("\n")
        self._organise_debounce()

    def _organise_toggle_view(self):
        """Builder <-> text. Going to text shows the rules as they are;
        coming back parses them into rows (hand-written rules the builder
        has no shape for become 'advanced' rows). Whichever panel is shown
        sits between step 2 and step 3."""
        if self.var_oadvanced.get():
            self.obuilder.pack_forget()
            self.otextframe.pack(fill="x", padx=12, pady=(0, 4),
                                 after=self._ostep2)
        else:
            self._orows = text_to_rows(self._orules)
            self._organise_render_rows()
            self.otextframe.pack_forget()
            self.obuilder.pack(fill="x", padx=12, pady=(0, 4), after=self._ostep2)

    # -- the simple builder --------------------------------------------------

    def _organise_rows_changed(self):
        """A builder row was edited: regenerate the text and re-plan."""
        if self._osyncing:
            return
        self._orules = rows_to_text(self._orows)
        self._osyncing = True
        try:
            self.rules_text.configure(state="normal")
            self.rules_text.delete("1.0", "end")
            self.rules_text.insert("1.0", self._orules)
            try:
                self.rules_text.edit_modified(False)
            except tk.TclError:
                pass
        finally:
            self._osyncing = False
        self._organise_debounce()

    def _organise_add_row(self, row=None):
        self._orows.append(row or {"cond": "starts", "value": "", "dest": "",
                                   "sub": "", "raw": None, "note": ""})
        self._organise_render_rows()
        self._organise_rows_changed()

    def _organise_clear_rows(self):
        if self._orows and not messagebox.askyesno(
                "Remove all rules?", "Clear every rule from the list?"):
            return
        self._orows = []
        self._organise_render_rows()
        self._organise_rows_changed()

    def _organise_row_op(self, i, op):
        rows = self._orows
        if op == "del":
            rows.pop(i)
        elif op == "up" and i > 0:
            rows[i - 1], rows[i] = rows[i], rows[i - 1]
        elif op == "down" and i < len(rows) - 1:
            rows[i + 1], rows[i] = rows[i], rows[i + 1]
        self._organise_render_rows()
        self._organise_rows_changed()

    def _organise_render_rows(self):
        """Rebuild the builder's widgets from the model."""
        for w in self._orow_widgets:
            w.destroy()
        self._orow_widgets = []
        frame = self.orows_frame
        if not self._orows:
            lbl = ttk.Label(frame, style="Dim.TLabel", text=(
                "No rules yet.  Press  Suggest rules  to have them drafted from "
                "the file names, or  + Add a rule  to write your own."))
            lbl.pack(fill="x", padx=6, pady=12)
            self._orow_widgets.append(lbl)
            return
        for i, row in enumerate(self._orows):
            box = ttk.Frame(frame, padding=(4, 3))
            box.pack(fill="x", pady=(0, 1))
            self._orow_widgets.append(box)
            self._organise_render_row(box, i, row)

    def _organise_render_row(self, box, i, row):
        """One rule as one line:
        [n.] [condition v] [value]  go into  [folder]  then a subfolder per
        [x v]   [Up] [Down] [Remove]   - with the note / problem underneath."""
        cond_labels = [lbl for k, lbl in COND]
        line = ttk.Frame(box)
        line.pack(fill="x")
        ttk.Label(line, text="{}.".format(i + 1), width=3).pack(side="left")
        if row.get("raw") is not None:
            raw = row["raw"]
            shown = raw if len(raw) <= 70 else raw[:67] + "..."
            lbl = ttk.Label(line, text="advanced rule:  " + shown,
                            style="Dim.TLabel")
            lbl.pack(side="left", fill="x", expand=True, padx=(4, 4))
            self.tip(lbl, "A rule written in the text syntax that the builder "
                          "has no boxes for (a pattern such as a reference "
                          "code). It works as it is; to change it, use More > "
                          "Advanced.\n\n" + raw)
        else:
            var_c = tk.StringVar(value=dict(COND).get(row["cond"], cond_labels[0]))
            cb = ttk.Combobox(line, textvariable=var_c, width=16, state="readonly",
                              values=cond_labels)
            cb.pack(side="left")
            self.tip(cb, "What to look for. 'Everything else' catches every "
                         "file no earlier rule took - put it last.")

            var_v = tk.StringVar(value=row.get("value", ""))
            if row["cond"] == "type":
                ev = ttk.Combobox(line, textvariable=var_v, width=19,
                                  state="readonly",
                                  values=[lbl for k, lbl in TYPES])
                var_v.set(dict(TYPES).get(row.get("value", ""),
                                          row.get("value", "")))
            elif row["cond"] == "any":
                ev = ttk.Label(line, text="(every remaining file)", width=21,
                               style="Dim.TLabel")
            else:
                ev = ttk.Entry(line, textvariable=var_v, width=21)
                hint = {"starts": "e.g.  Invoice", "contains": "e.g.  minutes",
                        "ends": "e.g.  final   (the part before .pdf)",
                        "exact": "e.g.  Thumbs.db", "ext": "e.g.  pdf, docx",
                        "older": "e.g.  3 years  /  6 months  /  30 days",
                        "newer": "e.g.  30 days"}.get(row["cond"], "")
                if hint:
                    self.tip(ev, hint)
            ev.pack(side="left", padx=(4, 0))

            def on_cond(_e=None, r=row, vc=var_c):
                key = {lbl: k for k, lbl in COND}[vc.get()]
                if key != r["cond"]:
                    r["cond"] = key
                    if key in ("type", "any"):
                        r["value"] = TYPES[0][0] if key == "type" else ""
                    self._organise_render_rows()
                    self._organise_rows_changed()
            cb.bind("<<ComboboxSelected>>", on_cond)

            def on_value(_e=None, r=row, vv=var_v):
                v = vv.get()
                if r["cond"] == "type":
                    v = {lbl: k for k, lbl in TYPES}.get(v, v)
                if v != r.get("value"):
                    r["value"] = v
                    self._organise_rows_changed()
            if isinstance(ev, ttk.Combobox):
                ev.bind("<<ComboboxSelected>>", on_value)
            elif isinstance(ev, ttk.Entry):
                ev.bind("<KeyRelease>", on_value)
                ev.bind("<FocusOut>", on_value)

            ttk.Label(line, text="  go into").pack(side="left")
            var_d = tk.StringVar(value=row.get("dest", ""))
            ed = ttk.Entry(line, textvariable=var_d, width=20)
            ed.pack(side="left", padx=(4, 0))
            self.tip(ed, "The folder to put them in, made inside the folder "
                         "being tidied. Use / for a folder inside a folder: "
                         "Finance/Invoices")

            def on_dest(_e=None, r=row, vd=var_d):
                if vd.get() != r.get("dest"):
                    r["dest"] = vd.get()
                    self._organise_rows_changed()
            ed.bind("<KeyRelease>", on_dest)
            ed.bind("<FocusOut>", on_dest)
            ttk.Label(line, text="  then a subfolder per").pack(side="left")
            var_s = tk.StringVar(value=dict(SUBS).get(row.get("sub", ""),
                                                      SUBS[0][1]))
            cs = ttk.Combobox(line, textvariable=var_s, width=14,
                              state="readonly", values=[lbl for k, lbl in SUBS])
            cs.pack(side="left", padx=(4, 0))
            self.tip(cs, "Optionally split that folder further - a subfolder "
                         "per year, per year and month, per file type...")

            def on_sub(_e=None, r=row, vs=var_s):
                key = {lbl: k for k, lbl in SUBS}[vs.get()]
                if key != r.get("sub", ""):
                    r["sub"] = key
                    self._organise_rows_changed()
            cs.bind("<<ComboboxSelected>>", on_sub)

        ops = ttk.Frame(line)
        ops.pack(side="right")
        for text, op, width, tip in (("Up", "up", 4, "Move this rule up - "
                                                    "earlier rules win"),
                                     ("Down", "down", 5, "Move this rule down"),
                                     ("Remove", "del", 7, "Take this rule out")):
            b = ttk.Button(ops, text=text, width=width,
                           command=lambda i=i, op=op: self._organise_row_op(i, op))
            b.pack(side="left", padx=(3, 0))
            self.tip(b, tip)

        note = row.get("note") or ""
        issue = row.get("issue")
        if note or issue:
            lbl = ttk.Label(box, wraplength=900,
                            style="Accent.TLabel" if issue else "Dim.TLabel",
                            text=(("\u26a0 " + issue + "    ") if issue else "")
                            + note)
            lbl.pack(fill="x", padx=(30, 0), pady=(1, 0))

    def _organise_debounce(self):
        if self._oafter:
            self.root.after_cancel(self._oafter)
        self._oafter = self.root.after(400, self._organise_plan)

    def _organise_options(self):
        return {"copy": bool(self.var_ocopy.get()),
                "sweep": (self.var_osweep_name.get().strip() or "_Unsorted")
                if self.var_osweep.get() else None,
                "skip_identical": bool(self.var_oident.get()),
                "remove_empty": bool(self.var_oempty.get())}

    # -- the plan ----------------------------------------------------------

    def _organise_plan(self):
        self._oafter = None
        root = self.var_oroot.get().strip()
        rules = self._organise_rules()
        options = self._organise_options()
        if not root or not os.path.isdir(root):
            self.var_ostatus.set("Choose the folder to organise."
                                 if not root else "Folder not found: " + root)
            return
        if not any(l.strip() and not l.strip().startswith("#")
                   for l in rules.splitlines()) and not options["sweep"]:
            self.var_ostatus.set("Write some rules (or press Suggest rules).")
            return
        self._ogen += 1
        gen = self._ogen
        self.var_ostatus.set("Planning...")

        def work():
            conn = None
            try:
                conn = self._ro()
                plan = findex_organise.build_plan(conn, root, rules, options)
                self.msgs.put(("call", self._organise_show, (gen, plan)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_ostatus.set,
                               ("Could not plan: {}: {}".format(
                                   type(exc).__name__, exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _organise_show(self, gen, plan):
        if gen != self._ogen:
            return
        self._oplan = plan
        human = findex.human
        # plan rows
        self.otree_plan.delete(*self.otree_plan.get_children())
        self._opaths = {}
        order = {"error": 0, "collision": 1, "move": 2, "copy": 2, "sweep": 3,
                 "identical": 4, "unchanged": 5, "unmatched": 6}
        rows = sorted(plan.rows, key=lambda r: (order.get(r[4], 9), r[0]))
        shown = 0
        for path, size, mtime, target, action, rule, note in rows:
            if shown >= 5000:
                break
            tags = ("odd",) if shown % 2 else ()
            if action in ("collision", "error"):
                tags += ("bad",)
            elif action in ("unchanged", "unmatched", "identical"):
                tags += ("dim",)
            iid = self.otree_plan.insert(
                "", "end", tags=tags,
                values=(action, path, target if action not in (
                    "unmatched", "unchanged") else "", note))
            self._opaths[iid] = path
            shown += 1
        # resulting tree
        self.otree_tree.delete(*self.otree_tree.get_children())
        root_iid = self.otree_tree.insert(
            "", "end", text=plan.root, open=True, tags=("head",),
            values=("{:,}".format(findex_organise._count(plan.tree)),
                    human(findex_organise._bytes(plan.tree))))
        self._organise_fill_tree(root_iid, plan.tree, depth=0)
        # issues
        self.otree_issues.delete(*self.otree_issues.get_children())
        self._oissue_lines = {}
        for i, (level, line, msg) in enumerate(plan.issues):
            tags = ("odd",) if i % 2 else ()
            tags += ("bad",) if level == "error" else (
                ("dim",) if level == "info" else ())
            iid = self.otree_issues.insert(
                "", "end", tags=tags, values=(level, line or "", msg))
            self._oissue_lines[iid] = line
        try:
            self.rules_text.tag_remove("errline", "1.0", "end")
            for level, line, msg in plan.issues:
                if level == "error" and line:
                    self.rules_text.tag_add("errline", "{}.0".format(line),
                                            "{}.end".format(line))
        except tk.TclError:
            pass
        # the same problems against the builder's rows
        line_of = rows_lines(self._orows)
        by_line = {}
        for level, line, msg in plan.issues:
            if line and level in ("error", "warning"):
                by_line.setdefault(line, []).append(msg)
        changed = False
        for row, line in zip(self._orows, line_of):
            issue = "; ".join(by_line.get(line, [])) or None
            if row.get("raw") is None:          # plainer words for blanks
                if not (row.get("dest") or "").strip():
                    issue = "type the folder these files should go into"
                elif row.get("cond") not in ("any", "type") and \
                        not (row.get("value") or "").strip():
                    issue = "type what to look for"
            if issue != row.get("issue"):
                row["issue"] = issue
                changed = True
        if changed and not self.var_oadvanced.get():
            self._organise_render_rows()
        errs = sum(1 for i in plan.issues if i[0] == "error")
        warns = sum(1 for i in plan.issues if i[0] == "warning")
        self.onb.tab(self.otab_issues, text=" Problems{} ".format(
            " ({})".format(errs + warns) if errs + warns else ""))
        # summary
        lines = plan.summary_lines()
        lines.append("")
        lines.append("Destinations")
        for k, (n, b) in sorted(plan.by_dest.items()):
            lines.append("  {:<40} {:>7,} files  {:>9}".format(k, n, human(b)))
        if plan.empty_after:
            lines.append("")
            lines.append("Folders left empty ({:,})".format(len(plan.empty_after)))
            lines += ["  " + f for f in plan.empty_after[:100]]
        self.osummary.configure(state="normal")
        self.osummary.delete("1.0", "end")
        self.osummary.insert("1.0", "\n".join(lines))
        self.osummary.configure(state="disabled")
        verb = "copy" if plan.options.get("copy") else "move"
        n = plan.counts["move"] + plan.counts["copy"] + plan.counts["sweep"]
        b = plan.bytes["move"] + plan.bytes["copy"] + plan.bytes["sweep"]
        self.var_oapply.set("{} {:,} file{}...".format(
            verb.capitalize(), n, "" if n == 1 else "s") if n
            else "Nothing to {}".format(verb))
        self.var_ostatus.set(
            "{:,} of {:,} files to {} ({}) into {:,} folder(s)  |  {:,} in "
            "place  |  {:,} collision(s)  |  {:,} unmatched{}{}".format(
                n, plan.total, verb, human(b), len(plan.by_dest),
                plan.counts["unchanged"] + plan.counts["identical"],
                plan.counts["collision"], plan.counts["unmatched"],
                "  |  {:,} rule error(s)".format(errs) if errs else "",
                "  |  {:,} folder(s) emptied".format(len(plan.empty_after))
                if plan.empty_after else ""))

    def _organise_fill_tree(self, parent, node, depth):
        human = findex.human
        for f in node["files"][:12]:
            self.otree_tree.insert(parent, "end", text=f, tags=("dim",),
                                   values=("", ""))
        if node["n"] > 12:
            self.otree_tree.insert(parent, "end", tags=("dim",),
                                   text="... {:,} more".format(node["n"] - 12),
                                   values=("", ""))
        for name in sorted(node["folders"]):
            child = node["folders"][name]
            iid = self.otree_tree.insert(
                parent, "end", text=name + "/", open=(depth < 1),
                values=("{:,}".format(findex_organise._count(child)),
                        human(findex_organise._bytes(child))))
            self._organise_fill_tree(iid, child, depth + 1)

    def _organise_goto_issue(self):
        sel = self.otree_issues.selection()
        line = self._oissue_lines.get(sel[0]) if sel else None
        if line:
            try:
                self.rules_text.see("{}.0".format(line))
                self.rules_text.tag_remove("sel", "1.0", "end")
                self.rules_text.tag_add("sel", "{}.0".format(line),
                                        "{}.end".format(line))
                self.rules_text.mark_set("insert", "{}.0".format(line))
                self.rules_text.focus_set()
            except tk.TclError:
                pass

    # -- suggestions -------------------------------------------------------

    def organise_suggest(self):
        root = self.var_oroot.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("No folder", "Choose the folder first.")
            return
        self.var_ostatus.set("Reading the names...")

        def work():
            conn = None
            try:
                conn = self._ro()
                files, folders = findex_organise.files_under(conn, root)
                sug = findex_organise.suggest(files)
                self.msgs.put(("call", self._organise_suggested, (sug, len(files))))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_ostatus.set,
                               ("Could not suggest: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _organise_suggested(self, sug, nfiles):
        if not nfiles:
            self.var_ostatus.set("Nothing indexed under that folder - index "
                                 "it first (Index tab).")
            return
        text = findex_organise.suggestion_text(sug, header=False)
        current = self._organise_rules()
        if current.strip():
            text = current.rstrip("\n") + "\n\n# --- suggested {} ---\n".format(
                time.strftime("%H:%M")) + text
        self._organise_set_rules(text)
        self.var_ostatus.set("{:,} rule(s) suggested from {:,} files - edit, "
                             "then check the plan".format(
                                 len(sug["rules"]), nfiles))

    # -- templates ---------------------------------------------------------

    def _organise_templates(self):
        """Rebuild the 'Load saved rules' submenu from the index."""
        try:
            conn = self._ro()
            names = [r[0] for r in findex_organise.templates(conn)]
            conn.close()
        except Exception:                                      # noqa: BLE001
            names = []
        self._otemplate_names = names
        self.otemplate_menu.delete(0, "end")
        if not names:
            self.otemplate_menu.add_command(label="(nothing saved yet)",
                                            state="disabled")
        for name in names:
            self.otemplate_menu.add_command(
                label=name, command=lambda n=name: self._organise_load_template(n))

    def _organise_load_template(self, name=None):
        name = name or self.var_otemplate.get()
        if not name:
            return
        try:
            conn = self._ro()
            rows = [r for r in findex_organise.templates(conn) if r[0] == name]
            conn.close()
        except Exception:                                      # noqa: BLE001
            rows = []
        if not rows:
            return
        _, rules, options, saved = rows[0]
        try:
            o = json.loads(options or "{}")
        except ValueError:
            o = {}
        self.var_otemplate.set(name)
        self.var_ocopy.set(bool(o.get("copy")))
        self.var_osweep.set(bool(o.get("sweep")))
        if o.get("sweep"):
            self.var_osweep_name.set(o["sweep"])
        self.var_oident.set(bool(o.get("skip_identical", True)))
        self.var_oempty.set(bool(o.get("remove_empty", True)))
        self._organise_set_rules(rules)
        self.var_ostatus.set("Loaded the saved rules {!r}".format(name))

    def organise_save_template(self):
        from tkinter import simpledialog
        rules = self._organise_rules()
        if not rules.strip():
            messagebox.showinfo("Nothing to save", "Write some rules first.")
            return
        name = simpledialog.askstring(
            "Save these rules", "A name for this set of rules:",
            initialvalue=self.var_otemplate.get() or "", parent=self.root)
        if not name:
            return
        try:
            conn = findex.open_db(self.var_db.get(), timeout=5)
            findex_organise.save_template(conn, name.strip(), rules,
                                          self._organise_options())
            conn.close()
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not save", str(exc))
            return
        self.var_otemplate.set(name.strip())
        self._organise_templates()
        self.var_ostatus.set("Saved these rules as {!r} - find them under More > "
                             "Load saved rules".format(name.strip()))

    def organise_delete_template(self):
        from tkinter import simpledialog
        names = getattr(self, "_otemplate_names", None)
        if names is None:
            self._organise_templates()
            names = self._otemplate_names
        if not names:
            messagebox.showinfo("Nothing saved", "There are no saved rule sets.")
            return
        name = simpledialog.askstring(
            "Delete saved rules", "Which one? Saved: {}".format(", ".join(names)),
            initialvalue=self.var_otemplate.get() or names[0], parent=self.root)
        if not name or name not in names:
            return
        if not messagebox.askyesno("Delete saved rules",
                                   "Delete the saved rules {!r}?".format(name)):
            return
        try:
            conn = findex.open_db(self.var_db.get(), timeout=5)
            findex_organise.delete_template(conn, name)
            conn.close()
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not delete", str(exc))
            return
        self.var_otemplate.set("")
        self._organise_templates()

    # -- export / apply / undo ---------------------------------------------

    def organise_export(self):
        plan = self._oplan
        if plan is None:
            messagebox.showinfo("No plan yet", "Choose a folder and rules first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export organise plan", initialdir=findex.downloads_dir(),
            initialfile="findex-organise-{}.html".format(
                time.strftime("%Y-%m-%d")),
            defaultextension=".html",
            filetypes=[("Web page (HTML)", "*.html"), ("CSV", "*.csv"),
                       ("Text", "*.txt"), ("JSON", "*.json")])
        if not path:
            return
        try:
            findex_organise.export(plan, path)
            self.var_ostatus.set("Plan written to " + path)
            _g().reveal_path(path)
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not export", str(exc))

    def organise_apply(self):
        plan = self._oplan
        if plan is None:
            messagebox.showinfo("No plan yet", "Choose a folder and rules first.")
            return
        n = plan.counts["move"] + plan.counts["copy"] + plan.counts["sweep"]
        if not n:
            messagebox.showinfo("Nothing to do", "The plan moves no files.")
            return
        errs = [i for i in plan.issues if i[0] == "error"]
        if errs:
            if not messagebox.askyesno(
                    "Rules have errors",
                    "{:,} rule error(s) - files those rules would have placed "
                    "stay put. Apply the rest anyway?".format(len(errs))):
                self.onb.select(self.otab_issues)
                return
        verb = "Copy" if plan.options.get("copy") else "Move"
        msg = "{} {:,} file(s) ({}) into {:,} folder(s) under\n{}".format(
            verb, n, findex.human(plan.bytes["move"] + plan.bytes["copy"]
                                  + plan.bytes["sweep"]),
            len(plan.by_dest), plan.root)
        if plan.counts["collision"]:
            msg += "\n\n{:,} collision(s) are skipped.".format(
                plan.counts["collision"])
        if plan.empty_after and plan.options.get("remove_empty"):
            msg += "\n{:,} emptied folder(s) will be removed.".format(
                len(plan.empty_after))
        msg += "\n\nOne batch - Undo reverses it exactly."
        if not messagebox.askyesno("{} {:,} file(s)?".format(verb, n), msg):
            return
        db = self.var_db.get()
        self.var_ostatus.set("Applying...")

        def work():
            conn = None
            try:
                conn = findex.open_db(db, timeout=10)
                res = findex_organise.apply(
                    conn, plan, log=lambda s: self.msgs.put(("log", s)),
                    progress=lambda d, t: self.msgs.put(
                        ("status", "Organising... {:,} of {:,}".format(d, t))))
                self.msgs.put(("call", self._organise_applied, (res,)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_ostatus.set,
                               ("Apply failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _organise_applied(self, res):
        msg = "Batch {}: {:,} moved, {:,} copied, {:,} empty folder(s) removed{}"\
            .format(res["batch"], res["moved"], res["copied"], res["removed"],
                    " - {:,} failed (see the Index tab's Output)".format(
                        len(res["failed"])) if res["failed"] else "")
        self.var_ostatus.set(msg)
        self.var_status.set(msg)
        self.refresh_results()
        self.refresh_stats()
        if getattr(self, "_jloaded", False):
            self.refresh_journal()
        self._organise_plan()

    def organise_undo(self):
        db = self.var_db.get()
        try:
            conn = self._ro()
            rows = [r for r in findex_rename.batches(conn) if r[2]]
            conn.close()
        except Exception:                                      # noqa: BLE001
            rows = []
        if not rows:
            messagebox.showinfo("Nothing to undo", "No batches are recorded.")
            return
        batch, ts, n = rows[0]
        if not messagebox.askyesno(
                "Undo batch {}?".format(batch),
                "Reverse the {:,} change(s) made at {}? (Rename and Organise "
                "share one history - this is the most recent batch of "
                "either.)".format(n, time.strftime("%H:%M on %d %b",
                                                   time.localtime(ts)))):
            return

        def work():
            conn = None
            try:
                conn = findex.open_db(db, timeout=10)
                b, done, failed = findex_rename.undo(
                    conn, batch, log=lambda s: self.msgs.put(("log", s)))
                self.msgs.put(("call", self._organise_undone, (b, done, failed)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_ostatus.set,
                               ("Undo failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _organise_undone(self, batch, done, failed):
        msg = "Undid batch {}: {:,} reversed{}".format(
            batch, done, " - {:,} failed (see Output)".format(len(failed))
            if failed else "")
        self.var_ostatus.set(msg)
        self.var_status.set(msg)
        self.refresh_results()
        self.refresh_stats()
        if getattr(self, "_jloaded", False):
            self.refresh_journal()
        self._organise_plan()

    def show_organise_help(self):
        win = tk.Toplevel(self.root)
        win.title("Organise rules")
        win.geometry("760x640")
        c = self.pal
        txt = tk.Text(win, wrap="none", padx=12, pady=10, relief="flat",
                      background=c["panel"], foreground=c["text"],
                      font=(self.mono_family, self.ui_size))
        txt.insert("1.0", RULES_HELP)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
