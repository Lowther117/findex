#!/usr/bin/env python3
"""
findex_tabs - the tool tabs of the desktop app: Health, Duplicates, Rename
and Verify. A mixin that findex_gui.FindexApp inherits; each tab drives one
of the engine's tool modules (findex_report, findex_hash, findex_rename,
findex_verify) and reuses the app's own machinery - launch() for engine
child processes with progress, the message queue, tooltips, the palette.

Long jobs (hashing, snapshots, disk verification) run as engine children so
Stop always works and the window never freezes; quick ones (the health scan,
a rename plan) run in a thread on a read-only connection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import findex
import findex_hash
import findex_rename
import findex_report
import findex_verify

TOOL_KINDS = ("hash", "hash-near", "snapshot", "verify", "report")


def _g():
    """findex_gui, imported lazily: it imports this module at start-up."""
    return sys.modules["findex_gui"]


def _folder_row(parent, var, tip_fn, tip_text, label="Folder:"):
    """A 'Folder: [entry] [Browse]' strip; blank means the whole index."""
    row = ttk.Frame(parent)
    ttk.Label(row, text=label).pack(side="left")
    e = ttk.Entry(row, textvariable=var, width=46)
    e.pack(side="left", fill="x", expand=True, padx=(6, 4))
    tip_fn(e, tip_text)

    def browse():
        d = filedialog.askdirectory(title="Choose a folder",
                                    initialdir=var.get() or None)
        if d:
            var.set(os.path.normpath(d))
    ttk.Button(row, text="Browse...", width=9, command=browse).pack(side="left")
    return row


class ToolTabs:
    """Mixin for findex_gui.FindexApp. Expects the app's usual attributes
    (root, nb, tip, launch, msgs, var_db, var_status, pal, log_line...)."""

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------

    def _build_tool_tabs(self):
        self._tool_trees = []
        self._tool_after = None
        self.tab_health = ttk.Frame(self.nb)
        self.tab_dupes = ttk.Frame(self.nb)
        self.tab_rename = ttk.Frame(self.nb)
        self.tab_verify = ttk.Frame(self.nb)
        self.nb.add(self.tab_health, text="  Health  ")
        self.nb.add(self.tab_dupes, text="  Duplicates  ")
        self.nb.add(self.tab_rename, text="  Rename  ")
        self.nb.add(self.tab_verify, text="  Verify  ")
        self._build_health_tab()
        self._build_dupes_tab()
        self._build_rename_tab()
        self._build_verify_tab()

    def _tool_tree(self, parent, cols, spec, height=None):
        """A results Treeview with scrollbar, remembered for theming.
        spec: [(key, heading, width, anchor, stretch)]."""
        holder = ttk.Frame(parent)
        kw = {"height": height} if height else {}
        # "#tree" as the first name asks for the tree column (#0) as well;
        # it is not a data column, so it is left out of `columns` - Tk
        # resets headings and widths if columns are changed afterwards.
        with_tree = bool(cols) and cols[0] == "#tree"
        data_cols = tuple(c for c in cols if c != "#tree")
        tree = ttk.Treeview(holder, columns=data_cols,
                            show=("tree", "headings") if with_tree
                            else "headings",
                            selectmode="extended", **kw)
        for key, text, width, anchor, stretch in spec:
            if key == "#0":
                tree.heading("#0", text=text)
                tree.column("#0", width=width, anchor=anchor, stretch=stretch)
            else:
                tree.heading(key, text=text)
                tree.column(key, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tool_trees.append(tree)
        return holder, tree

    def _theme_tools(self, c, pal):
        """Called from apply_theme: recolour the tool trees' tags."""
        for tree in getattr(self, "_tool_trees", []):
            tree.tag_configure("odd", background=pal["row_alt"])
            tree.tag_configure("bad", foreground=c["bad"])
            tree.tag_configure("dim", foreground=c["dim"])
            tree.tag_configure("good", foreground=c["good"])
            tree.tag_configure("head", font=(self.ui_family, self.ui_size,
                                             "bold"))

    def _tool_tab_shown(self, name):
        if name == "Health" and not self._health_scanned and self.proc is None:
            self.health_scan()
        elif name == "Rename":
            self._rename_debounce()
        elif name == "Verify":
            self._verify_refresh_history()

    def _tool_progress(self, p):
        """A @P line from a tool child: 'seen', 'done', 'total', 'elapsed'."""
        total = p.get("total") or 0
        seen = p.get("seen", 0)
        if total:
            pct = min(99.0, 100.0 * seen / total)
            self._progress_set(pct)
            self.var_counts.set("{:.0f}%  |  {:,} of {:,}  |  {:,} done  |  "
                                "{:.0f}s".format(pct, seen, total,
                                                 p.get("done", seen),
                                                 p.get("elapsed", 0)))
        else:
            self.var_counts.set("{:,} so far  |  {:.0f}s".format(
                seen, p.get("elapsed", 0)))

    def _tool_finished(self, kind, code):
        """The engine child for a tool tab ended."""
        if kind in ("hash", "hash-near"):
            if code == 0:
                self._dupes_query()
            else:
                self.var_dstatus.set("Hashing stopped - showing whatever "
                                     "was hashed so far")
                self._dupes_query()
        elif kind == "snapshot":
            out = getattr(self, "_snap_out", None)
            if code == 0 and out and os.path.exists(out):
                self.var_vstatus.set("Snapshot written: " + out)
                self.var_vsnap.set(out)
                _g().reveal_path(out)
            else:
                self.var_vstatus.set("Snapshot failed (exit {}) - see the "
                                     "Index tab's Output".format(code))
            self._verify_refresh_history()
        elif kind == "verify":
            self._verify_load_result(code)
        elif kind == "report":
            self._health_scanned = False
            self.health_scan()

    # helpers shared by the tabs ------------------------------------------

    def _ro(self):
        try:
            return findex.open_db_ro(self.var_db.get())
        except sqlite3.Error:
            return findex.open_db(self.var_db.get())

    def _paths_of(self, tree, mapping):
        return [mapping[i] for i in tree.selection() if i in mapping]

    def _tree_popup(self, tree, menu, event):
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
        menu.tk_popup(event.x_root, event.y_root)

    def _make_ctx(self, tree, mapping_attr, extra=None):
        """Open / Show in folder / Copy path / Delete for a tool tree."""
        g = _g()
        m = tk.Menu(self.root, tearoff=0)
        self._menus.append(m)

        def paths():
            return self._paths_of(tree, getattr(self, mapping_attr))

        def open_():
            for p in paths()[:1]:
                if os.path.exists(p):
                    g.open_path(p)
                else:
                    g.reveal_path(os.path.dirname(p))

        def reveal():
            for p in paths()[:1]:
                g.reveal_path(p if os.path.exists(p) else os.path.dirname(p))

        def copy():
            ps = paths()
            if ps:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(ps))
                self.var_status.set("Path copied" if len(ps) == 1
                                    else "{:,} paths copied".format(len(ps)))

        m.add_command(label="Open", command=open_)
        m.add_command(label="Show in folder", command=reveal)
        m.add_command(label="Copy path", command=copy)
        if extra:
            m.add_separator()
            for label, fn in extra:
                m.add_command(label=label, command=fn)
        m.add_separator()
        m.add_command(label="Delete...",
                      command=lambda: self._tool_delete(tree, mapping_attr))
        tree.bind("<Button-3>", lambda e: self._tree_popup(tree, m, e))
        tree.bind("<Button-2>", lambda e: self._tree_popup(tree, m, e))
        if sys.platform == "darwin":
            tree.bind("<Control-Button-1>",
                      lambda e: self._tree_popup(tree, m, e))
        tree.bind("<Double-1>", lambda e: open_())
        tree.bind("<Return>", lambda e: open_())
        tree.bind("<Delete>", lambda e: (self._tool_delete(tree, mapping_attr),
                                         "break")[1])
        tree.bind("<BackSpace>", lambda e: (self._tool_delete(tree, mapping_attr),
                                            "break")[1])
        tree.bind("<Control-a>", lambda e: (tree.selection_set(
            tree.get_children()), "break")[1])
        tree.bind("<Command-a>", lambda e: (tree.selection_set(
            tree.get_children()), "break")[1])
        return m

    def _tool_delete(self, tree, mapping_attr):
        """Recycle Bin / Trash, never permanent - same as the Search tab."""
        g = _g()
        paths = self._paths_of(tree, getattr(self, mapping_attr))
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        listed = "\n".join("    " + os.path.basename(p) or p
                           for p in paths[:8])
        if len(paths) > 8:
            listed += "\n    ...and {:,} more".format(len(paths) - 8)
        bin_name = "Recycle Bin" if os.name == "nt" else "Bin"
        if not messagebox.askyesno(
                "Delete {:,} item(s)?".format(len(paths)),
                "Move to the {} (recoverable from there):\n\n{}".format(
                    bin_name, listed)):
            return
        done, failed = g._trash_many(paths)
        self._db_forget([p for p in paths if p not in failed])
        for p in failed[:5]:
            self.log_line("could not delete: " + p)
        self.var_status.set("Sent {:,} item(s) to the {}{}".format(
            done, bin_name, " - {:,} failed (see Output)".format(len(failed))
            if failed else ""))
        gone = set(paths) - set(failed)
        mapping = getattr(self, mapping_attr)
        for iid in list(tree.get_children("")):
            self._prune_tree(tree, iid, mapping, gone)
        self.run_search(live=False)

    def _prune_tree(self, tree, iid, mapping, gone):
        for child in list(tree.get_children(iid)):
            self._prune_tree(tree, child, mapping, gone)
        if mapping.get(iid) in gone:
            tree.delete(iid)
            mapping.pop(iid, None)

    # ==================================================================
    # Health
    # ==================================================================

    def _build_health_tab(self):
        t = self.tab_health
        self._health = None
        self._health_scanned = False
        self._hpaths = {}
        self.var_hunder = tk.StringVar(value="")
        self.var_hstatus = tk.StringVar(value="Not scanned yet")
        self.var_hstale = tk.DoubleVar(value=3.0)

        top = ttk.Frame(t)
        top.pack(fill="x", padx=12, pady=(10, 4))
        row = _folder_row(top, self.var_hunder, self.tip,
                          "Limit the report to one folder or drive. Blank "
                          "= everything in the index.", label="Scope:")
        row.pack(side="left", fill="x", expand=True)
        ttk.Label(top, text="  Stale after").pack(side="left")
        spin = _g().Spinbox(top, from_=0.5, to=30, increment=0.5, width=5,
                            textvariable=self.var_hstale)
        spin.pack(side="left", padx=(4, 2))
        self._track_spin(spin)
        ttk.Label(top, text="years").pack(side="left")
        self.tip(spin, "A file counts as stale when it has not been "
                       "modified for this long.")
        b = ttk.Button(top, text="Scan", width=8, style="Accent.TButton",
                       command=self.health_scan)
        b.pack(side="left", padx=(12, 0))
        self.tip(b, "Read the index (nothing on disk is opened) and work "
                    "out every category on the left. Seconds, even for "
                    "hundreds of thousands of files.")
        b = ttk.Button(top, text="Fingerprint types", width=16,
                       command=self.health_fingerprint)
        b.pack(side="left", padx=(6, 0))
        self.tip(b, "Read the first 16 KB of every file to learn what it "
                    "really is, so 'Type does not match name' and "
                    "'Unreadable' can be filled in. Runs in the background "
                    "with progress; Stop on the Index tab cancels it. Only "
                    "files not yet fingerprinted are read.")
        b = ttk.Button(top, text="Export report...", width=15,
                       command=self.health_export)
        b.pack(side="left", padx=(6, 0))
        self.tip(b, "Save the whole report: .html is a self-contained page "
                    "with the summary, charts of age and type, and every "
                    "category; .csv is one finding per row; .txt and .json "
                    "too.")

        hint = ttk.Label(t, style="Dim.TLabel", text=(
            "What is wrong with the tree, from the index: empty folders, "
            "zero-byte and temp files, names Windows refuses, case clashes, "
            "paths past the 260-character limit, stale and huge files, files "
            "that could not be read, names that lie about their type, and "
            "passwords or keys sitting in documents."))
        hint.pack(fill="x", padx=12, pady=(0, 6))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        left, self.htree_cats = self._tool_tree(
            body, ("category", "count", "size"),
            [("category", "Category", 230, "w", True),
             ("count", "Count", 80, "e", False),
             ("size", "Size", 80, "e", False)])
        left.pack(side="left", fill="y", padx=(0, 8))
        self.htree_cats.configure(selectmode="browse")
        self.htree_cats.bind("<<TreeviewSelect>>",
                             lambda e: self._health_show_category())
        right, self.htree = self._tool_tree(
            body, ("name", "size", "modified", "folder", "detail"),
            [("name", "Name", 240, "w", False), ("size", "Size", 80, "e", False),
             ("modified", "Modified", 120, "w", False),
             ("folder", "Folder", 300, "w", True),
             ("detail", "Detail", 260, "w", False)])
        right.pack(side="left", fill="both", expand=True)
        self._make_ctx(self.htree, "_hpaths")
        self.tip(self.htree, "The files in the selected category. Works "
                             "like the Search list: select several, right-"
                             "click for Open / Show in folder / Delete "
                             "(Recycle Bin). Double-click opens.", popup=False)
        ttk.Label(t, textvariable=self.var_hstatus,
                  style="Dim.TLabel").pack(fill="x", padx=12, pady=(0, 8))

    def health_scan(self):
        if getattr(self, "_health_busy", False):
            return
        self._health_busy = True
        self.var_hstatus.set("Scanning the index...")
        db = self.var_db.get()
        under = self.var_hunder.get().strip() or None
        try:
            stale = float(self.var_hstale.get())
        except (tk.TclError, ValueError):
            stale = 3.0

        def work():
            conn = None
            try:
                conn = self._ro()
                h = findex_report.scan(
                    conn, under=under, stale_years=stale,
                    progress=lambda d, t: self.msgs.put(
                        ("status", "Scanning the index... {:,} of {:,}"
                         .format(d, t))))
                self.msgs.put(("call", self._health_done, (h, None)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self._health_done, (None, str(exc))))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _health_done(self, h, err):
        self._health_busy = False
        if err:
            self.var_hstatus.set("Scan failed: " + err)
            return
        self._health = h
        self._health_scanned = True
        keep = self.htree_cats.selection()
        self.htree_cats.delete(*self.htree_cats.get_children())
        for i, (key, label) in enumerate(findex_report.CATEGORIES):
            n = h.counts[key]
            size = h.bytes[key]
            tags = ("odd",) if i % 2 else ()
            if not n:
                tags += ("dim",)
            elif key not in ("largest", "deepest", "stale"):
                tags += ("bad",)
            self.htree_cats.insert(
                "", "end", iid=key, tags=tags,
                values=(label, "{:,}".format(n),
                        findex.human(size) if size and key in (
                            "stale", "zero-byte", "leftovers", "largest",
                            "mismatch", "unreadable") else ""))
        o = h.overview
        g, f, w = o["dupes_name_size"]
        line = ("{:,} files in {:,} folders, {}  |  {:,} duplicate sets by "
                "name+size ({} reclaimable)".format(
                    o["files"], o["folders"], findex.human(o["bytes"]), g,
                    findex.human(w)))
        if o["dupes_exact"] and o["dupes_exact"][0]:
            g, f, w = o["dupes_exact"]
            line += "  |  {:,} sets of identical files ({})".format(
                g, findex.human(w))
        if h.notes:
            line += "\n" + h.notes[0]
        self.var_hstatus.set(line)
        if keep and keep[0] in h.rows:
            self.htree_cats.selection_set(keep[0])
        else:
            self.htree_cats.selection_set(findex_report.CATEGORIES[0][0])
        self._health_show_category()

    def _health_show_category(self):
        sel = self.htree_cats.selection()
        self.htree.delete(*self.htree.get_children())
        self._hpaths = {}
        if not sel or self._health is None:
            return
        key = sel[0]
        rows = self._health.rows.get(key, [])
        human = findex.human
        fmt_time = _g().fmt_time
        for i, (path, size, mtime, is_dir, detail) in enumerate(rows[:5000]):
            iid = self.htree.insert(
                "", "end", tags=("odd",) if i % 2 else (),
                values=(os.path.basename(path) or path,
                        "folder" if is_dir else human(size or 0),
                        fmt_time(mtime) if mtime else "",
                        os.path.dirname(path), detail or ""))
            self._hpaths[iid] = path
        n = self._health.counts.get(key, 0)
        label = findex_report.LABEL[key]
        self.var_status.set("{}: {:,}{}".format(
            label, n, " (showing the first {:,})".format(min(len(rows), 5000))
            if n > 5000 else ""))

    def health_fingerprint(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "hash",
                                       "--all", "--progress"]
        under = self.var_hunder.get().strip()
        if under:
            cmd += ["--under", under]
        self._progress_est = 0
        self.launch(cmd, "report", "Fingerprinting file types...")

    def health_export(self):
        if self._health is None:
            messagebox.showinfo("Scan first", "Run Scan, then export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export health report",
            initialdir=findex.downloads_dir(),
            initialfile="findex-health-{}.html".format(
                time.strftime("%Y-%m-%d")),
            defaultextension=".html",
            filetypes=[("Web page (HTML)", "*.html"), ("Findings (CSV)", "*.csv"),
                       ("Text", "*.txt"), ("JSON", "*.json")])
        if not path:
            return
        h = self._health

        def work():
            try:
                findex_report.export(h, path)
                self.msgs.put(("status", "Report written to " + path))
                self.msgs.put(("call", _g().reveal_path, (path,)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("status", "Export failed: {}".format(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ==================================================================
    # Duplicates
    # ==================================================================

    DUPE_MODES = (("name", "Same name and size  (instant)"),
                  ("exact", "Identical contents  (hashes size-matches first)"),
                  ("near", "Near-identical text  (fingerprints documents)"))

    def _build_dupes_tab(self):
        t = self.tab_dupes
        self._dpaths = {}
        self._dgroups = []
        self.var_dmode = tk.StringVar(value="name")
        self.var_dunder = tk.StringVar(value="")
        self.var_dexts = tk.StringVar(value="")
        self.var_dnohash = tk.BooleanVar(value=False)
        self.var_dstatus = tk.StringVar(value="")

        modes = ttk.LabelFrame(t, text="What counts as a duplicate")
        modes.pack(fill="x", padx=12, pady=(10, 4))
        inner = ttk.Frame(modes)
        inner.pack(fill="x", padx=8, pady=6)
        tips = {"name": "The classic quick check: files sharing a name AND a "
                        "size. Instant, no files are read - but two "
                        "different files can share both, and a renamed copy "
                        "is missed.",
                "exact": "Proof: byte-for-byte identical files, whatever "
                         "they are called or where they live. Only files "
                         "whose size matches another file's are read at all, "
                         "and only fingerprint matches are hashed in full. "
                         "Re-runs read only what is new.",
                "near": "Documents whose extracted TEXT is nearly the same - "
                        "the draft and the final, the same report saved twice "
                        "under different names. Works on text findex already "
                        "extracted, so no files are opened; the first run "
                        "fingerprints every document once."}
        for key, label in self.DUPE_MODES:
            rb = ttk.Radiobutton(inner, text=label, value=key,
                                 variable=self.var_dmode)
            rb.pack(side="left", padx=(0, 18))
            self.tip(rb, tips[key])

        top = ttk.Frame(t)
        top.pack(fill="x", padx=12, pady=(4, 4))
        row = _folder_row(top, self.var_dunder, self.tip,
                          "Only look beneath this folder or drive. Blank = "
                          "the whole index.", label="Within:")
        row.pack(side="left", fill="x", expand=True)
        ttk.Label(top, text="  Type:").pack(side="left")
        self.dtype_box = ttk.Combobox(top, textvariable=self.var_dexts,
                                      width=16, height=28,
                                      values=["All types"],
                                      postcommand=self._refresh_dtypes)
        self.dtype_box.pack(side="left", padx=(4, 0))
        self.tip(self.dtype_box, "Restrict to a type or group - pdf, "
                                 "images, documents... Same list as the "
                                 "Search tab.")
        chk = ttk.Checkbutton(top, text="Use stored hashes only",
                              variable=self.var_dnohash)
        chk.pack(side="left", padx=(12, 0))
        self.tip(chk, "Skip reading files: list only what earlier hashing "
                      "already proved. Instant, but new files are not "
                      "considered.")
        b = ttk.Button(top, text="Find duplicates", width=15,
                       style="Accent.TButton", command=self.dupes_run)
        b.pack(side="left", padx=(12, 0))
        self.tip(b, "Run the chosen check. Identical / near-identical modes "
                    "first read or fingerprint what they need, with "
                    "progress in the status bar; Stop on the Index tab "
                    "cancels.")

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        holder, self.dtree = self._tool_tree(
            body, ("#tree", "size", "modified", "folder"),
            [("#0", "Set / file", 360, "w", True),
             ("size", "Size", 90, "e", False),
             ("modified", "Modified", 130, "w", False),
             ("folder", "Folder", 420, "w", True)])
        holder.pack(fill="both", expand=True)
        self._make_ctx(self.dtree, "_dpaths", extra=[
            ("Select all but the newest in every set", lambda: self._dupes_select("newest")),
            ("Select all but the oldest in every set", lambda: self._dupes_select("oldest")),
            ("Select all but the first in every set", lambda: self._dupes_select("first")),
        ])
        self.tip(self.dtree, "Each set is one row you can expand; the copies "
                             "sit underneath. Right-click: open, show, copy "
                             "path, keep-one selections, Delete to the "
                             "Recycle Bin.", popup=False)

        foot = ttk.Frame(t)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_dstatus,
                  style="Dim.TLabel").pack(side="left", fill="x", expand=True)
        b = ttk.Button(foot, text="Keep newest, select the rest",
                       command=lambda: self._dupes_select("newest"))
        b.pack(side="right")
        self.tip(b, "In every set, select every copy except the most "
                    "recently modified one - then press Delete (or right-"
                    "click > Delete) to send the selection to the Recycle "
                    "Bin. Nothing is deleted until you do.")
        b = ttk.Button(foot, text="Expand all",
                       command=lambda: self._dupes_expand(True))
        b.pack(side="right", padx=(0, 6))
        b = ttk.Button(foot, text="Collapse all",
                       command=lambda: self._dupes_expand(False))
        b.pack(side="right", padx=(0, 6))

    def _refresh_dtypes(self):
        self._refresh_types()
        self.dtype_box["values"] = self.type_box["values"]

    def _dupes_filter(self):
        g = _g()
        exts = g.parse_exts(self.var_dexts.get())
        if exts:
            expanded = []
            for e in exts:
                if e in g.TYPE_GROUPS:
                    expanded += [x.lstrip(".") for x in g.TYPE_GROUPS[e]]
                elif e not in ("folder", "folders", "dir"):
                    expanded.append(e)
            exts = expanded or None
        return exts, (self.var_dunder.get().strip() or None)

    def dupes_run(self):
        mode = self.var_dmode.get()
        if mode == "name" or self.var_dnohash.get():
            self._dupes_query()
            return
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        exts, under = self._dupes_filter()
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "hash",
                                       "--progress"]
        if mode == "near":
            cmd.append("--near-only")
        if under:
            cmd += ["--under", under]
        if exts:
            cmd += ["-e"] + exts
        self._progress_est = 0
        self.var_dstatus.set("Reading files..." if mode == "exact"
                             else "Fingerprinting documents...")
        self.launch(cmd, "hash" if mode == "exact" else "hash-near",
                    "Hashing for duplicates..." if mode == "exact"
                    else "Fingerprinting documents...")

    def _dupes_query(self):
        mode = self.var_dmode.get()
        exts, under = self._dupes_filter()
        db = self.var_db.get()
        self.var_dstatus.set("Looking for duplicates...")

        def work():
            conn = None
            try:
                conn = self._ro()
                groups = []
                if mode == "name":
                    rows = findex.dupe_rows(conn, 0, exts)
                    if under:
                        u = os.path.normcase(under.rstrip("\\/")) + os.sep
                        rows = [r for r in rows
                                if os.path.normcase(r[0]).startswith(u)]
                    cur, last = [], None
                    for path, size, mtime, n in rows:
                        key = (os.path.basename(path), size)
                        if key != last and cur:
                            groups.append(cur)
                            cur = []
                        last = key
                        cur.append((path, size, mtime))
                    if cur:
                        groups.append(cur)
                    groups = [g for g in groups if len(g) > 1]
                    summary = findex.dupe_summary(conn, exts)
                elif mode == "exact":
                    rows = findex_hash.exact_dupe_rows(conn, 0, exts, under)
                    cur, last = [], None
                    for path, size, mtime, n, fh in rows:
                        if fh != last and cur:
                            groups.append(cur)
                            cur = []
                        last = fh
                        cur.append((path, size, mtime))
                    if cur:
                        groups.append(cur)
                    summary = findex_hash.exact_dupe_summary(conn, exts, under)
                else:
                    near = findex_hash.near_dupe_groups(conn, exts, under)
                    groups = [[(p, s, m) for p, s, m, c, fh in g]
                              for g in near]
                    files = sum(len(g) for g in groups)
                    summary = (len(groups), files, 0)
                self.msgs.put(("call", self._dupes_show, (groups, summary, mode)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_dstatus.set,
                               ("Failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _dupes_show(self, groups, summary, mode):
        self.dtree.delete(*self.dtree.get_children())
        self._dpaths = {}
        self._dgroups = groups
        human = findex.human
        fmt_time = _g().fmt_time
        shown = 0
        for gi, g in enumerate(groups[:3000]):
            size = g[0][1] or 0
            if mode == "near":
                head = "{} similar documents".format(len(g))
            elif mode == "exact":
                head = "{} identical copies".format(len(g))
            else:
                head = "{} x {}".format(len(g), os.path.basename(g[0][0]))
            parent = self.dtree.insert(
                "", "end", text=head, open=(gi < 40), tags=("head",),
                values=(human(size) if mode != "near" else "",
                        "", "{} reclaimable".format(human(size * (len(g) - 1)))
                        if mode != "near" else ""))
            for i, (path, s, m) in enumerate(g):
                iid = self.dtree.insert(
                    parent, "end", text=os.path.basename(path),
                    tags=("odd",) if i % 2 else (),
                    values=(human(s or 0), fmt_time(m) if m else "",
                            os.path.dirname(path)))
                self._dpaths[iid] = path
                shown += 1
        n_groups, n_files, wasted = summary
        if mode == "near":
            msg = ("{:,} group(s) of near-identical documents, {:,} files"
                   .format(n_groups, n_files) if n_groups else
                   "No near-identical documents found")
        elif n_groups:
            msg = ("{:,} set(s), {:,} files - {} to be had back if each set "
                   "kept one copy".format(n_groups, n_files, human(wasted)))
        else:
            msg = ("No duplicates found" +
                   (" (matched by name + size)" if mode == "name" else
                    " among hashed files"))
        if len(groups) > 3000:
            msg += "  (showing the first 3,000 sets)"
        self.var_dstatus.set(msg)
        self.var_status.set(msg)

    def _dupes_expand(self, open_):
        for iid in self.dtree.get_children(""):
            self.dtree.item(iid, open=open_)

    def _dupes_select(self, keep):
        """Select every copy but one per set: the newest, oldest or first."""
        sel = []
        for parent in self.dtree.get_children(""):
            kids = list(self.dtree.get_children(parent))
            if len(kids) < 2:
                continue
            if keep == "first":
                spare = kids[1:]
            else:
                def mt(iid):
                    p = self._dpaths.get(iid)
                    try:
                        return os.path.getmtime(findex.lp(p))
                    except OSError:
                        return 0
                ranked = sorted(kids, key=mt, reverse=(keep == "newest"))
                spare = ranked[1:]
            sel += spare
            self.dtree.item(parent, open=True)
        self.dtree.selection_set(sel)
        if sel:
            self.dtree.see(sel[0])
        self.var_status.set("{:,} file(s) selected - Delete sends them to "
                            "the {}".format(len(sel), "Recycle Bin"
                                            if os.name == "nt" else "Bin"))

    # ==================================================================
    # Rename
    # ==================================================================

    def _build_rename_tab(self):
        t = self.tab_rename
        self._rplan = []
        self._rpaths = {}
        self._rename_source = None       # rows handed over from Search
        self._rafter = None
        self.var_rquery = tk.StringVar(value="")
        self.var_rexts = tk.StringVar(value="")
        self.var_rfind = tk.StringVar(value="")
        self.var_rrepl = tk.StringVar(value="")
        self.var_rregex = tk.BooleanVar(value=False)
        self.var_ricase = tk.BooleanVar(value=False)
        self.var_rcase = tk.StringVar(value="keep")
        self.var_rnorm = tk.BooleanVar(value=False)
        self.var_rdate = tk.BooleanVar(value=False)
        self.var_rdatefmt = tk.StringVar(value="%Y-%m-%d")
        self.var_rmaxlen = tk.IntVar(value=0)
        self.var_rextlower = tk.BooleanVar(value=False)
        self.var_rfolders = tk.BooleanVar(value=False)
        self.var_rstatus = tk.StringVar(value="")
        self.var_rsource = tk.StringVar(value="")

        sel = ttk.LabelFrame(t, text="Which files")
        sel.pack(fill="x", padx=12, pady=(10, 4))
        row = ttk.Frame(sel)
        row.pack(fill="x", padx=8, pady=6)
        e = ttk.Entry(row, textvariable=self.var_rquery, font=self.search_font)
        e.pack(side="left", fill="x", expand=True)
        self.tip(e, "A findex search, exactly as on the Search tab: "
                    "D:\\Photos ext:jpg, content:invoice, !draft... Every "
                    "result is a candidate. Or right-click files on the "
                    "Search tab > Rename these... to bring a hand-picked "
                    "selection here.", popup=False)
        ttk.Label(row, text="  Type:").pack(side="left")
        self.rtype_box = ttk.Combobox(row, textvariable=self.var_rexts,
                                      width=16, height=28,
                                      values=["All types"],
                                      postcommand=self._refresh_rtypes)
        self.rtype_box.pack(side="left", padx=(4, 0))
        lbl = ttk.Label(sel, textvariable=self.var_rsource, style="Accent.TLabel")
        lbl.pack(fill="x", padx=8, pady=(0, 4))

        ops = ttk.LabelFrame(t, text="What to change (applied in this order)")
        ops.pack(fill="x", padx=12, pady=(4, 4))
        r1 = ttk.Frame(ops)
        r1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(r1, text="Find:").pack(side="left")
        e = ttk.Entry(r1, textvariable=self.var_rfind, width=26)
        e.pack(side="left", padx=(4, 10))
        self.tip(e, "Text to replace in the name (the extension is left "
                    "alone). Tick Regex for a regular expression - groups "
                    "come back as \\1, \\2 in Replace.")
        ttk.Label(r1, text="Replace with:").pack(side="left")
        e = ttk.Entry(r1, textvariable=self.var_rrepl, width=26)
        e.pack(side="left", padx=(4, 10))
        chk = ttk.Checkbutton(r1, text="Regex", variable=self.var_rregex)
        chk.pack(side="left")
        chk = ttk.Checkbutton(r1, text="Ignore case", variable=self.var_ricase)
        chk.pack(side="left", padx=(8, 0))
        ttk.Label(r1, text="   Case:").pack(side="left")
        cb = ttk.Combobox(r1, textvariable=self.var_rcase, width=9,
                          state="readonly",
                          values=("keep", "lower", "upper", "title", "sentence"))
        cb.pack(side="left", padx=(4, 0))
        self.tip(cb, "Change the letter case of the name (not the extension).")

        r2 = ttk.Frame(ops)
        r2.pack(fill="x", padx=8, pady=(2, 6))
        chk = ttk.Checkbutton(r2, text="Normalise (safe everywhere)",
                              variable=self.var_rnorm)
        chk.pack(side="left")
        self.tip(chk, "Make the name safe on Windows, macOS, OneDrive and "
                      "SharePoint alike: NFC Unicode, < > : \" | ? * -> _, "
                      "runs of spaces to one, no leading/trailing spaces or "
                      "dots, reserved names like CON prefixed with _.")
        chk = ttk.Checkbutton(r2, text="Date prefix", variable=self.var_rdate)
        chk.pack(side="left", padx=(16, 2))
        e = ttk.Entry(r2, textvariable=self.var_rdatefmt, width=10)
        e.pack(side="left")
        for w in (chk, e):
            self.tip(w, "Put the file's modified date in front of the name: "
                        "2024-03-01 report.pdf. The format is strftime "
                        "(%Y-%m-%d, %Y%m%d, %d %b %Y...). Files already "
                        "starting with that date are left alone.")
        ttk.Label(r2, text="   Max length:").pack(side="left")
        spin = _g().Spinbox(r2, from_=0, to=255, width=5,
                            textvariable=self.var_rmaxlen)
        spin.pack(side="left", padx=(4, 0))
        self._track_spin(spin)
        self.tip(spin, "Shorten names longer than this many characters, "
                       "keeping the extension. 0 = off.")
        chk = ttk.Checkbutton(r2, text=".EXT -> .ext", variable=self.var_rextlower)
        chk.pack(side="left", padx=(16, 0))
        chk = ttk.Checkbutton(r2, text="Include folders", variable=self.var_rfolders)
        chk.pack(side="left", padx=(16, 0))
        self.tip(chk, "Also rename folders in the selection. Everything "
                      "indexed beneath a renamed folder is re-pointed.")

        for var in (self.var_rquery, self.var_rexts, self.var_rfind,
                    self.var_rrepl, self.var_rregex, self.var_ricase,
                    self.var_rcase, self.var_rnorm, self.var_rdate,
                    self.var_rdatefmt, self.var_rmaxlen, self.var_rextlower,
                    self.var_rfolders):
            var.trace_add("write", lambda *_: self._rename_debounce())
        self.var_rquery.trace_add("write", lambda *_: self._rename_use_query())

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        holder, self.rtree = self._tool_tree(
            body, ("current", "new", "status", "folder"),
            [("current", "Current name", 280, "w", False),
             ("new", "New name", 280, "w", False),
             ("status", "Status", 200, "w", False),
             ("folder", "Folder", 320, "w", True)])
        holder.pack(fill="both", expand=True)
        self._make_ctx(self.rtree, "_rpaths")
        self.tip(self.rtree, "The plan: what each file would be called. "
                             "Nothing is renamed until you press Apply. "
                             "Collisions (two files landing on one name, or "
                             "a name already taken) are skipped, never "
                             "overwritten.", popup=False)

        foot = ttk.Frame(t)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_rstatus,
                  style="Dim.TLabel").pack(side="left", fill="x", expand=True)
        b = ttk.Button(foot, text="Apply renames...", width=16,
                       style="Accent.TButton", command=self.rename_apply)
        b.pack(side="right")
        self.tip(b, "Rename every 'ok' row on disk and in the index, as one "
                    "batch you can undo. Asks first.")
        b = ttk.Button(foot, text="Undo last batch...", width=16,
                       command=self.rename_undo)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Reverse the most recent batch of renames, in the "
                    "opposite order. Files that have since moved on are "
                    "reported, not guessed at.")
        b = ttk.Button(foot, text="Refresh preview", width=15,
                       command=self._rename_plan)
        b.pack(side="right", padx=(0, 6))

    def _refresh_rtypes(self):
        self._refresh_types()
        self.rtype_box["values"] = self.type_box["values"]

    def rename_from_search(self):
        """Search tab > right-click > Rename these...: bring the selected
        rows here as the candidates."""
        rows = self.selected_rows()
        if not rows:
            return
        self._rename_source = [(r["path"], r["size"], r["mtime"],
                                bool(r.get("is_dir"))) for r in rows]
        self.var_rsource.set("Selection from the Search tab: {:,} item(s). "
                             "Type a search above to use that instead."
                             .format(len(rows)))
        self.nb.select(self.tab_rename)
        self._rename_plan()

    def _rename_use_query(self):
        if self._rename_source is not None:
            self._rename_source = None
            self.var_rsource.set("")

    def _rename_debounce(self):
        if self._rafter:
            self.root.after_cancel(self._rafter)
        self._rafter = self.root.after(350, self._rename_plan)

    def _rename_ops(self):
        try:
            max_len = int(self.var_rmaxlen.get())
        except (tk.TclError, ValueError):
            max_len = 0
        case = self.var_rcase.get()
        return {"find": self.var_rfind.get(), "replace": self.var_rrepl.get(),
                "regex": bool(self.var_rregex.get()),
                "ignore_case": bool(self.var_ricase.get()),
                "case": None if case == "keep" else case,
                "normalise": bool(self.var_rnorm.get()),
                "date_prefix": (self.var_rdatefmt.get() or True)
                if self.var_rdate.get() else None,
                "max_len": max_len or None,
                "ext_lower": bool(self.var_rextlower.get()),
                "include_folders": bool(self.var_rfolders.get())}

    def _rename_plan(self):
        self._rafter = None
        ops = self._rename_ops()
        query = self.var_rquery.get().strip()
        source = self._rename_source
        g = _g()
        exts = g.parse_exts(self.var_rexts.get())
        kind = None
        if exts:
            expanded = []
            for e in exts:
                if e in ("folder", "folders", "dir"):
                    kind = "folder"
                elif e in g.TYPE_GROUPS:
                    expanded += [x.lstrip(".") for x in g.TYPE_GROUPS[e]]
                else:
                    expanded.append(e)
            exts = expanded or None
        if source is None and not query and not exts:
            self.rtree.delete(*self.rtree.get_children())
            self._rpaths = {}
            self._rplan = []
            self.var_rstatus.set("Type a search above (or send files here "
                                 "from the Search tab) to see the plan.")
            return
        self._rgen = getattr(self, "_rgen", 0) + 1
        gen = self._rgen

        def work():
            conn = None
            try:
                conn = self._ro()
                rows = (source if source is not None else
                        findex_rename.select_rows(conn, query, exts=exts,
                                                  kind=kind))
                if len(rows) > 20000:
                    rows = rows[:20000]
                    note = " (first 20,000 of the selection)"
                else:
                    note = ""
                planned = findex_rename.plan(rows, ops, conn)
                self.msgs.put(("call", self._rename_show, (gen, planned, note)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_rstatus.set,
                               ("Could not build the plan: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _rename_show(self, gen, planned, note):
        if gen != getattr(self, "_rgen", 0):
            return
        self._rplan = planned
        self.rtree.delete(*self.rtree.get_children())
        self._rpaths = {}
        counts = {}
        shown = 0
        for old, new, status, why in planned:
            counts[status] = counts.get(status, 0) + 1
            if shown >= 5000:
                continue
            tags = ("odd",) if shown % 2 else ()
            if status in ("collision", "invalid"):
                tags += ("bad",)
            elif status in ("unchanged", "skipped"):
                tags += ("dim",)
            iid = self.rtree.insert(
                "", "end", tags=tags,
                values=(os.path.basename(old), os.path.basename(new)
                        if status == "ok" else "",
                        {"ok": "will rename", "unchanged": "no change"}.get(
                            status, status + (": " + why if why else "")),
                        os.path.dirname(old)))
            self._rpaths[iid] = old
            shown += 1
        self.var_rstatus.set(
            "{:,} selected{}: {:,} to rename, {:,} unchanged, {:,} collision(s), "
            "{:,} invalid, {:,} folder(s) skipped".format(
                len(planned), note, counts.get("ok", 0),
                counts.get("unchanged", 0), counts.get("collision", 0),
                counts.get("invalid", 0), counts.get("skipped", 0)))

    def rename_apply(self):
        todo = [p for p in self._rplan if p[2] == "ok"]
        if not todo:
            messagebox.showinfo("Nothing to rename",
                                "The plan has no files to rename.")
            return
        sample = "\n".join("    {}  ->  {}".format(
            os.path.basename(o), os.path.basename(n)) for o, n, s, w in todo[:6])
        if len(todo) > 6:
            sample += "\n    ...and {:,} more".format(len(todo) - 6)
        if not messagebox.askyesno(
                "Rename {:,} item(s)?".format(len(todo)),
                "{}\n\nFiles are renamed on disk and in the index, as one "
                "batch you can undo from this tab.".format(sample)):
            return
        planned = list(self._rplan)
        db = self.var_db.get()
        self.var_rstatus.set("Renaming...")

        def work():
            conn = None
            try:
                conn = findex.open_db(db, timeout=10)
                batch, done, failed = findex_rename.apply(
                    conn, planned, log=lambda s: self.msgs.put(("log", s)))
                self.msgs.put(("call", self._rename_applied,
                               (batch, done, failed)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_rstatus.set,
                               ("Rename failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _rename_applied(self, batch, done, failed):
        msg = "Renamed {:,} item(s) in batch {}{}".format(
            done, batch, " - {:,} failed (see the Index tab's Output)".format(
                len(failed)) if failed else "")
        self.var_rstatus.set(msg)
        self.var_status.set(msg)
        self._rename_source = None
        self.var_rsource.set("")
        self.run_search(live=False)
        if getattr(self, "_jloaded", False):
            self.refresh_journal()
        self._rename_plan()

    def rename_undo(self):
        db = self.var_db.get()
        try:
            conn = self._ro()
            rows = findex_rename.batches(conn) if hasattr(
                findex_rename, "batches") else []
            conn.close()
        except Exception:                                      # noqa: BLE001
            rows = []
        rows = [r for r in rows if r[2]]
        if not rows:
            messagebox.showinfo("Nothing to undo", "No renames are recorded.")
            return
        batch, ts, n = rows[0]
        if not messagebox.askyesno(
                "Undo batch {}?".format(batch),
                "Reverse the {:,} rename(s) made at {}?".format(
                    n, time.strftime("%H:%M on %d %b", time.localtime(ts)))):
            return

        def work():
            conn = None
            try:
                conn = findex.open_db(db, timeout=10)
                b, done, failed = findex_rename.undo(
                    conn, batch, log=lambda s: self.msgs.put(("log", s)))
                self.msgs.put(("call", self._rename_applied,
                               (b, done, failed)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_rstatus.set,
                               ("Undo failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    # ==================================================================
    # Verify
    # ==================================================================

    def _build_verify_tab(self):
        t = self.tab_verify
        self._vpaths = {}
        self._vresult = None
        self.var_vunder = tk.StringVar(value="")
        self.var_vhash = tk.BooleanVar(value=False)
        self.var_vsnap = tk.StringVar(value="")
        self.var_vmode = tk.StringVar(value="index")
        self.var_vfolder = tk.StringVar(value="")
        self.var_vdisk = tk.BooleanVar(value=True)
        self.var_vhash2 = tk.BooleanVar(value=False)
        self.var_vagainst = tk.StringVar(value="")
        self.var_vstatus = tk.StringVar(value="")
        self.var_vhistory = tk.StringVar(value="")

        snap = ttk.LabelFrame(t, text="1. Take a snapshot - a manifest of a "
                                      "tree as it is now")
        snap.pack(fill="x", padx=12, pady=(10, 4))
        row = ttk.Frame(snap)
        row.pack(fill="x", padx=8, pady=6)
        fr = _folder_row(row, self.var_vunder, self.tip,
                         "The folder or drive to snapshot. Paths are stored "
                         "relative to it, so a copy made elsewhere can be "
                         "verified against it. Blank = the whole index, with "
                         "absolute paths.")
        fr.pack(side="left", fill="x", expand=True)
        chk = ttk.Checkbutton(row, text="Hash every file first",
                              variable=self.var_vhash)
        chk.pack(side="left", padx=(12, 0))
        self.tip(chk, "Read every file so the snapshot records a content "
                      "hash for each. Slow on a big tree, but it lets verify "
                      "prove bytes rather than just sizes and dates, and "
                      "recognise moved files.")
        b = ttk.Button(row, text="Save snapshot...", width=16,
                       style="Accent.TButton", command=self.verify_snapshot)
        b.pack(side="left", padx=(12, 0))

        ver = ttk.LabelFrame(t, text="2. Verify - compare a snapshot with...")
        ver.pack(fill="x", padx=12, pady=(4, 4))
        row = ttk.Frame(ver)
        row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row, text="Snapshot:").pack(side="left")
        e = ttk.Entry(row, textvariable=self.var_vsnap)
        e.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.tip(e, "The .fxsnap file to start from.")

        def pick_snap(var):
            p = filedialog.askopenfilename(
                title="Choose a snapshot", initialdir=findex.downloads_dir(),
                filetypes=[("findex snapshot", "*.fxsnap *.tsv *.txt"),
                           ("All files", "*.*")])
            if p:
                var.set(p)
        ttk.Button(row, text="Browse...", width=9,
                   command=lambda: pick_snap(self.var_vsnap)).pack(side="left")

        r2 = ttk.Frame(ver)
        r2.pack(fill="x", padx=8, pady=2)
        rb = ttk.Radiobutton(r2, text="the same place, as the index has it now",
                             value="index", variable=self.var_vmode)
        rb.pack(side="left")
        self.tip(rb, "What changed since the snapshot: added, missing, "
                     "changed, moved. Uses the index, so run an index first "
                     "for a current answer.")
        chk = ttk.Checkbutton(r2, text="hash first", variable=self.var_vhash2)
        chk.pack(side="left", padx=(8, 0))
        self.tip(chk, "For a hashed snapshot: hash the current files too, so "
                      "content is compared rather than size and date.")

        r3 = ttk.Frame(ver)
        r3.pack(fill="x", padx=8, pady=2)
        rb = ttk.Radiobutton(r3, text="a copy at:", value="folder",
                             variable=self.var_vmode)
        rb.pack(side="left")
        self.tip(rb, "Did the copy come out right? Compare the snapshot with "
                     "a copy of the tree somewhere else - after a server "
                     "move, a SharePoint migration, a backup to USB.")
        e = ttk.Entry(r3, textvariable=self.var_vfolder, width=44)
        e.pack(side="left", fill="x", expand=True, padx=(6, 4))

        def pick_folder():
            d = filedialog.askdirectory(title="The copy to check")
            if d:
                self.var_vfolder.set(os.path.normpath(d))
                self.var_vmode.set("folder")
        ttk.Button(r3, text="Browse...", width=9,
                   command=pick_folder).pack(side="left")
        chk = ttk.Checkbutton(r3, text="read the disk directly (not the index)",
                              variable=self.var_vdisk)
        chk.pack(side="left", padx=(8, 0))
        self.tip(chk, "Walk and hash the copy right now, so it need not be "
                      "indexed - the usual choice for a drive or share. "
                      "Untick to compare against the index's rows for it.")

        r4 = ttk.Frame(ver)
        r4.pack(fill="x", padx=8, pady=(2, 6))
        rb = ttk.Radiobutton(r4, text="another snapshot:", value="snap",
                             variable=self.var_vmode)
        rb.pack(side="left")
        e = ttk.Entry(r4, textvariable=self.var_vagainst)
        e.pack(side="left", fill="x", expand=True, padx=(6, 4))
        ttk.Button(r4, text="Browse...", width=9,
                   command=lambda: (pick_snap(self.var_vagainst),
                                    self.var_vmode.set("snap"))).pack(side="left")
        b = ttk.Button(r4, text="Verify", width=10, style="Accent.TButton",
                       command=self.verify_run)
        b.pack(side="left", padx=(12, 0))

        body = ttk.Frame(t)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 6))
        holder, self.vtree = self._tool_tree(
            body, ("kind", "path", "newpath", "before", "after", "detail"),
            [("kind", "Result", 90, "w", False),
             ("path", "Path", 330, "w", True),
             ("newpath", "Now at", 260, "w", False),
             ("before", "Before", 80, "e", False),
             ("after", "After", 80, "e", False),
             ("detail", "Detail", 220, "w", False)])
        holder.pack(fill="both", expand=True)
        self._make_ctx(self.vtree, "_vpaths")

        foot = ttk.Frame(t)
        foot.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(foot, textvariable=self.var_vstatus,
                  style="Dim.TLabel").pack(side="left", fill="x", expand=True)
        b = ttk.Button(foot, text="Save result...", width=14,
                       command=self.verify_export)
        b.pack(side="right")
        self.tip(b, "Write the comparison as a web page, CSV, text or JSON.")
        ttk.Label(t, textvariable=self.var_vhistory,
                  style="Dim.TLabel").pack(fill="x", padx=12, pady=(0, 8))

    def _verify_refresh_history(self):
        """Recent snapshots in Downloads, for a reminder of what exists."""
        try:
            d = findex.downloads_dir()
            snaps = sorted((f for f in os.listdir(d) if f.endswith(".fxsnap")),
                           key=lambda f: os.path.getmtime(os.path.join(d, f)),
                           reverse=True)[:4]
        except OSError:
            snaps = []
        self.var_vhistory.set(
            "Recent snapshots in {}: {}".format(d, ", ".join(snaps))
            if snaps else "")

    def verify_snapshot(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        under = self.var_vunder.get().strip()
        base = os.path.basename(under.rstrip("\\/")) if under else "index"
        base = "".join(c for c in base if c.isalnum() or c in "-_ ") or "index"
        path = filedialog.asksaveasfilename(
            title="Save snapshot", initialdir=findex.downloads_dir(),
            initialfile="{}-{}.fxsnap".format(base, time.strftime("%Y-%m-%d")),
            defaultextension=".fxsnap",
            filetypes=[("findex snapshot", "*.fxsnap"),
                       ("Uncompressed (TSV)", "*.tsv")])
        if not path:
            return
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "snapshot",
                                       "-o", path, "--progress"]
        if under:
            cmd += ["--under", under]
        if self.var_vhash.get():
            cmd.append("--hash")
        self._snap_out = path
        self._progress_est = 0
        self.launch(cmd, "snapshot", "Writing the snapshot...")

    def verify_run(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        snap = self.var_vsnap.get().strip()
        if not snap or not os.path.exists(snap):
            messagebox.showwarning("No snapshot", "Choose a snapshot file first.")
            return
        mode = self.var_vmode.get()
        out = os.path.join(findex.HERE, ".findex-verify.json")
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "verify",
                                       snap, "-o", out, "-f", "json", "--progress"]
        if mode == "folder":
            folder = self.var_vfolder.get().strip()
            if not folder or not os.path.isdir(folder):
                messagebox.showwarning("No folder", "Choose the copy's folder.")
                return
            cmd += ["--folder", folder]
            if self.var_vdisk.get():
                cmd.append("--disk")
        elif mode == "snap":
            other = self.var_vagainst.get().strip()
            if not other or not os.path.exists(other):
                messagebox.showwarning("No snapshot",
                                       "Choose the second snapshot.")
                return
            cmd += ["--against", other]
        elif self.var_vhash2.get():
            cmd.append("--hash")
        self._verify_out = out
        self._progress_est = 0
        self.var_vstatus.set("Comparing...")
        self.launch(cmd, "verify", "Verifying...")

    def _verify_load_result(self, code):
        out = getattr(self, "_verify_out", None)
        try:
            with open(out, encoding="utf-8") as fh:
                res = json.load(fh)
        except Exception:                                      # noqa: BLE001
            self.var_vstatus.set("Verification did not produce a result "
                                 "(exit {}) - see the Index tab's Output"
                                 .format(code))
            return
        try:
            os.remove(out)
        except OSError:
            pass
        self._vresult = res
        self.vtree.delete(*self.vtree.get_children())
        self._vpaths = {}
        human = findex.human
        base = None
        if self.var_vmode.get() == "folder":
            base = self.var_vfolder.get().strip()
        elif self.var_vmode.get() == "index":
            try:
                head, _ = findex_verify.read_snapshot(self.var_vsnap.get())
                base = head.get("under")
            except Exception:                                  # noqa: BLE001
                base = None
        n = 0
        for key, label in findex_verify.SECTIONS:
            for row in res.get(key, []):
                if n >= 5000:
                    break
                tags = ("odd",) if n % 2 else ()
                if key in ("changed", "missing"):
                    tags += ("bad",)
                elif key == "touched":
                    tags += ("dim",)
                elif key == "added":
                    tags += ("good",)
                if key == "changed":
                    vals = (label, row[0], "", human(row[1]), human(row[2]), row[3])
                    where = row[0]
                elif key in ("moved", "likely"):
                    vals = (label.split(" (")[0], row[0], row[1],
                            human(row[2]), human(row[2]), "")
                    where = row[1]
                elif key in ("missing", "added"):
                    vals = (label, row[0], "", human(row[1]) if key == "missing"
                            else "", human(row[1]) if key == "added" else "", "")
                    where = row[0]
                else:
                    vals = (label, row[0], "", "", "", "")
                    where = row[0]
                iid = self.vtree.insert("", "end", tags=tags, values=vals)
                self._vpaths[iid] = (os.path.join(base, where.replace("/", os.sep))
                                     if base and not os.path.isabs(where)
                                     else where)
                n += 1
        d = res.get("differences", 0)
        self.var_vstatus.set(
            ("IDENTICAL - {:,} files unchanged".format(res.get("same", 0)))
            if not d else
            "{:,} difference(s): {}   |   {:,} unchanged".format(
                d, ", ".join("{:,} {}".format(len(res.get(k, [])), k.replace(
                    "_", " ")) for k, _ in findex_verify.SECTIONS
                    if res.get(k)), res.get("same", 0)))
        self.var_status.set(self.var_vstatus.get())

    def verify_export(self):
        res = self._vresult
        if not res:
            messagebox.showinfo("Nothing to save", "Run a verification first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save verification result",
            initialdir=findex.downloads_dir(),
            initialfile="findex-verify-{}.html".format(time.strftime("%Y-%m-%d")),
            defaultextension=".html",
            filetypes=[("Web page (HTML)", "*.html"), ("CSV", "*.csv"),
                       ("Text", "*.txt"), ("JSON", "*.json")])
        if not path:
            return
        try:
            findex_verify.export(res, path, res.get("from", "snapshot"),
                                 res.get("to", "now"))
            self.var_vstatus.set("Saved " + path)
            _g().reveal_path(path)
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not save", str(exc))
