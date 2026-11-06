#!/usr/bin/env python3
"""
findex_tabs - the tool tab of the desktop app: Duplicates. A mixin that
findex_gui.FindexApp inherits; the tab drives the engine's findex_hash
module and reuses the app's own machinery - launch() for engine child
processes with progress, the message queue, tooltips, the palette.

Long jobs (hashing) run as engine children so Stop always works and the
window never freezes; the instant checks run in a thread on a read-only
connection.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import findex
import findex_hash

TOOL_KINDS = ("hash", "hash-near", "hash-images")


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
        self.tab_dupes = ttk.Frame(self.nb)
        self.nb.add(self.tab_dupes, text="  Duplicates  ")
        self._build_dupes_tab()

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
        if kind in ("hash", "hash-near", "hash-images"):
            if code == 0:
                self._dupes_query()
            else:
                self.var_dstatus.set("Hashing stopped - showing whatever "
                                     "was hashed so far")
                self._dupes_query()

    # helpers shared by the tabs ------------------------------------------

    def _ro(self, db=None):
        if db is None:
            db = self.var_db.get()
        try:
            return findex.open_db_ro(db)
        except sqlite3.Error:
            return findex.open_db(db)

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
        failed_set = set(failed)
        self._db_forget([p for p in paths if p not in failed_set])
        for p in failed[:5]:
            self.log_line("could not delete: " + p)
        self.var_status.set("Sent {:,} item(s) to the {}{}".format(
            done, bin_name, " - {:,} failed (see Output)".format(len(failed))
            if failed else ""))
        gone = set(paths) - set(failed)
        mapping = getattr(self, mapping_attr)
        for iid in list(tree.get_children("")):
            self._prune_tree(tree, iid, mapping, gone)
        self.refresh_results()

    def _prune_tree(self, tree, iid, mapping, gone):
        for child in list(tree.get_children(iid)):
            self._prune_tree(tree, child, mapping, gone)
        if mapping.get(iid) in gone:
            tree.delete(iid)
            mapping.pop(iid, None)

    # ==================================================================
    # Duplicates
    # ==================================================================

    DUPE_MODES = (("name", "Same name and size  (instant)"),
                  ("exact", "Identical contents  (hashes size-matches first)"),
                  ("near", "Near-identical text  (fingerprints documents)"),
                  ("images", "Similar images  (fingerprints pictures)"))

    def _build_dupes_tab(self):
        t = self.tab_dupes
        self._dpaths = {}
        self._dgroups = []
        self.var_dmode = tk.StringVar(value="name")
        self.var_dunder = tk.StringVar(value="")
        self.var_dexts = tk.StringVar(value="")
        self.var_dnohash = tk.BooleanVar(value=False)
        self.var_ddist = tk.IntVar(value=findex_hash.IMAGE_DISTANCE)
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
                        "fingerprints every document once.",
                "images": "Pictures that LOOK the same, whatever the file "
                          "says: the same photo as PNG and JPEG, the original "
                          "and the copy shrunk for email, a re-saved "
                          "screenshot. Each image is decoded once (small) "
                          "and given a 64-bit picture fingerprint; two "
                          "fingerprints within the distance set on the right "
                          "count as a match. Re-runs read only new images."}
        for key, label in self.DUPE_MODES:
            rb = ttk.Radiobutton(inner, text=label, value=key,
                                 variable=self.var_dmode)
            rb.pack(side="left", padx=(0, 18))
            self.tip(rb, tips[key])
        Spinbox = _g().Spinbox
        dist = ttk.Frame(inner)
        dist.pack(side="right")
        lbl = ttk.Label(dist, text="Image distance:")
        lbl.pack(side="left")
        spin = Spinbox(dist, from_=0, to=32, width=4,
                       textvariable=self.var_ddist)
        spin.pack(side="left", padx=(4, 0))
        self._track_spin(spin)
        for w in (lbl, spin):
            self.tip(w, "For Similar images: how many of the 64 fingerprint "
                        "bits may differ and still count as the same "
                        "picture. 0 = only near-pixel-identical images; 10 "
                        "(the default) catches resizes, re-saves and light "
                        "edits; above 16 starts pairing pictures that merely "
                        "look alike.")

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
        elif mode == "images":
            cmd.append("--images-only")
        if under:
            cmd += ["--under", under]
        if exts:
            cmd += ["-e"] + exts
        self._progress_est = 0
        doing = {"exact": ("hash", "Reading files...",
                           "Hashing for duplicates..."),
                 "near": ("hash-near", "Fingerprinting documents...",
                          "Fingerprinting documents..."),
                 "images": ("hash-images", "Fingerprinting images...",
                            "Fingerprinting images...")}[mode]
        self.var_dstatus.set(doing[1])
        self.launch(cmd, doing[0], doing[2])

    def _dupes_query(self):
        mode = self.var_dmode.get()
        exts, under = self._dupes_filter()
        db = self.var_db.get()
        distance = max(0, _g().int_of(self.var_ddist,
                                      findex_hash.IMAGE_DISTANCE))
        self.var_dstatus.set("Looking for duplicates...")

        def work():
            conn = None
            try:
                conn = self._ro(db)
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
                elif mode == "images":
                    alike = findex_hash.similar_image_groups(
                        conn, exts, under, distance)
                    groups = [[(p, s, m) for p, s, m, dh in g] for g in alike]
                    files = sum(len(g) for g in groups)
                    summary = (len(groups), files, 0)
                else:
                    near = findex_hash.near_dupe_groups(conn, exts, under)
                    groups = [[(p, s, m) for p, s, m, c, fh in g]
                              for g in near]
                    files = sum(len(g) for g in groups)
                    summary = (len(groups), files, 0)
                self.msgs.put(("call", self._dupes_show,
                               (groups, summary, mode, distance)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("call", self.var_dstatus.set,
                               ("Failed: {}".format(exc),)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _dupes_show(self, groups, summary, mode, distance=None):
        self.dtree.delete(*self.dtree.get_children())
        self._dpaths = {}
        self._dgroups = groups
        human = findex.human
        fmt_time = _g().fmt_time
        shown = 0
        for gi, g in enumerate(groups[:3000]):
            size = g[0][1] or 0
            fuzzy = mode in ("near", "images")
            if mode == "near":
                head = "{} similar documents".format(len(g))
            elif mode == "images":
                head = "{} similar images".format(len(g))
            elif mode == "exact":
                head = "{} identical copies".format(len(g))
            else:
                head = "{} x {}".format(len(g), os.path.basename(g[0][0]))
            parent = self.dtree.insert(
                "", "end", text=head, open=(gi < 40), tags=("head",),
                values=(human(size) if not fuzzy else "",
                        "", "{} reclaimable".format(human(size * (len(g) - 1)))
                        if not fuzzy else ""))
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
        elif mode == "images":
            msg = ("{:,} group(s) of look-alike images, {:,} files (within "
                   "{} bits)".format(n_groups, n_files, distance) if n_groups
                   else "No look-alike images found (within {} bits)".format(
                       distance))
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
