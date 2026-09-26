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

import findex
import findex_organise
import findex_rename

RULES_HELP = """\
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
        self.var_ostatus = tk.StringVar(value="Choose a folder and write or "
                                              "suggest some rules.")

        top = ttk.Frame(t)
        top.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(top, text="Folder:").pack(side="left")
        e = ttk.Entry(top, textvariable=self.var_oroot)
        e.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.tip(e, "The folder to tidy. Every file beneath it - subfolders "
                    "included - is considered and re-sorted against the "
                    "rules into subfolders of this folder.", popup=False)

        def browse():
            d = filedialog.askdirectory(title="Folder to organise",
                                        initialdir=self.var_oroot.get() or None)
            if d:
                self.var_oroot.set(os.path.normpath(d))
        ttk.Button(top, text="Browse...", width=9, command=browse).pack(side="left")
        ttk.Label(top, text="   ").pack(side="left")
        rb = ttk.Radiobutton(top, text="Move", value=False, variable=self.var_ocopy)
        rb.pack(side="left")
        self.tip(rb, "Move files into place. Instant on the same drive, and "
                     "the folders they leave empty can be removed.")
        rb = ttk.Radiobutton(top, text="Copy", value=True, variable=self.var_ocopy)
        rb.pack(side="left", padx=(6, 0))
        self.tip(rb, "Copy files into place and leave the originals where "
                     "they are. Undo removes the copies (if unchanged).")
        b = ttk.Button(top, text="Suggest rules", width=13,
                       command=self.organise_suggest)
        b.pack(side="left", padx=(16, 0))
        self.tip(b, "Read the names under the folder and draft a rule set: "
                    "recurring leading words (Invoice, Board minutes...), "
                    "reference codes (ACM-0042), date-named files, and type "
                    "groups for the rest - each with a count. Appended below "
                    "any rules already written; edit freely.")
        b = ttk.Button(top, text="Check rules", width=11,
                       command=lambda: (self._organise_plan(),
                                        self.onb.select(self.otab_issues)))
        b.pack(side="left", padx=(6, 0))
        self.tip(b, "Re-run the plan and open the Issues list: bad patterns, "
                    "unknown tokens, rules that can never match, rules an "
                    "earlier rule shadows, collisions.")

        opts = ttk.Frame(t)
        opts.pack(fill="x", padx=12, pady=(0, 4))
        chk = ttk.Checkbutton(opts, text="Sweep unmatched files into",
                              variable=self.var_osweep)
        chk.pack(side="left")
        e = ttk.Entry(opts, textvariable=self.var_osweep_name, width=12)
        e.pack(side="left", padx=(4, 12))
        for w in (chk, e):
            self.tip(w, "Files no rule matches normally stay where they are. "
                        "Tick this and they are gathered into this folder "
                        "instead - the same as a final '* -> _Unsorted' rule.")
        chk = ttk.Checkbutton(opts, text="Identical file already there = done",
                              variable=self.var_oident)
        chk.pack(side="left", padx=(0, 12))
        self.tip(chk, "When a byte-identical copy is already at the target "
                      "(by content hash - see the Duplicates tab), treat the "
                      "file as placed and skip it rather than calling it a "
                      "collision.")
        chk = ttk.Checkbutton(opts, text="Remove folders left empty",
                              variable=self.var_oempty)
        chk.pack(side="left", padx=(0, 12))
        self.tip(chk, "After moving, remove the folders the moves emptied. "
                      "Only folders something moved out of; folders that "
                      "were already empty are the Health tab's business.")
        ttk.Label(opts, text="Template:").pack(side="left")
        self.otemplate_box = ttk.Combobox(opts, textvariable=self.var_otemplate,
                                          width=16, state="readonly",
                                          values=[],
                                          postcommand=self._organise_templates)
        self.otemplate_box.pack(side="left", padx=(4, 4))
        self.otemplate_box.bind("<<ComboboxSelected>>",
                                lambda e: self._organise_load_template())
        self.tip(self.otemplate_box, "Saved rule sets (with their options), "
                                     "kept in the index. Pick one to load it.")
        b = ttk.Button(opts, text="Save as...", width=9,
                       command=self.organise_save_template)
        b.pack(side="left")
        b = ttk.Button(opts, text="Delete", width=7,
                       command=self.organise_delete_template)
        b.pack(side="left", padx=(4, 0))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=False, padx=(0, 8))
        hdr = ttk.Label(left, style="Dim.TLabel",
                        text="Rules - one per line, first match wins.  "
                             "Help > Organise rules for the syntax.")
        hdr.pack(fill="x", pady=(0, 4))
        holder = ttk.Frame(left)
        holder.pack(fill="both", expand=True)
        self.rules_text = tk.Text(holder, width=54, wrap="none", relief="flat",
                                  padx=8, pady=6, undo=True)
        vsb = ttk.Scrollbar(holder, orient="vertical",
                            command=self.rules_text.yview)
        self.rules_text.configure(yscrollcommand=vsb.set)
        self.rules_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.rules_text.bind("<<Modified>>", self._organise_text_changed)
        self.tip(self.rules_text, "Type rules here. The plan on the right "
                                  "updates as you type.", popup=False)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self.onb = ttk.Notebook(right)
        self.onb.pack(fill="both", expand=True)
        self.otab_plan = ttk.Frame(self.onb)
        self.otab_tree = ttk.Frame(self.onb)
        self.otab_issues = ttk.Frame(self.onb)
        self.otab_summary = ttk.Frame(self.onb)
        self.onb.add(self.otab_plan, text=" Plan ")
        self.onb.add(self.otab_tree, text=" Resulting tree ")
        self.onb.add(self.otab_issues, text=" Issues ")
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
             ("line", "Rule line", 80, "e", False),
             ("message", "What", 600, "w", True)])
        h.pack(fill="both", expand=True)
        self.otree_issues.bind("<Double-1>", lambda e: self._organise_goto_issue())
        self.tip(self.otree_issues, "Problems with the rules. Double-click "
                                    "one to jump to its line.", popup=False)

        self.osummary = tk.Text(self.otab_summary, wrap="word", relief="flat",
                                padx=10, pady=8, state="disabled")
        self.osummary.pack(fill="both", expand=True)

        foot = ttk.Frame(t)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_ostatus,
                  style="Dim.TLabel").pack(side="left", fill="x", expand=True)
        b = ttk.Button(foot, text="Apply...", width=10, style="Accent.TButton",
                       command=self.organise_apply)
        b.pack(side="right")
        self.tip(b, "Carry the plan out as one batch. Asks first, and refuses "
                    "while the rules have errors unless you say so.")
        b = ttk.Button(foot, text="Undo last batch...", width=16,
                       command=self.organise_undo)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Reverse the most recent batch - moves, copies, folders "
                    "created and folders removed - in the opposite order.")
        b = ttk.Button(foot, text="Export plan...", width=13,
                       command=self.organise_export)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Save the summary, rule check, resulting tree and every "
                    "planned move as a web page, CSV, text or JSON.")

        for var in (self.var_oroot, self.var_ocopy, self.var_osweep,
                    self.var_osweep_name, self.var_oident, self.var_oempty):
            var.trace_add("write", lambda *_: self._organise_debounce())

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
        try:
            return self.rules_text.get("1.0", "end").rstrip("\n")
        except tk.TclError:
            return ""

    def _organise_set_rules(self, text):
        self.rules_text.configure(state="normal")
        self.rules_text.delete("1.0", "end")
        self.rules_text.insert("1.0", text)
        try:
            self.rules_text.edit_modified(False)
        except tk.TclError:
            pass
        self._organise_debounce()

    def _organise_text_changed(self, _event=None):
        try:
            if not self.rules_text.edit_modified():
                return
            self.rules_text.edit_modified(False)
        except tk.TclError:
            pass
        self._organise_debounce()

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
        errs = sum(1 for i in plan.issues if i[0] == "error")
        warns = sum(1 for i in plan.issues if i[0] == "warning")
        self.onb.tab(self.otab_issues, text=" Issues{} ".format(
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
        try:
            conn = self._ro()
            names = [r[0] for r in findex_organise.templates(conn)]
            conn.close()
        except Exception:                                      # noqa: BLE001
            names = []
        self.otemplate_box["values"] = names

    def _organise_load_template(self):
        name = self.var_otemplate.get()
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
        self.var_ocopy.set(bool(o.get("copy")))
        self.var_osweep.set(bool(o.get("sweep")))
        if o.get("sweep"):
            self.var_osweep_name.set(o["sweep"])
        self.var_oident.set(bool(o.get("skip_identical", True)))
        self.var_oempty.set(bool(o.get("remove_empty", True)))
        self._organise_set_rules(rules)
        self.var_ostatus.set("Loaded template {!r}".format(name))

    def organise_save_template(self):
        from tkinter import simpledialog
        rules = self._organise_rules()
        if not rules.strip():
            messagebox.showinfo("Nothing to save", "Write some rules first.")
            return
        name = simpledialog.askstring(
            "Save template", "Name for this rule set:",
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
        self.var_ostatus.set("Saved template {!r}".format(name.strip()))

    def organise_delete_template(self):
        name = self.var_otemplate.get()
        if not name:
            return
        if not messagebox.askyesno("Delete template",
                                   "Delete the template {!r}?".format(name)):
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
        self.run_search(live=False)
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
        self.run_search(live=False)
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
