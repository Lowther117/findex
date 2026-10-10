#!/usr/bin/env python3
"""
findex_tabs_summary - the Summary tab of the desktop app: what a folder
holds, as labelled sections. A mixin that findex_tabs.ToolTabs inherits; it
drives findex_summary and reuses the app's own machinery (launch() for
engine children with progress and Stop, the message queue, tooltips).

Kept deliberately plain: one row of controls, two lists and a card.

    Folder [.....................] [Browse] [Go to v] [Summarise] [AI v] [More v]
    +- Sections ----------+  +- Files in the section ---------------------+
    |                     |  |                                            |
    +---------------------+  +--------------------------------------------+
                             +- the selected section's / file's card -----+

Everything is about ONE folder - the one in the Folder box. Get there by
typing or browsing, from "Go to" (the folders inside this one), or from the
Search tab (right-click > Summarise this folder). Everything occasional -
the local-AI actions and model choice, export, how many sections - lives in
the two menus.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import findex
import findex_summary as fs

SUMMARY_KINDS = ("summary", "summary-ai", "summary-setup")
DETAIL_CHOICES = (("Fewer, broader sections", "low"),
                  ("A balanced number of sections", "normal"),
                  ("More, narrower sections", "high"))
OVERVIEW = "overview"
MAX_ROWS = 3000              # files listed for one section
MENU_FOLDERS = 30            # sub-folders offered in the Go to menu
AI_CONFIRM_ABOVE = 25        # files: ask before a long AI job


def _g():
    """findex_gui, imported lazily: it imports this module at start-up."""
    return sys.modules["findex_gui"]


class SummaryTab:

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def _build_summary_tab(self):
        t = self.tab_summary
        cfg = getattr(self, "cfg", {})
        self._srun = None             # the run on show (dict) or None
        self._sexact = False          # ...and whether it is this folder's own
        self._ssections = {}          # tree iid -> section dict
        self._spaths = {}             # file tree iid -> path
        self._sfiles = {}             # file tree iid -> file dict
        self._ssubs = []              # [(sub-folder path, files, has own run)]
        self._sgen = 0                # discards results of an older load
        self._sfgen = 0
        self._sai = {"ok": False, "models": [], "error": "", "at": 0.0}
        self._sautorun = False
        self._snote = ""              # said once, ahead of the next status
        self._swant_model = ""        # model being downloaded
        self._sused = set()           # models this session has run
        self._sdigest = None          # this folder's "selected files" summary
        self._sshow_digest = False    # show it as soon as it is written
        self._scard = None            # (parts, terms) of the card on show
        self.var_sfolder = tk.StringVar(value="")
        self.var_sstatus = tk.StringVar(
            value="Choose a folder, then press Summarise.")
        self.var_smodel = tk.StringVar(value=cfg.get("ai_model", ""))
        detail = cfg.get("summary_detail", "normal")
        self.var_sdetail = tk.StringVar(
            value=detail if detail in dict((k, 1) for _, k in DETAIL_CHOICES)
            else "normal")

        # -- the one row of controls ---------------------------------------
        top = ttk.Frame(t)
        top.pack(side="top", fill="x", padx=14, pady=(14, 10))
        ttk.Label(top, text="Folder").pack(side="left")
        e = ttk.Entry(top, textvariable=self.var_sfolder)
        e.pack(side="left", fill="x", expand=True, padx=(8, 6))
        e.bind("<Return>", lambda _e: self.summary_load())
        self.tip(e, "The folder this tab is about - the sections, the "
                    "Summarise button, the AI summaries and the export all "
                    "cover this folder and what is below it, nothing else. "
                    "Blank = the whole index. Press Enter after typing.")
        b = ttk.Button(top, text="Browse...", command=self._summary_browse)
        b.pack(side="left")
        self.tip(b, "Pick the folder to look at.")

        self.sgo = ttk.Menubutton(top, text="Go to")
        self.sgo.pack(side="left", padx=(6, 0))
        self.sgo_menu = tk.Menu(self.sgo, tearoff=0,
                                postcommand=self._summary_go_menu)
        self._menus.append(self.sgo_menu)
        self.sgo["menu"] = self.sgo_menu
        self.tip(self.sgo, "The folders inside this one, biggest first - "
                           "pick one to look at just that folder - and Up "
                           "to go back out.")

        self.btn_srun = ttk.Button(top, text="Summarise",
                                   style="Accent.TButton",
                                   command=self.summary_run)
        self.btn_srun.pack(side="left", padx=(14, 0))
        self.tip(self.btn_srun,
                 "Sort this folder's files into sections by what they are "
                 "about, and give each readable file a card (kind of "
                 "document, title, key phrases, dates and amounts). Uses "
                 "the text findex already holds - nothing on disk is "
                 "opened and no AI is involved. A second run only reads "
                 "what changed.")

        self.sai = ttk.Menubutton(top, text="AI summaries")
        self.sai.pack(side="left", padx=(6, 0))
        self.sai_menu = tk.Menu(self.sai, tearoff=0,
                                postcommand=self._summary_ai_menu)
        self._menus.append(self.sai_menu)
        self.sai["menu"] = self.sai_menu
        self.smodel_menu = tk.Menu(self.sai_menu, tearoff=0)
        self._menus.append(self.smodel_menu)
        self.tip(self.sai, "Optional: have a small language model running "
                           "on this computer write proper summaries - of "
                           "the files you select (each one, then all of "
                           "them together), or of the whole folder section "
                           "by section. Nothing is sent anywhere. "
                           "The model is chosen (and downloaded) here too.")

        self.smore = ttk.Menubutton(top, text="More")
        self.smore.pack(side="left", padx=(6, 0))
        m = tk.Menu(self.smore, tearoff=0)
        self._menus.append(m)
        self.smore["menu"] = m
        m.add_command(label="Show this section in Search",
                      command=self.summary_to_search)
        m.add_command(label="Export this summary...",
                      command=self.summary_export)
        m.add_separator()
        for label, key in DETAIL_CHOICES:
            m.add_radiobutton(label=label, value=key,
                              variable=self.var_sdetail,
                              command=self._summary_save_prefs)
        m.add_separator()
        m.add_command(label="Forget this folder's summary",
                      command=self.summary_forget)
        self.tip(self.smore, "Open the selected section in the Search tab, "
                             "export the summary (web page, CSV, text, "
                             "JSON), and choose how finely the next "
                             "Summarise divides the folder.")

        # -- status line: packed before the body so it can never be the
        # thing that falls off the bottom of a short window. While
        # something runs it also carries a progress bar and Stop. ---------
        foot = ttk.Frame(t)
        foot.pack(side="bottom", fill="x", padx=14, pady=(8, 10))
        self.sfoot = foot
        self.btn_sstop = ttk.Button(foot, text="Stop", width=7,
                                    command=self.stop_index)
        self.tip(self.btn_sstop, "Stop what is running. Everything done so "
                                 "far is kept, so starting again carries "
                                 "on from there.")
        self.sbar = ttk.Progressbar(foot, mode="indeterminate", length=220)
        ttk.Label(foot, textvariable=self.var_sstatus, style="Dim.TLabel"
                  ).pack(side="left", fill="x", expand=True)

        # -- body: three panes with draggable dividers - sections | files
        # over the card - so whichever matters most can be given the room.
        # The positions are remembered. -------------------------------------
        body = ttk.PanedWindow(t, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, padx=14)
        self.spane_h = body
        left, self.stree_secs = self._tool_tree(
            body, ("section", "count"),
            [("section", "Section", 250, "w", True),
             ("count", "Files", 70, "e", False)], height=6)
        body.add(left, weight=1)
        self.stree_secs.configure(selectmode="browse")
        self.stree_secs.bind("<<TreeviewSelect>>",
                             lambda _e: self._summary_show_section())
        self.stree_secs.bind("<Double-1>",
                             lambda _e: self.summary_to_search())
        self.tip(self.stree_secs,
                 "The folder's files, grouped: sections by subject first, "
                 "then documents that fitted nowhere, then files with no "
                 "text by type. Click one to list its files; double-click "
                 "opens it in Search.", popup=False)

        right = ttk.PanedWindow(body, orient="vertical")
        body.add(right, weight=3)
        self.spane_v = right
        holder, self.stree_files = self._tool_tree(
            right, ("name", "kind", "about"),
            [("name", "File", 260, "w", False),
             ("kind", "Kind", 130, "w", False),
             ("about", "About", 260, "w", True)], height=6)
        right.add(holder, weight=3)
        card = ttk.Frame(right)
        right.add(card, weight=1)
        self.sdetail = tk.Text(card, height=8, wrap="word", relief="flat",
                               padx=14, pady=10)
        vsb = ttk.Scrollbar(card, orient="vertical",
                            command=self.sdetail.yview)
        self.sdetail.configure(yscrollcommand=vsb.set, state="disabled")
        vsb.pack(side="right", fill="y")
        self.sdetail.pack(side="left", fill="both", expand=True)
        for pane in (body, right):
            self.tip(pane, "Drag the divider between two panes to give one "
                           "of them more room. Column edges in the lists "
                           "drag too.", popup=False)
        self._ssash = list(cfg.get("summary_sash") or [])
        self._ssash_done = False
        self._make_ctx(self.stree_files, "_spaths", extra=[
            ("Summarise with AI", self.summary_ai_files),
            ("Look at this file's folder", self._summary_file_folder)])
        self.stree_files.bind("<<TreeviewSelect>>",
                              lambda _e: self._summary_show_file(), add="+")
        self.tip(self.stree_files,
                 "The files in the selected section, newest first. Click "
                 "one for its card below; select several and right-click "
                 "for Open / Show in folder / Summarise with AI.",
                 popup=False)

    def _theme_summary(self, c, pal):
        w = getattr(self, "sdetail", None)
        if w is None:
            return
        size = self.ui_size
        w.configure(background=c["panel"], foreground=c["text"],
                    insertbackground=c["accent"], selectbackground=c["sel"],
                    selectforeground=c["text"],
                    font=(self.ui_family, size), relief="flat",
                    spacing1=2, spacing3=4,       # air between the lines
                    highlightthickness=1, highlightbackground=c["border"],
                    highlightcolor=c["accent"])
        w.tag_configure("h", font=(self.ui_family, size + 2, "bold"),
                        spacing3=8)
        w.tag_configure("dim", foreground=c["dim"])
        w.tag_configure("key", foreground=c["dim"],
                        font=(self.ui_family, size, "bold"))

    # ------------------------------------------------------------------
    # dividers and progress
    # ------------------------------------------------------------------

    def _summary_restore_sash(self):
        """Put the dividers back where they were left (once, when the tab
        is first on screen - a pane has no size to divide before that)."""
        if self._ssash_done:
            return
        self._ssash_done = True
        try:
            self.root.update_idletasks()
            high = self.spane_v.winfo_height()
            if len(self._ssash) < 2:        # first time: most of the room
                self._ssash = [self.spane_h.sashpos(0),    # to the file list
                               max(120, high - 200)]
            x, y = (int(v) for v in self._ssash[:2])
            if 120 <= x <= self.spane_h.winfo_width() - 200:
                self.spane_h.sashpos(0, x)
            if 80 <= y <= high - 60:
                self.spane_v.sashpos(0, y)
        except (ValueError, TypeError, tk.TclError):
            pass

    def _summary_sash(self):
        """Where the dividers are now, for the settings file."""
        try:
            if self._ssash_done and self.spane_h.winfo_width() > 1:
                return [int(self.spane_h.sashpos(0)),
                        int(self.spane_v.sashpos(0))]
        except tk.TclError:
            pass
        return list(self._ssash)

    def _summary_progress_begin(self, kind, text):
        """Show the bar and Stop beside the status line. It starts as a
        moving 'busy' bar and turns into a filling one as soon as the run
        reports how far it has got."""
        if self.proc is None:           # the run did not start
            return
        self._skind = kind
        self.var_sstatus.set(text)
        self.btn_sstop.pack(side="right", padx=(8, 0))
        self.sbar.pack(side="right", padx=(12, 0))
        self.sbar.configure(mode="indeterminate")
        self.sbar.start(14)

    def _summary_progress(self, p):
        """A progress line from the run this tab started."""
        total, seen = p.get("total") or 0, p.get("seen", 0)
        kind = getattr(self, "_skind", "")
        if not total:
            return
        if kind == "summary" and seen >= total:
            # every file read: grouping them reports nothing until done
            if str(self.sbar.cget("mode")) != "indeterminate":
                self.sbar.configure(mode="indeterminate")
                self.sbar.start(14)
            self.var_sstatus.set("Read {:,} files - now sorting them into "
                                 "sections...".format(total))
            return
        if str(self.sbar.cget("mode")) != "determinate":
            self.sbar.stop()
            self.sbar.configure(mode="determinate", maximum=100)
        pct = min(100.0, 100.0 * seen / total)
        self.sbar.configure(value=pct)
        if kind == "summary":
            text = "Reading files: {:,} of {:,}".format(seen, total)
        elif kind == "summary-setup":
            text = "Downloading: {:,} of {:,} MB".format(seen, total)
        else:
            text = "Writing summaries: step {:,} of {:,}".format(seen, total)
        self.var_sstatus.set("{}  ({:.0f}%)".format(text, pct))

    def _summary_progress_end(self):
        self._skind = ""
        self.sbar.stop()
        self.sbar.pack_forget()
        self.btn_sstop.pack_forget()

    # ------------------------------------------------------------------
    # the folder
    # ------------------------------------------------------------------

    def _summary_scope(self):
        return fs.norm_scope(self.var_sfolder.get())

    def _summary_browse(self):
        d = filedialog.askdirectory(title="Choose the folder to look at",
                                    initialdir=self.var_sfolder.get() or None)
        if d:
            self.summary_go(os.path.normpath(d))

    def summary_go(self, folder):
        """Make `folder` the one this tab is about, and show what is known."""
        self.var_sfolder.set(fs.norm_scope(folder))
        self.summary_load()

    def summary_up(self):
        scope = self._summary_scope()
        if not scope:
            return
        sep = "\\" if (len(scope) >= 2 and scope[1] == ":") \
            or scope.startswith("\\\\") else "/"
        self.summary_go(scope.rsplit(sep, 1)[0] if sep in scope else "")

    def _summary_go_menu(self):
        """Rebuilt each time it opens: Up, then the folders inside."""
        m = self.sgo_menu
        m.delete(0, "end")
        scope = self._summary_scope()
        m.add_command(label="Up one folder", command=self.summary_up,
                      state="normal" if scope else "disabled")
        m.add_command(label="Everything in the index",
                      command=lambda: self.summary_go(""),
                      state="normal" if scope else "disabled")
        m.add_separator()
        if not self._ssubs:
            m.add_command(label="(no folders inside this one)",
                          state="disabled")
        for path, n, own in self._ssubs[:MENU_FOLDERS]:
            name = (os.path.basename(path) or path) if scope else path
            m.add_command(
                label="{}    {:,} files{}".format(
                    name, n, "  -  summarised" if own else ""),
                command=lambda p=path: self.summary_go(p))
        if len(self._ssubs) > MENU_FOLDERS:
            m.add_command(label="...and {:,} smaller ones - use Browse"
                          .format(len(self._ssubs) - MENU_FOLDERS),
                          state="disabled")

    def _summary_file_folder(self):
        paths = self._paths_of(self.stree_files, self._spaths)
        if paths:
            self.summary_go(os.path.dirname(paths[0]))

    def summary_from_search(self):
        """Search tab > right-click > Summarise this folder: the selected
        folder (or the folder of the selected file) becomes this tab's
        folder; if it has never been summarised, the run starts."""
        row = self.selected_row()
        if not row:
            return
        folder = row["path"] if row.get("is_dir") \
            else os.path.dirname(row["path"])
        self._sautorun = True
        self.nb.select(self.tab_summary)
        self.summary_go(folder)

    def _summary_tab_shown(self):
        self.root.after(60, self._summary_restore_sash)
        if time.time() - self._sai["at"] > 20:
            self._summary_ai_check()
        if self._srun is None and not self._ssubs:
            self.summary_load()

    # ------------------------------------------------------------------
    # loading what is stored
    # ------------------------------------------------------------------

    def summary_load(self):
        """Read this folder's stored summary (or the nearest one above it)
        and its sub-folders from the index, off the UI thread."""
        self._sgen += 1
        gen = self._sgen
        scope = self._summary_scope()
        self.var_sstatus.set("Looking...")

        def work():
            conn = None
            try:
                conn = self._ro()
                run, exact = fs.find_run(conn, scope)
                secs = fs.sections(conn, run["id"],
                                   None if exact else scope) if run else []
                own = {r["scope"] for r in fs.runs(conn)}
                subs = [(p, n, p in own)
                        for p, n in fs.subfolders(conn, scope)]
                digest = fs.digest_for(conn, scope)
                in_index = conn.execute(
                    "SELECT COUNT(*) FROM files f WHERE f.is_dir=0"
                    + fs._scope_sql(scope)[0],
                    fs._scope_sql(scope)[1]).fetchone()[0]
                self.msgs.put(("call", self._summary_show,
                               (gen, scope, run, exact, secs, subs,
                                in_index, None, digest)))
            except (sqlite3.Error, OSError) as exc:
                self.msgs.put(("call", self._summary_show,
                               (gen, scope, None, False, [], [], 0,
                                str(exc), None)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _summary_show(self, gen, scope, run, exact, secs, subs, in_index,
                      err, digest=None):
        if gen != self._sgen:
            return
        self._srun, self._sexact = run, exact
        self._sdigest = digest
        self._ssubs = subs
        keep = self.stree_secs.selection()
        self.stree_secs.delete(*self.stree_secs.get_children())
        self.stree_files.delete(*self.stree_files.get_children())
        self._ssections = {}
        self._spaths, self._sfiles = {}, {}
        title = scope or "Everything in the index"
        if err:
            self.var_sstatus.set("Could not read the index: " + err)
            self._summary_text([("h", "Could not read the index\n"),
                                ("", err)])
            return
        auto, self._sautorun = self._sautorun, False
        note, self._snote = self._snote, ""
        if not in_index:
            self.var_sstatus.set("Nothing in the index under this folder.")
            self._summary_text([
                ("h", title + "\n"),
                ("", "The index holds no files under this folder, so there "
                     "is nothing to summarise yet. Add the folder (or the "
                     "drive it is on) on the Index tab and run an index.")])
            return
        if run is None or (auto and not exact):
            self.var_sstatus.set(
                "{:,} files here, not summarised yet - press Summarise."
                .format(in_index))
            self._summary_text([
                ("h", title + "\n"),
                ("", "{:,} files in the index here, not summarised yet.\n"
                     .format(in_index)),
                ("dim", "Summarise sorts them into sections by subject, "
                        "using the text findex already holds - it opens "
                        "nothing on disk.")])
            if auto and self.proc is None:
                self.summary_run()
            return
        self.stree_secs.insert("", "end", iid=OVERVIEW, tags=("head",),
                               values=("Overview", "{:,}".format(
                                   sum(s["n"] for s in secs))))
        for i, s in enumerate(secs):
            iid = "s{}".format(s["id"])
            tags = ("odd",) if i % 2 == 0 else ()
            if s["kind"] != "topic":
                tags += ("dim",)
            self.stree_secs.insert(
                "", "end", iid=iid, tags=tags,
                values=(s["ai_title"] or s["label"], "{:,}".format(s["n"])))
            self._ssections[iid] = s
        when = time.strftime("%d %b %Y, %H:%M",
                             time.localtime(run["created"] or 0))
        if exact:
            status = "{:,} files in {:,} sections  -  summarised {}".format(
                run["files"], len(secs), when)
        else:
            status = ("Showing the sections of {} that reach into this "
                      "folder. Press Summarise for sections of its own."
                      .format(run["scope"] or "the whole index"))
        self.var_sstatus.set((note + "  " if note else "") + status)
        pick = keep[0] if keep and self.stree_secs.exists(keep[0]) \
            else OVERVIEW
        show, self._sshow_digest = self._sshow_digest, False
        self.stree_secs.selection_set(pick)
        self._summary_show_section()
        if show and digest:
            # just written: put it on the card - a moment later, because
            # selecting the row above redraws the card when Tk next idles
            self.root.after(300, self._summary_show_digest)

    # ------------------------------------------------------------------
    # one section / one file
    # ------------------------------------------------------------------

    def _summary_text(self, parts):
        w = self.sdetail
        w.configure(state="normal")
        w.delete("1.0", "end")
        for tag, text in parts:
            if text:
                w.insert("end", text, (tag,) if tag else ())
        w.configure(state="disabled")
        w.yview_moveto(0)

    def _summary_overview_text(self):
        run, secs = self._srun, list(self._ssections.values())
        o = run["overview"]
        parts = [("h", (self._summary_scope()
                        or "Everything in the index") + "\n")]
        if run.get("ai") and self._sexact:
            parts.append(("", run["ai"] + "\n"))
        if self._sexact:
            parts.append(("", "{:,} files, {}. {:,} had text to read.\n"
                          .format(run["files"],
                                  findex.human(run["bytes"] or 0),
                                  run["text_files"] or 0)))
            if o.get("doctypes"):
                parts += [("key", "Mostly  "),
                          ("", ",  ".join("{} ({:,})".format(k, c)
                                          for k, c in o["doctypes"][:5])
                           + "\n")]
            if o.get("keywords"):
                parts += [("key", "About  "),
                          ("", ",  ".join(o["keywords"][:10]) + "\n")]
        else:
            parts.append(("", "{:,} files here, in {:,} of the sections "
                              "found when {} was summarised.\n".format(
                                  sum(s["n"] for s in secs), len(secs),
                                  run["scope"] or "the whole index")))
        if not run.get("ai") and self._sexact:
            parts.append(("dim", "AI summaries > Summarise this whole "
                                 "folder writes one summary of everything "
                                 "in it.\n"))
        return parts + self._summary_digest_parts()

    def _summary_digest_parts(self, heading=False):
        """The latest combined summary of hand-picked files, for the card."""
        d = self._sdigest
        if not d or not d.get("text"):
            return []
        title = "The {:,} selected files, together".format(d["n"])
        when = time.strftime("%d %b %Y, %H:%M",
                             time.localtime(d["created"] or 0))
        return [("h" if heading else "key", ("" if heading else "\n")
                 + title + "\n"),
                ("", d["text"] + "\n"),
                ("dim", "{}{}  -  written {} by {}\n".format(
                    d["names"] or "", " ..." if d["n"] > 6 else "", when,
                    d["model"] or "a local model"))]

    def _summary_show_digest(self):
        if self._sdigest:
            self._scard = None
            self._summary_text(self._summary_digest_parts(heading=True))

    def _summary_in_short(self, rows, terms):
        """Card lines for what a set of files adds up to (no AI): the
        period their text mentions, recurring names, and typical files."""
        d = fs.in_short(rows, terms)
        parts = []
        if d["years"]:
            lo, hi = d["years"]
            parts += [("key", "Covers  "),
                      ("", (lo if lo == hi else lo + " to " + hi)
                       + "  (years mentioned in the files)\n")]
        if d["names"]:
            parts += [("key", "Names that recur  "),
                      ("", ",  ".join(d["names"]) + "\n")]
        if d["typical"]:
            parts.append(("key", "Typical of these files\n"))
            for name, said in d["typical"]:
                parts += [("", "  " + name + "  "),
                          ("dim", said[:260] + "\n")]
        return parts

    def _summary_show_section(self):
        sel = self.stree_secs.selection()
        if not sel or self._srun is None:
            return
        key = sel[0]
        s = self._ssections.get(key)
        self._scard = None
        if key == OVERVIEW:
            parts = self._summary_overview_text()
            self._scard = (parts, ", ".join(
                self._srun["overview"].get("keywords") or []))
            self._summary_text(parts)
        elif s:
            parts = [("h", "{}\n".format(s["ai_title"] or s["label"]))]
            if s["ai"]:
                parts.append(("", s["ai"] + "\n"))
            parts.append(("dim", "{:,} files,  {}\n".format(
                s["n"], findex.human(s["bytes"]))))
            if s["terms"]:
                parts += [("key", "About  "),
                          ("", s["terms"].replace(";", ",") + "\n")]
            if not s["ai"] and s["kind"] != "type":
                parts.append(("dim", "AI summaries > Summarise this whole "
                                     "folder writes one summary of all "
                                     "these files.\n"))
            self._scard = (parts, s["terms"])
            self._summary_text(parts)
        self._sfgen += 1
        gen = self._sfgen
        scope = self._summary_scope()
        exact = self._sexact

        def work():
            conn = None
            try:
                conn = self._ro()
                if s:
                    rows = fs.section_files(conn, s["id"],
                                            None if exact else scope, MAX_ROWS)
                    total = s["n"]
                else:
                    rows = fs.recent_files(conn, scope, 500)
                    total = None
                self.msgs.put(("call", self._summary_fill_files,
                               (gen, rows, total)))
            except sqlite3.Error as exc:
                self.msgs.put(("status", "Summary: {}".format(exc)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _summary_fill_files(self, gen, rows, total):
        if gen != self._sfgen:
            return
        tree = self.stree_files
        tree.delete(*tree.get_children())
        self._spaths, self._sfiles = {}, {}
        label = fs.DOCTYPE_LABEL
        for i, f in enumerate(rows):
            iid = tree.insert(
                "", "end", tags=("odd",) if i % 2 else (),
                values=(os.path.basename(f["path"]),
                        label.get(f["doctype"], ""),
                        f["title"] or (f["keywords"] or "").replace(";", ",")))
            self._spaths[iid] = f["path"]
            self._sfiles[iid] = f
        if self._scard and not tree.selection():
            # the card of the section (or folder) on show: add what its
            # files amount to, now that their cards are here
            parts, terms = self._scard
            self._summary_text(parts + self._summary_in_short(rows, terms))
        if total is None:
            self.var_status.set("Overview: the {:,} most recently changed "
                                "files".format(len(rows)))
        elif total > len(rows):
            self.var_status.set("Showing the newest {:,} of {:,} - More > "
                                "Show this section in Search lists them all"
                                .format(len(rows), total))
        else:
            self.var_status.set("{:,} file(s) in this section".format(
                len(rows)))

    def _summary_show_file(self):
        sel = self.stree_files.selection()
        if len(sel) != 1 or sel[0] not in self._sfiles:
            return
        f = self._sfiles[sel[0]]
        parts = [("h", (f["title"] or os.path.basename(f["path"])) + "\n")]
        summary = f["ai"] or f["gist"]
        if summary:
            parts.append(("", summary + "\n"))
        if f["doctype"]:
            parts += [("key", "Kind  "),
                      ("", fs.DOCTYPE_LABEL.get(f["doctype"], f["doctype"])
                       + ("   (summary written by AI)" if f["ai"] else "")
                       + "\n")]
        else:
            parts.append(("dim", "No text in the index for this file - it "
                                 "is grouped by its type.\n"))
        if f["keywords"]:
            parts += [("key", "About  "),
                      ("", f["keywords"].replace(";", ",") + "\n")]
        found = fs.entities_text(f["entities"], "\n")
        for line in found.split("\n") if found else ():
            name, _, value = line.partition(": ")
            parts += [("key", name + "  "), ("", value + "\n")]
        parts.append(("dim", f["path"]))
        self._summary_text(parts)

    # ------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------

    def _summary_detail(self):
        return self.var_sdetail.get() or "normal"

    def _summary_save_prefs(self):
        try:
            _g().save_setting("ai_model", self.var_smodel.get().strip())
            _g().save_setting("summary_detail", self._summary_detail())
        except Exception:                                      # noqa: BLE001
            pass

    def _summary_busy(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running - "
                                        "wait for it, or press Stop on the "
                                        "Index tab.")
            return True
        return False

    def summary_run(self):
        if self._summary_busy():
            return
        scope = self._summary_scope()
        self.var_sfolder.set(scope)
        self._summary_save_prefs()
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "summarise",
                                       "--detail", self._summary_detail(),
                                       "--progress"]
        if scope:
            cmd.append(scope)
        self._progress_est = 0
        self.launch(cmd, "summary", "Summarising {}...".format(
            scope or "the whole index"))
        self._summary_progress_begin("summary", "Summarising...")

    def summary_forget(self):
        if self._srun is None or not self._sexact or self._summary_busy():
            return
        scope = self._summary_scope()
        if not messagebox.askyesno(
                "Forget this summary?",
                "Remove the sections of {}?\n\nThe files are untouched, and "
                "their cards are kept, so summarising again is quick."
                .format(scope or "the whole index")):
            return
        try:
            conn = findex.open_db(self.var_db.get(), timeout=5)
            fs.forget(conn, scope)
            conn.close()
        except sqlite3.Error as exc:
            self.var_sstatus.set("Could not remove it: {}".format(exc))
            return
        self.summary_load()

    # ------------------------------------------------------------------
    # local AI
    # ------------------------------------------------------------------

    def _summary_ai_menu(self):
        """Rebuilt each time it opens, from the last look at Ollama."""
        m, st = self.sai_menu, self._sai
        m.delete(0, "end")
        models = [x for x in st["models"] if "embed" not in x.lower()]
        ready = st["ok"] and bool(models)
        gpu = fs.gpu_info()
        if ready:
            current = fs.pick_model(models, self.var_smodel.get().strip()
                                    or None)
            self.var_smodel.set(current)
            m.add_command(label="Ready - using {}".format(current),
                          state="disabled")
            where = st.get("where")
            if where and where.get("model") == current:
                m.add_command(label="    loaded {}".format(where["text"]),
                              state="disabled")
        elif st["ok"]:
            m.add_command(label="Ready for a model - none downloaded yet",
                          state="disabled")
        else:
            m.add_command(label="Not set up yet (optional)",
                          state="disabled")
        m.add_command(label="    " + (
            "{} - {} GB for models".format(gpu["name"], gpu["vram_gb"])
            if gpu["kind"] else "No graphics card found - models run on "
                                "the CPU"), state="disabled")
        m.add_separator()
        m.add_command(label="Summarise the selected files  "
                            "(each one, then all of them together)",
                      command=self.summary_ai_files)
        m.add_command(label="Summarise this whole folder  "
                            "(every section, then the folder)",
                      command=self.summary_ai_sections)
        m.add_separator()
        sub = self.smodel_menu
        sub.delete(0, "end")
        for name in models:
            sub.add_radiobutton(label=name, value=name,
                                variable=self.var_smodel,
                                command=self._summary_save_prefs)
        if models:
            sub.add_separator()
        offered = 0
        best = fs.tier_model()
        if best and not fs.has_model(models, best):
            offered += 1
            sub.add_command(
                label="Download {}   {}  -  the best fit for this "
                      "computer's GPU".format(best, fs.model_size(best)),
                command=lambda n=best: self.summary_ai_setup(n))
        for name, size, note in fs.AI_MODELS:
            if name in models:
                continue
            offered += 1
            sub.add_command(
                label="Download {}   {}  -  {}".format(name, size, note),
                command=lambda n=name: self.summary_ai_setup(n))
        others = [(need, name, size, note) for need, name, size, note
                  in fs.AI_TIERS if name != best and name not in models]
        if others:
            big = tk.Menu(sub, tearoff=0)
            self._menus.append(big)
            for need, name, size, note in others:
                big.add_command(
                    label="Download {}   {}  -  needs a {} GB card; {}"
                    .format(name, size, need, note),
                    command=lambda n=name: self.summary_ai_setup(n))
            sub.add_cascade(label="Bigger models (need a graphics card)",
                            menu=big)
        if offered > 1:
            sub.add_command(
                label="Download all the small ones   {} in total".format(
                    fs.AI_MODELS_TOTAL),
                command=lambda: self.summary_ai_setup("all"))
        if not offered:
            sub.add_command(label="(all the suggested models are installed)",
                            state="disabled")
        m.add_cascade(label="Model", menu=sub)
        if not ready:
            m.add_command(label="Set up (downloads {})".format(
                self._summary_auto_text()),
                command=self.summary_ai_setup)

    def _summary_auto_text(self):
        """'gemma3:1b (815 MB) and gemma3:12b (8.1 GB), plus nomic-embed-text
        (274 MB) for search by meaning' - what Set up fetches here."""
        names = [m for m in fs.auto_models() if m != fs.EMBED_MODEL]
        return " and ".join("{} ({})".format(n, fs.model_size(n))
                            for n in names) + \
            ", plus {} ({}) for search by meaning".format(
                fs.EMBED_MODEL, fs.EMBED_MODEL_SIZE)

    def _summary_ai_cmd(self):
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "summarise",
                                       "--progress"]
        model = self.var_smodel.get().strip()
        if model:
            cmd += ["--model", model]
        used = fs.pick_model(self._sai["models"], model or None)
        if used:
            self._sused.add(used)       # unloaded again when the app closes
        return cmd

    def _summary_ai_ready(self):
        if self._sai["ok"] and self._sai["models"]:
            return True
        if messagebox.askyesno(
                "AI summaries are not set up",
                "Written summaries need a language model on this computer. "
                "Setting up downloads {}{}. No app is installed - it stays "
                "in findex's own folder and runs hidden, only while findex "
                "is open. Nothing is sent anywhere.\n\n{}\n\nSet it up now?"
                .format(self._summary_auto_text(),
                        self._summary_engine_note(), fs.gpu_line())):
            self.summary_ai_setup(confirm=False)
        return False

    def summary_ai_sections(self):
        if self._summary_busy():
            return
        if self._srun is None or not self._sexact:
            messagebox.showinfo(
                "Summarise first",
                "This folder has no summary of its own yet. Press "
                "Summarise first.")
            return
        if not self._summary_ai_ready():
            return
        self._summary_save_prefs()
        cmd = self._summary_ai_cmd() + ["--ai-sections"]
        scope = self._summary_scope()
        if scope:
            cmd.append(scope)
        self._progress_est = 0
        self.launch(cmd, "summary-ai", "Summarising the whole folder...")
        self._summary_progress_begin(
            "summary-ai", "Summarising every section, then the folder - "
                          "waiting for the model...")

    def summary_ai_files(self):
        if self._summary_busy():
            return
        picked = [self._sfiles[i] for i in self.stree_files.selection()
                  if i in self._sfiles]
        ids = [f["id"] for f in picked
               if f["doctype"] and f["doctype"] not in fs.NON_PROSE]
        if not picked:
            messagebox.showinfo("Select files", "Select one or more files "
                                                "in the list first.")
            return
        if not ids:
            messagebox.showinfo(
                "Nothing to read",
                "The selected files have no readable text in the index "
                "(pictures, video, archives and the like), so there is "
                "nothing to summarise.")
            return
        if not self._summary_ai_ready():
            return
        if len(ids) > AI_CONFIRM_ABOVE and not messagebox.askyesno(
                "Summarise {:,} files?".format(len(ids)),
                "This takes a few seconds per file. Stop on the Index tab "
                "cancels and keeps what was done.\n\nGo ahead?"):
            return
        self._summary_save_prefs()
        cmd = self._summary_ai_cmd() + [
            "--ai-ids", ",".join(str(i) for i in ids)]
        scope = self._summary_scope()
        if scope:
            cmd.append(scope)       # where the combined summary is kept
        self._sshow_digest = len(ids) > 1
        self._progress_est = 0
        self.launch(cmd, "summary-ai", "Summarising {:,} file(s) with "
                                       "AI...".format(len(ids)))
        self._summary_progress_begin(
            "summary-ai", "Summarising {:,} file(s) - waiting for the "
                          "model...".format(len(ids)))

    def summary_ai_setup(self, model=None, confirm=True):
        """Fetch/start the engine if needed and download `model` (the
        default small one when None)."""
        if self._summary_busy():
            return
        name = model or "auto"
        size = fs.model_size(name)
        if name == "all":
            size = fs.AI_MODELS_TOTAL + " in total"
        what = ("Every suggested small model" if name == "all" else
                self._summary_auto_text() if name == "auto" else name)
        if confirm and not messagebox.askyesno(
                "Download {}?".format("all the suggested models"
                                      if name == "all" else
                                      "the models for this computer"
                                      if name == "auto" else name),
                "{}{} will be downloaded{}. It all runs on this "
                "computer.\n\nProgress shows on the Index tab. Go ahead?"
                .format(what, " ({})".format(size) if size else "",
                        "" if self._sai["ok"] else
                        self._summary_engine_note())):
            return
        self._swant_model = name
        cmd = _g().engine_command() + ["--db", self.var_db.get(), "summarise",
                                       "--ai-setup", "--model", name,
                                       "--progress"]
        self._progress_est = 0
        what = ("the AI models" if name == "all" else
                "the models for this computer" if name == "auto" else name)
        self.launch(cmd, "summary-setup", "Getting {}...".format(what))
        self._summary_progress_begin(
            "summary-setup", "Getting {} ready...".format(what))

    def _summary_finished(self, kind, code):
        """The engine child for this tab ended."""
        self._summary_progress_end()
        if kind == "summary":
            if code != 0:
                self._snote = "Stopped early - run it again to carry on."
            self.summary_load()
        elif kind == "summary-ai":
            if code == 2:
                self._snote = ("The AI model could not be reached - see "
                               "the Index tab's Output.")
            self._summary_ai_check()    # also notes where the model landed
            self.summary_load()
        elif kind == "summary-setup":
            want, self._swant_model = self._swant_model, ""
            if code == 0 and want in ("all", "auto"):
                self.var_sstatus.set("The AI models are installed.")
                if want == "auto":
                    self.var_smodel.set("")     # let pick_model choose
                    self._summary_save_prefs()
            elif code == 0 and want:
                self.var_smodel.set(want)
                self._summary_save_prefs()
                self.var_sstatus.set("{} is ready - AI summaries will use "
                                     "it.".format(want))
            self._summary_ai_check()
            if code == 3:
                self.var_sstatus.set("A model could not be downloaded - "
                                     "see the Index tab's Output.")
            if code == 2:
                messagebox.showinfo(
                    "Could not set up AI summaries",
                    "The AI engine could not be downloaded or started - "
                    "the reason is in the Index tab's Output. Check the "
                    "internet connection and try AI summaries > Set up "
                    "again.")

    def _summary_engine_note(self):
        """' and the engine that runs it (170 MB)' when findex has no
        engine on this computer yet, else ''."""
        if fs.find_ollama():
            return ""
        size = fs.engine_size()
        return " and the engine that runs it{}".format(
            " ({})".format(size) if size else "")

    def _summary_shutdown(self):
        """The app is closing: end the Ollama findex started and the model
        processes under it, or - when Ollama was already running before
        findex - just unload the models findex used. Quick, and never
        allowed to stop the window closing."""
        try:
            fs.ai_stop(models=sorted(self._sused))
        except Exception:                                      # noqa: BLE001
            pass

    def _summary_ai_check(self):
        self._sai["at"] = time.time()

        def work():
            fs.gpu_info()               # looked up once, off the UI thread
            st = fs.ai_status()
            if not st["ok"] and fs.find_ollama():
                # installed (a build does that) but not running: start it,
                # so the models it already has are simply there to use
                st = fs.ai_start(log=lambda *a: None)
            if st["ok"]:
                st["where"] = fs.ai_placement()     # None unless loaded
            self.msgs.put(("call", self._summary_ai_state, (st,)))
        threading.Thread(target=work, daemon=True).start()

    def _summary_ai_state(self, st):
        st["at"] = time.time()
        self._sai = st

    # ------------------------------------------------------------------
    # onwards
    # ------------------------------------------------------------------

    def summary_to_search(self):
        sel = self.stree_secs.selection()
        s = self._ssections.get(sel[0]) if sel else None
        scope = self._summary_scope()
        if s:
            query = "section:#{}".format(s["id"])
            if not self._sexact and scope:
                query += ' path:"{}"'.format(scope)
        elif scope:
            query = 'path:"{}"'.format(scope)
        else:
            return
        self.var_exts.set("")
        self.var_query.set(query)
        self.nb.select(self.tab_search)
        self.run_search(live=False)

    def summary_export(self):
        if self._srun is None:
            messagebox.showinfo("Summarise first", "There is no summary of "
                                                   "this folder to export.")
            return
        scope = self._summary_scope()
        name = os.path.basename(scope) or "index"
        path = filedialog.asksaveasfilename(
            title="Export this folder's summary",
            initialdir=self.save_dir() if hasattr(self, "save_dir")
            else findex.downloads_dir(),
            initialfile="findex-summary-{}-{}.html".format(
                "".join(ch if ch.isalnum() or ch in "-_ " else "_"
                        for ch in name), time.strftime("%Y-%m-%d")),
            defaultextension=".html",
            filetypes=[("Web page (HTML)", "*.html"),
                       ("One file per row (CSV)", "*.csv"),
                       ("Text", "*.txt"), ("JSON", "*.json")])
        if not path:
            return
        run, exact = self._srun, self._sexact

        def work():
            conn = None
            try:
                conn = self._ro()
                fs.export(conn, run, path, None, None if exact else scope)
                self.msgs.put(("status", "Summary written to " + path))
                self.msgs.put(("call", _g().reveal_path, (path,)))
            except Exception as exc:                           # noqa: BLE001
                self.msgs.put(("status", "Export failed: {}".format(exc)))
            finally:
                if conn is not None:
                    conn.close()
        threading.Thread(target=work, daemon=True).start()
