#!/usr/bin/env python3
"""
findex_gui - desktop front end for findex.

Run it:
    Windows   double-click findex-gui.bat
    Any OS    python findex_gui.py        or        python findex.py gui

Search tab  filename search (live as you type) and full-text content search.
Index tab   pick folders, run an index, watch progress, auto re-index on a timer.

Settings are kept in findex_gui.json next to this script. Indexing runs as a
separate findex.py process so the window never freezes and Stop always works.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import findex  # noqa: E402

SETTINGS_PATH = os.path.join(HERE, "findex_gui.json")
FINDEX_PY = os.path.join(HERE, "findex.py")
DEFAULT_DB = os.path.join(HERE, "findex.db")

POLL_MS = 100            # queue drain interval
LIVE_SEARCH_MS = 120     # debounce for search-as-you-type
AUTO_CHECK_MS = 20000    # how often the auto re-index timer is checked
MAX_LOG_LINES = 500

# Tk 8.5 (older macOS system Tk) has no ttk.Spinbox - fall back to the classic.
Spinbox = getattr(ttk, "Spinbox", tk.Spinbox)

DEFAULTS = {
    "db": "findex.db",
    "roots": [],
    "workers": 0,
    "rebuild": False,
    "include_cloud": False,
    "ocr": False,
    "theme": "system",
    "auto_index": False,
    "auto_minutes": 60,
    "limit": 0,
    "mode": "name",
    "exts": "",
    "geometry": "1060x700",
}


# ---------------------------------------------------------------------------
# Settings + small helpers
# ---------------------------------------------------------------------------

def portable(path):
    """Store a path relative to this folder when it lives inside it.

    Keeps findex_gui.json free of machine-specific paths, so the whole folder
    can be moved, renamed, copied to the other PC or carried on a USB stick and
    still find its own index.
    """
    if not path:
        return path
    try:
        rel = os.path.relpath(path, HERE)
    except ValueError:          # a different drive on Windows
        return path
    return rel if not rel.startswith("..") else path


def resolve(path, fallback=None):
    """Inverse of portable(): make a stored path usable again."""
    if not path:
        return fallback
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(HERE, path))


def load_settings():
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            for key in DEFAULTS:
                if key in saved:
                    data[key] = saved[key]
    except (OSError, ValueError):
        pass
    # Relative paths resolve against this folder. An absolute one that no longer
    # exists - the folder was moved, or the index was pointed somewhere else on
    # another machine - falls back to the index next to this script rather than
    # failing every search.
    db = resolve(data.get("db"), DEFAULT_DB)
    if not os.path.exists(db) and not os.path.isdir(os.path.dirname(db) or "."):
        db = DEFAULT_DB
    data["db"] = db
    data["roots"] = [resolve(r) for r in (data.get("roots") or []) if r]
    # A one-off action, never a remembered setting: leaving this on by accident
    # would make every future run re-read every file.
    data["rebuild"] = False
    if data.get("limit") == 200:
        data["limit"] = 0    # the old default cap; the default is now ALL
    return data


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


# The Windows built-in OCR engine is reached through these maintained,
# wheel-only packages (the old all-in-one winsdk package stopped shipping
# wheels for new Python versions and tries to compile itself instead).
WINRT_PACKAGES = [
    "winrt-runtime",
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
    "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging",
    "winrt-Windows.Media.Ocr",
    "winrt-Windows.Storage.Streams",
]

LIGHT_PALETTE = {
    "bg": "#f5f6f8", "fg": "#16191d", "field": "#ffffff",
    "btn": "#e8eaee", "btn_hi": "#dde0e6",
    "sel": "#1f6fd6", "sel_fg": "#ffffff",
    "row_alt": "#f2f4f7", "hint": "#5c6672", "accent": "#1a5fb4",
    "hit_bg": "#ffe27a", "hit_fg": "#16191d",
    "tip_bg": "#fffbe6", "tip_fg": "#20262c",
    "border": "#cfd4db",
}
DARK_PALETTE = {
    "bg": "#1e2226", "fg": "#e2e8ee", "field": "#272c31",
    "btn": "#343b42", "btn_hi": "#3f474f",
    "sel": "#2f74c9", "sel_fg": "#ffffff",
    "row_alt": "#2c3238", "hint": "#9aa5b0", "accent": "#79b3ef",
    "hit_bg": "#8a6d00", "hit_fg": "#ffffff",
    "tip_bg": "#3a4048", "tip_fg": "#e8edf2",
    "border": "#3d444c",
}


def system_dark():
    """Is the operating system currently in dark mode?"""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g",
                                "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=5)
            return "dark" in r.stdout.lower()
        if os.name == "nt":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes"
                r"\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except Exception:
        pass
    return False


def missing_packages():
    """Python components the app wants but this environment lacks."""
    import importlib.util as iu
    missing = []
    if iu.find_spec("pymupdf") is None and iu.find_spec("fitz") is None:
        missing.append("pymupdf")          # PDF text extraction
    if iu.find_spec("mutagen") is None:
        missing.append("mutagen")          # music/video tags
    if iu.find_spec("extract_msg") is None:
        missing.append("extract-msg")      # Outlook .msg emails
    if sys.platform == "darwin" and iu.find_spec("Vision") is None:
        missing.append("pyobjc-framework-Vision")   # macOS built-in OCR
    if os.name == "nt":
        have = False
        for mod in ("winrt.windows.media.ocr", "winsdk.windows.media.ocr"):
            try:
                if iu.find_spec(mod) is not None:
                    have = True
                    break
            except Exception:
                pass
        if not have:
            missing += WINRT_PACKAGES               # Windows built-in OCR
    return missing


def in_venv():
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def child_python():
    """Executable used for background findex.py runs.

    The GUI itself is launched with pythonw.exe on Windows so no console
    appears, but pythonw is a poor parent for multiprocessing, so children
    are started with the matching python.exe instead (CREATE_NO_WINDOW keeps
    it invisible).
    """
    exe = sys.executable or "python"
    if os.name == "nt":
        base = os.path.basename(exe).lower()
        if base.startswith("pythonw"):
            twin = os.path.join(os.path.dirname(exe),
                                base.replace("pythonw", "python", 1))
            if os.path.exists(twin):
                return twin
    return exe


def no_window():
    """Keep a console window from flashing up on Windows."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def open_path(path):
    try:
        if os.name == "nt":
            os.startfile(path)                              # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showerror("Could not open", "{}\n\n{}".format(path, exc))


def reveal_path(path):
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as exc:
        messagebox.showerror("Could not open folder", "{}\n\n{}".format(path, exc))


def _win_set_file_clipboard(paths, move):
    """Put real files on the Windows clipboard (CF_HDROP), so they can be
    pasted in Explorer. move=True marks them as cut."""
    try:
        import ctypes
        import struct
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        def hglobal(payload):
            handle = kernel32.GlobalAlloc(0x2, len(payload))   # GMEM_MOVEABLE
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            return handle

        dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1)      # wide paths
        dropfiles += ("\0".join(paths) + "\0\0").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            user32.SetClipboardData(15, hglobal(dropfiles))    # CF_HDROP
            fmt = user32.RegisterClipboardFormatW("Preferred DropEffect")
            user32.SetClipboardData(
                fmt, hglobal(struct.pack("<I", 2 if move else 5)))
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def _win_get_file_clipboard():
    """Files currently on the Windows clipboard (copied in Explorer)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        user32.GetClipboardData.restype = ctypes.c_void_p
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_wchar_p, ctypes.c_uint]
        if not user32.IsClipboardFormatAvailable(15):
            return []
        if not user32.OpenClipboard(None):
            return []
        try:
            handle = user32.GetClipboardData(15)
            if not handle:
                return []
            out = []
            for i in range(shell32.DragQueryFileW(handle, 0xFFFFFFFF,
                                                  None, 0)):
                n = shell32.DragQueryFileW(handle, i, None, 0)
                buf = ctypes.create_unicode_buffer(n + 1)
                shell32.DragQueryFileW(handle, i, buf, n + 1)
                out.append(buf.value)
            return out
        finally:
            user32.CloseClipboard()
    except Exception:
        return []


def _mac_set_file_clipboard(paths):
    """Put real files on the macOS pasteboard, so they can be pasted in
    Finder."""
    try:
        from AppKit import NSPasteboard
        from Foundation import NSURL
    except ImportError:
        return False
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.writeObjects_(
            [NSURL.fileURLWithPath_(p) for p in paths]))
    except Exception:
        return False


def _mac_get_file_clipboard():
    """Files currently on the macOS pasteboard (copied in Finder)."""
    try:
        from AppKit import NSPasteboard
        from Foundation import NSURL
    except ImportError:
        return []
    try:
        pb = NSPasteboard.generalPasteboard()
        urls = pb.readObjectsForClasses_options_([NSURL], None) or []
        return [str(u.path()) for u in urls if u.isFileURL()]
    except Exception:
        return []


def _win_recycle(paths):
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND),
                        ("wFunc", ctypes.c_uint),
                        ("pFrom", ctypes.c_wchar_p),
                        ("pTo", ctypes.c_wchar_p),
                        ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", ctypes.c_wchar_p)]

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = 3                            # FO_DELETE
        op.pFrom = "\0".join(paths) + "\0\0"
        op.pTo = None
        op.fFlags = 0x40 | 0x10 | 0x4  # ALLOWUNDO | NOCONFIRMATION | SILENT
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def _trash_one(path):
    try:
        if sys.platform == "darwin":
            try:
                from Foundation import NSFileManager, NSURL
                result = NSFileManager.defaultManager().\
                    trashItemAtURL_resultingItemURL_error_(
                        NSURL.fileURLWithPath_(path), None, None)
                return bool(result[0] if isinstance(result, tuple) else result)
            except ImportError:
                r = subprocess.run(
                    ["osascript", "-e",
                     'tell application "Finder" to delete POSIX file "{}"'
                     .format(path.replace('"', '\\"'))],
                    capture_output=True, timeout=30)
                return r.returncode == 0
        gio = shutil.which("gio")
        if gio:
            return subprocess.run([gio, "trash", path],
                                  capture_output=True).returncode == 0
    except Exception:
        pass
    return False


def _trash_many(paths):
    """Send files to the Recycle Bin / Trash - NEVER a permanent delete.
    Returns (deleted_count, failed_paths)."""
    if os.name == "nt":
        if _win_recycle(paths):
            return len(paths), []
        failed = [p for p in paths if not _win_recycle([p])]
        return len(paths) - len(failed), failed
    failed = [p for p in paths if not _trash_one(p)]
    return len(paths) - len(failed), failed


def parse_exts(text):
    """'pdf, docx' -> ['pdf', 'docx']. Blank or 'All types' -> no filter.
    Dropdown entries like 'pdf (12,430)' work too."""
    text = re.sub(r"\([^)]*\)", "", text or "")
    if "all types" in text.lower():
        return None
    parts = [p.strip().lstrip(".").lower()
             for p in text.replace(";", ",").replace(" ", ",").split(",")]
    parts = [p for p in parts if p]
    return parts or None


def parse_progress(line):
    """'@P seen=1 done=2 ...' -> dict of ints/floats."""
    out = {}
    for token in line[3:].split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        try:
            out[key] = float(value) if "." in value else int(value)
        except ValueError:
            pass
    return out


def fmt_time(mtime):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    except (ValueError, OSError):
        return "?"


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class FindexApp:

    def __init__(self, root, settings):
        self.root = root
        self.cfg = settings
        self.msgs = queue.Queue()
        self.rows = []
        self.search_gen = 0
        self.search_after = None
        self.proc = None
        self.proc_kind = ""
        self.last_index_finished = time.time()
        self.sort_col = None
        self.sort_desc = False
        self._tip_after = None
        self._tip_win = None
        self._setup_retried = False
        self._pending_index = False
        self._last_dark = None
        self.pal = LIGHT_PALETTE
        self._clip = {"paths": [], "move": False}
        self._render_gen = 0
        self._progress_est = 0

        root.title("findex")
        root.geometry(self.cfg.get("geometry") or DEFAULTS["geometry"])
        root.minsize(820, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        import tkinter.font as tkfont
        size = 13 if sys.platform == "darwin" else 10
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        self.search_font = tkfont.Font(
            family=tkfont.nametofont("TkDefaultFont").cget("family"),
            size=size + 3)

        self._build_vars()
        self._build_menu()
        self._build_layout()
        self._native_theme = ttk.Style().theme_use()
        self.apply_theme()

        self.root.after(POLL_MS, self._pump)
        self.root.after(AUTO_CHECK_MS, self._auto_tick)
        self.refresh_stats()
        self.run_search(live=False)      # fill the list on launch
        self.root.after(800, self._auto_setup)
        self.query_entry.focus_set()

    # -- variables ---------------------------------------------------------

    def _build_vars(self):
        c = self.cfg
        self.var_db = tk.StringVar(value=c["db"])
        self.var_query = tk.StringVar()
        self.var_mode = tk.StringVar(value=c.get("mode", "name"))
        self.var_exts = tk.StringVar(value=c.get("exts", ""))
        self.var_exts.trace_add("write", lambda *_: self.on_query_changed())
        self.var_limit = tk.IntVar(value=int(c.get("limit", 0)))
        self.var_status = tk.StringVar(value="Ready")
        self.var_hint = tk.StringVar(value="")
        self.var_workers = tk.IntVar(value=int(c.get("workers", 0)))
        self.var_rebuild = tk.BooleanVar(value=bool(c.get("rebuild", False)))
        self.var_cloud = tk.BooleanVar(value=bool(c.get("include_cloud", False)))
        self.var_ocr = tk.BooleanVar(value=bool(c.get("ocr", False)))
        self.var_theme = tk.StringVar(value=c.get("theme", "system"))
        self.var_auto = tk.BooleanVar(value=bool(c.get("auto_index", False)))
        self.var_auto_mins = tk.IntVar(value=int(c.get("auto_minutes", 60)))
        self.var_auto_next = tk.StringVar(value="")
        self.var_counts = tk.StringVar(value="Idle")
        self.var_stats = tk.StringVar(value="")

        self.var_query.trace_add("write", lambda *_: self.on_query_changed())
        self.var_mode.trace_add("write", lambda *_: self.run_search(live=False))

    # -- menu --------------------------------------------------------------

    def _build_menu(self):
        bar = tk.Menu(self.root)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Choose index database...", command=self.choose_db)
        m.add_command(label="Open index folder", command=self.open_db_folder)
        m.add_separator()
        m.add_command(label="Quit", command=self.on_close)
        bar.add_cascade(label="File", menu=m)

        accel = "Cmd" if sys.platform == "darwin" else "Ctrl"
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Copy files", accelerator=accel + "+C",
                      command=self.copy_files)
        m.add_command(label="Cut files", accelerator=accel + "+X",
                      command=self.cut_files)
        m.add_command(label="Paste into folder...", accelerator=accel + "+V",
                      command=self.paste_files)
        m.add_separator()
        m.add_command(label="Select all", accelerator=accel + "+A",
                      command=self.select_all)
        m.add_command(label="Delete...", accelerator="Del",
                      command=self.delete_files)
        bar.add_cascade(label="Edit", menu=m)

        m = tk.Menu(bar, tearoff=0)
        for label, value in (("Match system", "system"), ("Light", "light"),
                             ("Dark", "dark")):
            m.add_radiobutton(label=label, value=value,
                              variable=self.var_theme,
                              command=self.apply_theme)
        bar.add_cascade(label="Appearance", menu=m)

        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Search syntax", command=self.show_syntax)
        m.add_command(label="About findex", command=self.show_about)
        bar.add_cascade(label="Help", menu=m)

        self.root.config(menu=bar)

    # -- appearance --------------------------------------------------------

    def apply_theme(self):
        """One coherent, high-contrast look drawn entirely from a single
        palette - light or dark, following the system unless chosen."""
        choice = self.var_theme.get()
        dark = choice == "dark" or (choice == "system" and system_dark())
        self._last_dark = dark
        pal = DARK_PALETTE if dark else LIGHT_PALETTE
        self.pal = pal
        style = ttk.Style()
        try:
            style.theme_use("clam")     # the one theme that recolours fully
        except tk.TclError:
            pass
        style.configure(".", background=pal["bg"], foreground=pal["fg"],
                        bordercolor=pal["border"], focuscolor=pal["accent"],
                        lightcolor=pal["bg"], darkcolor=pal["bg"],
                        troughcolor=pal["bg"])
        for cls in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton"):
            style.configure(cls, background=pal["bg"], foreground=pal["fg"])
        for cls in ("TCheckbutton", "TRadiobutton"):
            style.map(cls, background=[("active", pal["bg"])],
                      foreground=[("disabled", pal["hint"])])
        style.configure("TLabelframe", background=pal["bg"],
                        bordercolor=pal["border"], lightcolor=pal["bg"],
                        darkcolor=pal["bg"], padding=10)
        style.configure("TLabelframe.Label", background=pal["bg"],
                        foreground=pal["hint"])
        style.configure("TButton", background=pal["btn"],
                        foreground=pal["fg"], bordercolor=pal["border"],
                        lightcolor=pal["btn"], darkcolor=pal["btn"],
                        padding=(12, 5))
        style.map("TButton",
                  background=[("pressed", pal["btn_hi"]),
                              ("active", pal["btn_hi"])],
                  foreground=[("disabled", pal["hint"])])
        style.configure("Accent.TButton", background=pal["sel"],
                        foreground=pal["sel_fg"], bordercolor=pal["sel"],
                        lightcolor=pal["sel"], darkcolor=pal["sel"],
                        padding=(14, 5))
        style.map("Accent.TButton", background=[("active", pal["accent"]),
                                                ("pressed", pal["accent"])])
        style.configure("TNotebook", background=pal["bg"],
                        bordercolor=pal["border"], tabmargins=(8, 6, 8, 0))
        style.configure("TNotebook.Tab", background=pal["bg"],
                        foreground=pal["hint"], padding=(16, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", pal["field"])],
                  foreground=[("selected", pal["fg"])])
        for cls in ("TEntry", "TSpinbox", "TCombobox", "Search.TEntry"):
            style.configure(cls, fieldbackground=pal["field"],
                            foreground=pal["fg"], insertcolor=pal["fg"],
                            background=pal["btn"], arrowcolor=pal["fg"],
                            bordercolor=pal["border"],
                            lightcolor=pal["field"], darkcolor=pal["field"],
                            padding=4)
            style.map(cls, bordercolor=[("focus", pal["accent"])],
                      lightcolor=[("focus", pal["accent"])],
                      darkcolor=[("focus", pal["accent"])])
        style.configure("Search.TEntry", padding=7)
        style.configure("Treeview", background=pal["field"],
                        foreground=pal["fg"], fieldbackground=pal["field"],
                        bordercolor=pal["border"], rowheight=24)
        style.configure("Treeview.Heading", background=pal["bg"],
                        foreground=pal["hint"], bordercolor=pal["border"],
                        relief="flat", padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", pal["btn"])])
        style.map("Treeview", background=[("selected", pal["sel"])],
                  foreground=[("selected", pal["sel_fg"])])
        style.configure("Vertical.TScrollbar", background=pal["btn"],
                        troughcolor=pal["bg"], bordercolor=pal["bg"],
                        arrowcolor=pal["hint"], lightcolor=pal["btn"],
                        darkcolor=pal["btn"])
        style.configure("TPanedwindow", background=pal["bg"])
        style.configure("Horizontal.TProgressbar", background=pal["sel"],
                        troughcolor=pal["btn"], bordercolor=pal["border"],
                        lightcolor=pal["sel"], darkcolor=pal["sel"])
        style.configure("Hint.TLabel", background=pal["bg"],
                        foreground=pal["hint"])
        style.configure("Accent.TLabel", background=pal["bg"],
                        foreground=pal["accent"])
        # plain-tk widgets follow the same palette
        self.root.configure(background=pal["bg"])
        self.preview.configure(background=pal["field"], foreground=pal["fg"],
                               insertbackground=pal["fg"],
                               relief="flat", highlightthickness=1,
                               highlightbackground=pal["border"],
                               highlightcolor=pal["border"])
        self.preview.tag_configure("hit", background=pal["hit_bg"],
                                   foreground=pal["hit_fg"])
        self.preview.tag_configure("path", foreground=pal["accent"])
        self.tree.tag_configure("odd", background=pal["row_alt"])
        self.roots_list.configure(background=pal["field"],
                                  foreground=pal["fg"],
                                  selectbackground=pal["sel"],
                                  selectforeground=pal["sel_fg"],
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=pal["border"],
                                  highlightcolor=pal["border"])
        self.log.configure(relief="flat", highlightthickness=1,
                           highlightbackground=pal["border"],
                           highlightcolor=pal["border"])


    # -- tooltips ----------------------------------------------------------

    def tip(self, widget, text, popup=True):
        """Describe a control: status-bar hint on hover, balloon after a pause.

        popup=False gives the status-bar hint only - used for the results list
        and preview, where a balloon following the mouse would be a nuisance.
        """
        widget.bind("<Enter>",
                    lambda e, w=widget: self._tip_enter(w, text, popup), add="+")
        widget.bind("<Leave>", lambda e: self._tip_leave(), add="+")
        widget.bind("<ButtonPress>", lambda e: self._tip_leave(), add="+")

    def _tip_enter(self, widget, text, popup):
        flat = " ".join(text.split())    # one fixed line - no layout jumping
        self.var_hint.set(flat if len(flat) <= 110 else flat[:107] + "...")
        self._tip_cancel()
        if popup:
            self._tip_after = self.root.after(
                600, lambda: self._tip_show(widget, text))

    def _tip_leave(self):
        self.var_hint.set("")
        self._tip_cancel()
        self._tip_hide()

    def _tip_cancel(self):
        if self._tip_after is not None:
            try:
                self.root.after_cancel(self._tip_after)
            except (ValueError, tk.TclError):
                pass
            self._tip_after = None

    def _tip_hide(self):
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None

    def _tip_show(self, widget, text):
        self._tip_after = None
        self._tip_hide()
        try:
            if not widget.winfo_viewable():
                return
            x = widget.winfo_rootx() + 14
            y = widget.winfo_rooty() + widget.winfo_height() + 8
        except tk.TclError:
            return
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        try:    # stops macOS animating/shadowing the balloon
            win.tk.call("::tk::unsupported::MacWindowStyle", "style",
                        win._w, "help", "noActivates")
        except tk.TclError:
            pass
        tk.Label(win, text=text, justify="left",
                 background=self.pal["tip_bg"], foreground=self.pal["tip_fg"],
                 relief="solid", borderwidth=1,
                 wraplength=380, padx=8, pady=5).pack()
        win.update_idletasks()
        screen_w = win.winfo_screenwidth()
        if x + win.winfo_width() > screen_w - 8:
            x = max(8, screen_w - win.winfo_width() - 8)
        win.wm_geometry("+{}+{}".format(x, y))
        self._tip_win = win

    # -- layout ------------------------------------------------------------

    def _build_layout(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self.tab_search = ttk.Frame(self.nb)
        self.tab_index = ttk.Frame(self.nb)
        self.nb.add(self.tab_search, text="  Search  ")
        self.nb.add(self.tab_index, text="  Index  ")

        self._build_search_tab()
        self._build_index_tab()

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom", padx=12, pady=8)
        ttk.Label(bar, textvariable=self.var_status).pack(side="left")
        ttk.Label(bar, textvariable=self.var_hint,
                  style="Hint.TLabel").pack(side="left", padx=14)
        self.busy = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.busy.pack(side="right")

    def _build_search_tab(self):
        top = ttk.Frame(self.tab_search)
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.query_entry = ttk.Entry(top, textvariable=self.var_query,
                                     style="Search.TEntry",
                                     font=self.search_font)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda e: self.run_search(live=False))
        self.query_entry.bind("<Escape>", lambda e: self.var_query.set(""))
        self.query_entry.bind("<Down>", self._focus_results)
        self.tip(self.query_entry,
                 "What to look for. Esc clears the box; the down arrow jumps "
                 "into the results list.", popup=False)

        btn = ttk.Button(top, text="Search", width=10,
                         style="Accent.TButton",
                         command=lambda: self.run_search(live=False))
        btn.pack(side="left", padx=(8, 0))
        self.tip(btn, "Run the search now - the same as pressing Enter.")

        opts = ttk.Frame(self.tab_search)
        opts.pack(fill="x", padx=12, pady=(0, 4))

        rb = ttk.Radiobutton(opts, text="Filename", value="name",
                             variable=self.var_mode)
        rb.pack(side="left")
        self.tip(rb, "Search file names only. Results appear as you type. "
                     "Use * and ? as wildcards, e.g. *budget*2024*.pdf")

        rb = ttk.Radiobutton(opts, text="File contents", value="content",
                             variable=self.var_mode)
        rb.pack(side="left", padx=(10, 20))
        self.tip(rb, "Search the text inside PDFs, Word, Excel, PowerPoint "
                     "and plain-text files - results appear as you type. See "
                     "Help > Search syntax for AND / OR / \"phrases\".")

        lbl = ttk.Label(opts, text="Type:")
        lbl.pack(side="left")
        self.type_box = ttk.Combobox(opts, textvariable=self.var_exts,
                                     width=16, height=25,
                                     values=["All types"])
        self.type_box.pack(side="left", padx=4)
        for w in (lbl, self.type_box):
            self.tip(w, "Every file type actually in your index, with counts. "
                        "Pick one, or type your own list like: pdf, docx, "
                        "xlsx. 'All types' or an empty box means everything.")

        lbl = ttk.Label(opts, text="Max results:")
        lbl.pack(side="left", padx=(20, 4))
        spin = Spinbox(opts, from_=0, to=1000000, increment=100, width=7,
                       textvariable=self.var_limit)
        spin.pack(side="left")
        for w in (lbl, spin):
            self.tip(w, "How many results to list. 0 means ALL of them - "
                        "the full list streams in behind the first screenful. "
                        "Set a number to cap very broad searches.")

        body = ttk.Frame(self.tab_search)
        body.pack(fill="both", expand=True, padx=12, pady=(8, 10))

        holder = ttk.Frame(body)
        cols = ("name", "size", "modified", "folder")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings",
                                 selectmode="extended")
        headings = (("name", "Name", 320), ("size", "Size", 80),
                    ("modified", "Modified", 130), ("folder", "Folder", 460))
        for key, text, width in headings:
            self.tree.heading(key, text=text,
                              command=lambda k=key: self.sort_by(k))
            anchor = "e" if key == "size" else "w"
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "folder"))
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("odd", background="#f4f6f8")
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_preview())
        self.tree.bind("<Button-3>", self.popup_menu)
        self.tree.bind("<Button-2>", self.popup_menu)      # mac right-click
        self.tree.bind("<Control-Button-1>", self.popup_menu)
        for seq, fn in (("<Control-c>", self.copy_files),
                        ("<Control-x>", self.cut_files),
                        ("<Control-v>", self.paste_files),
                        ("<Command-c>", self.copy_files),
                        ("<Command-x>", self.cut_files),
                        ("<Command-v>", self.paste_files),
                        ("<Control-a>", self.select_all),
                        ("<Command-a>", self.select_all),
                        ("<Delete>", self.delete_files),
                        ("<BackSpace>", self.delete_files)):
            try:
                self.tree.bind(seq, lambda e, f=fn: (f(), "break")[1])
            except tk.TclError:
                pass
        self.tip(self.tree,
                 "Works like a file manager: Ctrl/Cmd-click selects several "
                 "files, Ctrl/Cmd+C copies them for pasting into Explorer or "
                 "Finder, Ctrl/Cmd+X cuts, Ctrl/Cmd+V pastes into a folder "
                 "you pick, and Delete sends to the Recycle Bin. Right-click "
                 "for the menu; double-click opens.", popup=False)
        prev = ttk.Frame(body)
        prev.pack(side="bottom", fill="x", pady=(6, 0))
        holder.pack(side="top", fill="both", expand=True)

        self.preview = tk.Text(prev, height=2, wrap="word", relief="flat",
                               background="#ffffff", padx=8, pady=6)
        pvsb = ttk.Scrollbar(prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pvsb.set, state="disabled")
        self.preview.pack(side="left", fill="x", expand=True)
        pvsb.pack(side="right", fill="y")
        self.preview.tag_configure("hit", background="#ffe680")
        self.preview.tag_configure("path", foreground="#20558a")
        self.tip(self.preview,
                 "The full path of the selected file, and in contents mode the "
                 "matching text with your search terms highlighted.",
                 popup=False)

        self.ctx = tk.Menu(self.root, tearoff=0)
        self.ctx.add_command(label="Open file", command=self.open_selected)
        self.ctx.add_command(label="Show in folder", command=self.reveal_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Copy", command=self.copy_files)
        self.ctx.add_command(label="Cut", command=self.cut_files)
        self.ctx.add_command(label="Paste into folder...",
                             command=self.paste_files)
        self.ctx.add_separator()
        self.ctx.add_command(label="Copy full path", command=self.copy_selected)
        self.ctx.add_command(label="Select all", command=self.select_all)
        self.ctx.add_separator()
        self.ctx.add_command(label="Delete...", command=self.delete_files)

    def _build_index_tab(self):
        left = ttk.LabelFrame(self.tab_index, text="Folders to index")
        left.pack(fill="x", padx=8, pady=8)

        row = ttk.Frame(left)
        row.pack(fill="x", padx=8, pady=8)
        self.roots_list = tk.Listbox(row, height=6, activestyle="none")
        rsb = ttk.Scrollbar(row, orient="vertical", command=self.roots_list.yview)
        self.roots_list.configure(yscrollcommand=rsb.set)
        self.roots_list.pack(side="left", fill="both", expand=True)
        rsb.pack(side="left", fill="y")
        for r in self.cfg.get("roots", []):
            self.roots_list.insert("end", r)
        self.tip(self.roots_list,
                 "Everything under these folders gets indexed. The list is "
                 "remembered, so you set it once.", popup=False)

        btns = ttk.Frame(row)
        btns.pack(side="left", padx=8)
        b = ttk.Button(btns, text="Add folder...", width=14, command=self.add_root)
        b.pack(pady=2)
        self.tip(b, "Choose a folder - or a whole drive such as D:\\ - to add "
                    "to the list.")
        b = ttk.Button(btns, text="Remove", width=14, command=self.remove_root)
        b.pack(pady=2)
        self.tip(b, "Take the selected folder off the list. Files already in "
                    "the index stay searchable until the next indexing run "
                    "clears them out.")
        b = ttk.Button(btns, text="Clear all", width=14, command=self.clear_roots)
        b.pack(pady=2)
        self.tip(b, "Empty the folder list. Nothing is deleted from the index "
                    "or from your disk.")

        opts = ttk.LabelFrame(self.tab_index, text="Options")
        opts.pack(fill="x", padx=8)
        line = ttk.Frame(opts)
        line.pack(fill="x", padx=8, pady=8)

        lbl = ttk.Label(line, text="Worker processes:")
        lbl.pack(side="left")
        spin = Spinbox(line, from_=0, to=64, width=5,
                       textvariable=self.var_workers)
        spin.pack(side="left", padx=(4, 4))
        note = ttk.Label(line, text="(0 = one per CPU core)",
                         style="Hint.TLabel")
        note.pack(side="left")
        for w in (lbl, spin, note):
            self.tip(w, "How many files are read at the same time. 0 uses every "
                        "CPU core, which is fastest; set it to 2 or 4 if you "
                        "want to keep using the PC while it indexes.")

        chk = ttk.Checkbutton(line, text="Re-extract everything",
                              variable=self.var_rebuild)
        chk.pack(side="left", padx=(20, 0))
        self.tip(chk, "Read every file again, even ones that have not changed. "
                      "Slow - normally findex only touches new and modified "
                      "files, which is why repeat runs are quick.")

        chk = ttk.Checkbutton(line, text="Include OneDrive online-only files",
                              variable=self.var_cloud)
        chk.pack(side="left", padx=(20, 0))
        self.tip(chk, "Also index OneDrive files that are not downloaded yet. "
                      "Reading them forces a download, so this can take a long "
                      "time and fill disk space. Off is the safe default.")

        ocr_row = ttk.Frame(opts)
        ocr_row.pack(fill="x", padx=8, pady=(0, 8))
        chk = ttk.Checkbutton(ocr_row,
                              text="OCR scanned PDFs (slower)",
                              variable=self.var_ocr)
        chk.pack(side="left")
        self.tip(chk, "For PDFs that are pictures of pages rather than text: "
                      "read up to 20 pages with the OCR engine built into "
                      "Windows / macOS, so scans become searchable. Much "
                      "slower - leave off for huge image collections.")

        auto = ttk.Frame(opts)
        auto.pack(fill="x", padx=8, pady=(0, 8))
        chk = ttk.Checkbutton(auto, text="Auto re-index every",
                              variable=self.var_auto,
                              command=self.update_auto_label)
        chk.pack(side="left")
        self.tip(chk, "Keep the index current on its own: while this window is "
                      "open, findex re-runs the folders above on a timer. It "
                      "waits if an index is already running.")
        spin = Spinbox(auto, from_=5, to=1440, increment=5, width=6,
                       textvariable=self.var_auto_mins,
                       command=self.update_auto_label)
        spin.pack(side="left", padx=4)
        self.tip(spin, "Minutes between automatic runs. 60 is a sensible "
                       "starting point; re-runs only read what changed.")
        ttk.Label(auto, text="minutes while the app is open").pack(side="left")
        ttk.Label(auto, textvariable=self.var_auto_next,
                  style="Accent.TLabel").pack(side="left", padx=12)

        run = ttk.Frame(self.tab_index)
        run.pack(fill="x", padx=8, pady=8)
        self.btn_start = ttk.Button(run, text="Start indexing", width=16,
                                    command=self.start_index)
        self.btn_start.pack(side="left")
        self.tip(self.btn_start,
                 "Scan the folders above and update the search index. Only new "
                 "and changed files are read, and files that have been deleted "
                 "are dropped. You can carry on searching while it runs.")
        self.btn_stop = ttk.Button(run, text="Stop", width=10,
                                   command=self.stop_index, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.tip(self.btn_stop,
                 "Cancel the run in progress. Everything indexed up to that "
                 "point is kept - the next run picks up where this one stopped.")
        self.bar = ttk.Progressbar(run, mode="indeterminate", length=220)
        self.bar.pack(side="left", padx=12)
        counts = ttk.Label(run, textvariable=self.var_counts)
        counts.pack(side="left")
        self.tip(counts,
                 "Live totals. \"Unchanged\" are files already in the index "
                 "that were left alone - on a repeat run that should be nearly "
                 "all of them, which is why it finishes quickly.",
                 popup=False)

        logf = ttk.LabelFrame(self.tab_index, text="Output")
        logf.pack(fill="both", expand=True, padx=8)
        self.log = tk.Text(logf, height=10, wrap="none", relief="flat",
                           background="#111418", foreground="#d6dde5",
                           insertbackground="#d6dde5", padx=8, pady=6)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.tip(self.log, "Messages from the indexer, including any files it "
                           "could not read.", popup=False)

        foot = ttk.Frame(self.tab_index)
        foot.pack(fill="x", padx=8, pady=8)
        stats = ttk.Label(foot, textvariable=self.var_stats)
        stats.pack(side="left")
        self.tip(stats, "What the index currently holds: number of files, how "
                        "much text was extracted, and the size of the database "
                        "file on disk.", popup=False)
        b = ttk.Button(foot, text="Optimise + compact", command=self.run_vacuum)
        b.pack(side="right")
        self.tip(b, "Merge the search index and shrink the database file. "
                    "Makes searches a little faster and reclaims disk space - "
                    "worth running after a big indexing job.")
        b = ttk.Button(foot, text="Refresh", command=self.refresh_stats)
        b.pack(side="right", padx=6)
        self.tip(b, "Re-read the figures shown on the left.")
        b = ttk.Button(foot, text="Clear index...",
                       command=self.clear_index_ui)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Start fresh: delete findex's entire database - every "
                    "recorded file name and all extracted text. Your actual "
                    "files on disk are never touched. Asks before doing "
                    "anything.")

        self.update_auto_label()

    # -- searching ---------------------------------------------------------

    def on_query_changed(self):
        if self.search_after:
            self.root.after_cancel(self.search_after)
            self.search_after = None
        self.search_after = self.root.after(LIVE_SEARCH_MS,
                                            lambda: self.run_search(live=True))

    def run_search(self, live=False):
        text = self.var_query.get().strip()
        self.search_gen += 1
        gen = self.search_gen
        mode = self.var_mode.get()
        db = self.var_db.get()
        try:
            limit = max(0, int(self.var_limit.get()))
        except (tk.TclError, ValueError):
            limit = 0
        exts = parse_exts(self.var_exts.get())
        self.var_status.set("Searching...")
        threading.Thread(target=self._search_worker,
                         args=(gen, db, mode, text, limit, exts, live),
                         daemon=True).start()

    def _search_worker(self, gen, db, mode, text, limit, exts,
                       live=False):
        conn = None
        try:
            try:
                conn = findex.open_db_ro(db)
            except sqlite3.Error:
                conn = findex.open_db(db)   # index file not created yet
            if not text:
                # Empty box = browse the whole index, newest first, capped so
                # the list stays instant. Typing narrows it down.
                total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                raw = findex.name_rows(conn, "*", limit, exts)
                rows = [{"path": r[0], "name": os.path.basename(r[0]),
                         "size": r[1], "mtime": r[2], "snippet": ""}
                        for r in raw]
                self.msgs.put(("results", gen, rows, total))
            elif mode == "content":
                attempt = text
                if live and text and (text[-1].isalnum() or text[-1] == "_"):
                    attempt = text + "*"    # the word being typed matches
                                            # as a prefix while you type
                try:
                    raw = findex.search_rows(conn, attempt, limit, exts,
                                             snippet_len=18)
                except sqlite3.OperationalError:
                    # mid-typing syntax (an unclosed quote, a lone AND):
                    # search the real words literally as prefixes, ignoring
                    # operators and stray punctuation
                    words = [w for w in re.findall(r"\w+", text)
                             if w.upper() not in ("AND", "OR", "NOT", "NEAR")]
                    safe = " ".join('"{}"*'.format(w) for w in words)
                    if not safe:
                        raise
                    raw = findex.search_rows(conn, safe, limit, exts,
                                             snippet_len=18)
                rows = [{"path": r[0], "name": os.path.basename(r[0]),
                         "size": r[1], "mtime": r[2], "snippet": r[3]}
                        for r in raw]
            else:
                raw = findex.name_rows(conn, text, limit, exts)
                rows = [{"path": r[0], "name": os.path.basename(r[0]),
                         "size": r[1], "mtime": r[2], "snippet": ""}
                        for r in raw]
            self.msgs.put(("results", gen, rows, None))
        except sqlite3.OperationalError as exc:
            self.msgs.put(("search_error", gen, str(exc)))
        except Exception as exc:                               # noqa: BLE001
            self.msgs.put(("search_error", gen,
                           "{}: {}".format(type(exc).__name__, exc)))
        finally:
            if conn is not None:
                conn.close()

    def render_rows(self):
        """Show results without ever freezing the window: the first screenful
        appears at once, the rest streams in between keystrokes, and a newer
        search abandons the old stream mid-way."""
        self._render_gen += 1
        self.tree.delete(*self.tree.get_children())
        self.set_preview("")
        self._render_chunk(self._render_gen, 0)

    def _render_chunk(self, gen, start):
        if gen != self._render_gen:
            return                       # superseded by a newer result set
        end = min(start + (300 if start == 0 else 800), len(self.rows))
        insert = self.tree.insert
        human = findex.human
        for i in range(start, end):
            row = self.rows[i]
            insert("", "end", iid=str(i),
                   tags=("odd",) if i % 2 else (),
                   values=(row["name"], human(row["size"]),
                           fmt_time(row["mtime"]),
                           os.path.dirname(row["path"])))
        if end < len(self.rows):
            self.root.after(5, lambda: self._render_chunk(gen, end))

    def sort_by(self, col):
        if not self.rows:
            return
        self.sort_desc = not self.sort_desc if self.sort_col == col else False
        self.sort_col = col
        key = {"name": lambda r: r["name"].lower(),
               "size": lambda r: r["size"] or 0,
               "modified": lambda r: r["mtime"] or 0,
               "folder": lambda r: os.path.dirname(r["path"]).lower()}[col]
        self.rows.sort(key=key, reverse=self.sort_desc)
        self.render_rows()

    def selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.rows[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def show_preview(self):
        row = self.selected_row()
        if not row:
            return
        self.set_preview(row["snippet"], row["path"])

    def set_preview(self, snippet, path=""):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if path:
            self.preview.insert("end", path + "\n\n", ("path",))
        if snippet:
            for i, part in enumerate(snippet.replace("<<", ">>").split(">>")):
                self.preview.insert("end", part, ("hit",) if i % 2 else ())
        elif not path:
            self.preview.insert("end",
                                "The list shows your indexed files, newest "
                                "first. Both search modes narrow it live as "
                                "you type.")
        self.preview.configure(state="disabled")
        # grow or shrink with the content: a short path takes one line, a long
        # path or a text snippet takes more, capped so the list keeps the room
        try:
            shown = int(self.preview.count("1.0", "end-1c",
                                           "displaylines")[0])
        except (tk.TclError, TypeError, IndexError):
            shown = 2
        self.preview.configure(height=max(2, min(8, shown)))

    def _focus_results(self, _event):
        kids = self.tree.get_children()
        if kids:
            self.tree.focus_set()
            self.tree.selection_set(kids[0])
            self.tree.focus(kids[0])
        return "break"

    def selected_rows(self):
        out = []
        for iid in self.tree.selection():
            try:
                out.append(self.rows[int(iid)])
            except (ValueError, IndexError):
                pass
        return out

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def copy_files(self, move=False):
        """Put the selected files on the clipboard - the real files, so they
        can be pasted straight into Explorer or Finder."""
        rows = self.selected_rows()
        if not rows:
            return
        paths = [r["path"] for r in rows]
        self._clip = {"paths": paths, "move": move}
        on_system = False
        if os.name == "nt":
            on_system = _win_set_file_clipboard(paths, move)
        elif sys.platform == "darwin" and not move:
            on_system = _mac_set_file_clipboard(paths)
        if not on_system:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))
        self.var_status.set("{} {:,} file(s){}".format(
            "Cut" if move else "Copied", len(paths),
            " - paste in Explorer/Finder or here" if on_system
            else " (paths on clipboard; paste here works too)"))

    def cut_files(self):
        self.copy_files(move=True)

    def paste_files(self):
        """Copy or move the clipboard's files into a folder you choose.
        Accepts files copied inside findex OR in Explorer/Finder."""
        paths = list(self._clip["paths"])
        move = self._clip["move"]
        if not paths:
            paths = (_win_get_file_clipboard() if os.name == "nt"
                     else _mac_get_file_clipboard())
            move = False
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            self.var_status.set("Nothing to paste")
            return
        sel = self.selected_rows()
        dest = filedialog.askdirectory(
            title="Paste {:,} file(s) into...".format(len(paths)),
            initialdir=os.path.dirname(sel[0]["path"]) if sel else HERE)
        if not dest:
            return
        done, moved_from = 0, []
        for p in paths:
            try:
                base, ext = os.path.splitext(os.path.basename(p))
                target = os.path.join(dest, base + ext)
                n = 2
                while os.path.exists(target):
                    target = os.path.join(dest,
                                          "{} ({}){}".format(base, n, ext))
                    n += 1
                if move:
                    shutil.move(p, target)
                    moved_from.append(p)
                else:
                    shutil.copy2(p, target)
                done += 1
            except Exception as exc:                           # noqa: BLE001
                self.log_line("paste failed: {}: {}".format(p, exc))
        if move:
            self._db_forget(moved_from)
            self._clip = {"paths": [], "move": False}
        self.var_status.set("{} {:,} file(s) into {}".format(
            "Moved" if move else "Copied", done, dest))
        self.run_search(live=False)

    def delete_files(self):
        """Send the selected files to the Recycle Bin / Trash (never a
        permanent delete), and drop them from the index immediately."""
        rows = self.selected_rows()
        if not rows:
            return
        listed = "\n".join("    " + r["name"] for r in rows[:8])
        if len(rows) > 8:
            listed += "\n    ...and {:,} more".format(len(rows) - 8)
        bin_name = "Recycle Bin" if os.name == "nt" else "Bin"
        if not messagebox.askyesno(
                "Delete {:,} file(s)?".format(len(rows)),
                "Move to the {} (recoverable from there):\n\n{}".format(
                    bin_name, listed)):
            return
        paths = [r["path"] for r in rows]
        done, failed = _trash_many(paths)
        self._db_forget([p for p in paths if p not in failed])
        for p in failed[:5]:
            self.log_line("could not delete: " + p)
        self.var_status.set("Sent {:,} file(s) to the {}{}".format(
            done, bin_name,
            " - {:,} failed (see Output)".format(len(failed))
            if failed else ""))
        self.run_search(live=False)

    def _db_forget(self, paths):
        """Drop rows for files that no longer exist at their old path, so the
        list is right immediately (the next index run would fix it anyway)."""
        if not paths:
            return
        try:
            conn = sqlite3.connect(self.var_db.get(), timeout=3)
            cur = conn.cursor()
            for p in paths:
                row = cur.execute("SELECT id FROM files WHERE path=?",
                                  (p,)).fetchone()
                if row:
                    cur.execute("DELETE FROM docs WHERE rowid=?", (row[0],))
                    cur.execute("DELETE FROM files WHERE id=?", (row[0],))
            conn.commit()
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass

    def popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.ctx.tk_popup(event.x_root, event.y_root)

    def open_selected(self):
        row = self.selected_row()
        if row:
            open_path(row["path"])

    def reveal_selected(self):
        row = self.selected_row()
        if row:
            reveal_path(row["path"])

    def copy_selected(self):
        rows = self.selected_rows()
        if rows:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(r["path"] for r in rows))
            self.var_status.set("Path copied" if len(rows) == 1
                                else "{:,} paths copied".format(len(rows)))

    # -- indexing ----------------------------------------------------------

    def current_roots(self):
        return list(self.roots_list.get(0, "end"))

    def add_root(self):
        folder = filedialog.askdirectory(title="Choose a folder or drive to index")
        if folder:
            folder = os.path.abspath(folder)
            if folder not in self.current_roots():
                self.roots_list.insert("end", folder)

    def remove_root(self):
        for i in reversed(self.roots_list.curselection()):
            self.roots_list.delete(i)

    def clear_roots(self):
        self.roots_list.delete(0, "end")

    def start_index(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        roots = self.current_roots()
        if not roots:
            messagebox.showwarning("No folders",
                                   "Add at least one folder to index.")
            return
        if self.var_rebuild.get() and not messagebox.askyesno(
                "Re-extract everything?",
                "This ignores what is already indexed and reads every file "
                "again, which can take hours on a large drive.\n\n"
                "You normally do not need it. findex keeps its index between "
                "runs and only reads files that are new or have changed.\n\n"
                "Carry on with a full re-extract?"):
            return
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing:
            keep = [r for r in roots if os.path.isdir(r)]
            listed = "\n".join("    " + m for m in missing[:8])
            if not keep:
                messagebox.showwarning(
                    "Folders not found",
                    "None of these folders are on this machine right now:\n\n"
                    + listed + "\n\nNothing to index.")
                return
            if not messagebox.askyesno(
                    "Folders not found",
                    "These folders are not on this machine right now:\n\n"
                    + listed + "\n\nTheir entries in the index will be left "
                    "alone. Index the remaining folders anyway?"):
                return
            roots = keep
        if self.var_ocr.get() and not findex.have_ocr_backend():
            if self._offer_tesseract():
                return    # tesseract is installing; indexing follows by itself
            self.log_line("-- tesseract not installed: OCR will be skipped "
                          "this run --")
        self._progress_est = 0
        try:                # last known file count = a solid progress estimate
            conn = findex.open_db_ro(self.var_db.get())
            self._progress_est = conn.execute(
                "SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
        cmd = [child_python(), "-u", FINDEX_PY, "--db", self.var_db.get(),
               "index"] + roots + ["--progress"]
        if self.var_rebuild.get():
            cmd.append("--rebuild")
        if self.var_cloud.get():
            cmd.append("--include-cloud")
        if self.var_ocr.get():
            cmd.append("--ocr")
        workers = int(self.var_workers.get() or 0)
        if workers > 0:
            cmd += ["--workers", str(workers)]
        self.launch(cmd, "index", "Indexing...")

    def _auto_setup(self):
        """Install any missing Python components into the app's own
        environment, automatically, with progress in the Output pane."""
        pkgs = missing_packages()
        if not pkgs or self.proc is not None:
            return
        self.log_line("-- setup: installing " + ", ".join(pkgs) + " --")
        cmd = [child_python(), "-m", "pip", "install",
               "--only-binary", ":all:"] + pkgs
        self.launch(cmd, "setup",
                    "Installing components ({})...".format(", ".join(pkgs)))

    def _offer_tesseract(self):
        """Offer to install tesseract via the system package manager.
        Returns True when an install has started - indexing resumes after."""
        if os.name == "nt":
            manager = "winget"
            found = shutil.which("winget")
            cmd = [found, "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
                   "--accept-source-agreements",
                   "--accept-package-agreements"] if found else None
        else:
            manager = "Homebrew"
            found = (shutil.which("brew")
                     or next((p for p in ("/opt/homebrew/bin/brew",
                                          "/usr/local/bin/brew")
                              if os.path.exists(p)), None))
            cmd = [found, "install", "tesseract"] if found else None
        if cmd is None:
            messagebox.showinfo(
                "Tesseract needed",
                "OCR needs the tesseract program, and {} was not found to "
                "install it with.\n\nInstall tesseract manually, or untick "
                "OCR.".format(manager))
            return False
        if not messagebox.askyesno(
                "Install tesseract?",
                "OCR needs the tesseract program, which is not installed.\n\n"
                "Install it now with {}? The index run will start by itself "
                "once the install finishes.".format(manager)):
            return False
        self._pending_index = True
        self.launch(cmd, "setup", "Installing tesseract...")
        return True

    def _handle_setup_end(self, code):
        """After a setup run ends: outside a venv, retry pip once with the
        flags newer system Pythons demand; then resume any index run that was
        waiting on the install. Returns True when another run has started."""
        if code != 0 and not self._setup_retried and not in_venv():
            self._setup_retried = True
            pkgs = missing_packages()
            if pkgs:
                self.log_line("-- retrying with --user "
                              "--break-system-packages --")
                self.proc_kind = ""
                self.launch([child_python(), "-m", "pip", "install",
                             "--only-binary", ":all:", "--user",
                             "--break-system-packages"] + pkgs,
                            "setup", "Installing components (second try)...")
                return True
        if code == 0 and self._pending_index:
            self._pending_index = False
            self.proc_kind = ""
            self.log_line("-- setup finished - starting the index run --")
            self.start_index()
            return True
        if code != 0:
            self._pending_index = False
        return False

    def clear_index_ui(self):
        """Wipe the index and start fresh. Files on disk are untouched."""
        if self.proc is not None:
            messagebox.showinfo("Busy", "Stop the current run first.")
            return
        total = 0
        try:
            conn = findex.open_db_ro(self.var_db.get())
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
        if not messagebox.askyesno(
                "Clear the index?",
                "Delete the entire index ({:,} files recorded)?\n\n"
                "Only findex's own database is deleted - the files on your "
                "disk are untouched. The next indexing run starts from "
                "scratch.".format(total)):
            return
        try:
            result = findex.clear_index(self.var_db.get())
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not clear", str(exc))
            return
        self.log_line("-- " + result + " --")
        self.var_status.set("Index cleared - ready to start fresh")
        self.refresh_stats()
        self.run_search(live=False)

    def run_vacuum(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        cmd = [child_python(), "-u", FINDEX_PY, "--db", self.var_db.get(),
               "vacuum"]
        self.launch(cmd, "vacuum", "Optimising the index...")

    def launch(self, cmd, kind, status):
        self.log_line("$ " + " ".join(cmd))
        try:
            kwargs = no_window()
            if os.name != "nt":
                kwargs["start_new_session"] = True   # own process group
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=HERE, **kwargs)
        except Exception as exc:                               # noqa: BLE001
            self.proc = None
            messagebox.showerror("Could not start", str(exc))
            return
        self.proc_kind = kind
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.bar.start(12)
        self.busy.start(12)
        self.var_status.set(status)
        self.var_counts.set("starting...")
        threading.Thread(target=self._reader, args=(self.proc,),
                         daemon=True).start()

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("@P "):
                    self.msgs.put(("progress", parse_progress(line)))
                elif line.strip():
                    self.msgs.put(("log", line))
        except Exception:                                      # noqa: BLE001
            pass
        finally:
            code = proc.wait()
            self.msgs.put(("done", code))

    def stop_index(self):
        if self.proc is None:
            return
        self.log_line("-- stopping: ending the run and all of its worker "
                      "processes --")
        self._kill_tree()
        self.root.after(3000, self._ensure_stopped)

    def _kill_tree(self):
        """End the background run AND every worker process it started.
        Terminating only the parent left the workers running - which is why
        Stop used to say 'stopping' and nothing happened."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid),
                                "/T", "/F"],
                               capture_output=True, **no_window())
            else:
                import signal
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        except Exception:                                      # noqa: BLE001
            try:
                proc.terminate()
            except Exception:                                  # noqa: BLE001
                pass

    def _ensure_stopped(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        self.log_line("-- still running: force-killing the worker tree --")
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid),
                                "/T", "/F"],
                               capture_output=True, **no_window())
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:                                      # noqa: BLE001
            pass

    # -- queue pump --------------------------------------------------------

    def _pump(self):
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "results":
                    _, gen, rows, total = msg
                    if gen == self.search_gen:
                        self.rows = rows
                        self.sort_col = None
                        self.render_rows()
                        if total is None:
                            self.var_status.set(
                                "{:,} result(s)".format(len(rows)))
                        elif total > len(rows):
                            self.var_status.set(
                                "Showing the {:,} most recent of {:,} indexed "
                                "files - type to narrow".format(len(rows), total))
                        else:
                            self.var_status.set(
                                "All {:,} indexed files - type to narrow"
                                .format(total))
                elif kind == "search_error":
                    _, gen, err = msg
                    if gen == self.search_gen:
                        self.rows = []
                        self.render_rows()
                        self.var_status.set("Query error: " + err)
                elif kind == "log":
                    self.log_line(msg[1])
                elif kind == "progress":
                    p = msg[1]
                    pct_txt = ""
                    est = self._progress_est
                    if est > 0:
                        if str(self.bar.cget("mode")) != "determinate":
                            self.bar.stop()
                            self.bar.configure(mode="determinate",
                                               maximum=100)
                        pct = min(99.0, 100.0 * p.get("seen", 0) / est)
                        self.bar.configure(value=pct)
                        pct_txt = "{:.0f}%  |  ".format(pct)
                    self.var_counts.set(
                        pct_txt +
                        "{:,} seen | {:,} unchanged | {:,} updated | "
                        "{:,} with text | {:,} errors | {:.0f}s".format(
                            p.get("seen", 0), p.get("unchanged", 0),
                            p.get("done", 0), p.get("ok", 0),
                            p.get("error", 0), p.get("elapsed", 0)))
                elif kind == "done":
                    self._finish(msg[1])
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._pump)

    def _finish(self, code):
        self.proc = None
        self.bar.stop()
        self.bar.configure(mode="indeterminate", value=0)
        self._progress_est = 0
        self.busy.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if self.proc_kind == "index":
            self.last_index_finished = time.time()
            self.update_auto_label()
        if self.proc_kind == "setup" and self._handle_setup_end(code):
            return
        word = "finished" if code == 0 else "stopped (exit {})".format(code)
        self.var_status.set("{} {}".format(self.proc_kind.title(), word))
        self.log_line("-- {} {} --\n".format(self.proc_kind, word))
        self.proc_kind = ""
        self.refresh_stats()
        self.run_search(live=False)      # refresh the visible list

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", "{}.0".format(lines - MAX_LOG_LINES))
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- auto re-index -----------------------------------------------------

    def update_auto_label(self):
        if not self.var_auto.get():
            self.var_auto_next.set("")
            return
        mins = max(5, int(self.var_auto_mins.get() or 60))
        due = self.last_index_finished + mins * 60
        self.var_auto_next.set("next run " + time.strftime("%H:%M",
                                                           time.localtime(due)))

    def _auto_tick(self):
        try:
            if (self.var_auto.get() and self.proc is None
                    and self.current_roots()):
                mins = max(5, int(self.var_auto_mins.get() or 60))
                if time.time() - self.last_index_finished >= mins * 60:
                    self.log_line("-- auto re-index --")
                    self.start_index()
            if (self.var_theme.get() == "system"
                    and system_dark() != self._last_dark):
                self.apply_theme()          # the OS switched mode
            self.update_auto_label()
        except Exception:                                      # noqa: BLE001
            pass
        self.root.after(AUTO_CHECK_MS, self._auto_tick)

    # -- stats + database --------------------------------------------------

    def refresh_stats(self):
        db = self.var_db.get()
        try:
            try:
                conn = findex.open_db_ro(db)
            except sqlite3.Error:
                conn = findex.open_db(db)   # index file not created yet
            total, chars = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(chars),0) FROM files").fetchone()
            errs = conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
            last = findex.get_meta(conn, "last_index")
            summary = findex.get_meta(conn, "last_summary")
            kinds = conn.execute(
                "SELECT ext, COUNT(*) FROM files "
                "WHERE ext IS NOT NULL AND ext != '' "
                "GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 500").fetchall()
            conn.close()
            self.type_box["values"] = ["All types"] + [
                "{} ({:,})".format(e.lstrip("."), n) for e, n in kinds]
            size = os.path.getsize(db) if os.path.exists(db) else 0
            text = ("{:,} files indexed   |   {} of text   |   database {}   |  "
                    " {:,} errors".format(total, findex.human(chars),
                                          findex.human(size), errs))
            if last:
                try:
                    when = time.strftime("%a %d %b %H:%M",
                                         time.localtime(float(last)))
                    text += "\nLast updated {}".format(when)
                    if summary:
                        text += "   ({})".format(summary)
                except (ValueError, OSError):
                    pass
            self.var_stats.set(text)
        except Exception as exc:                               # noqa: BLE001
            self.var_stats.set("Index not readable: {}".format(exc))

    def choose_db(self):
        path = filedialog.asksaveasfilename(
            title="Index database", initialdir=HERE,
            initialfile=os.path.basename(self.var_db.get()),
            defaultextension=".db",
            filetypes=[("findex index", "*.db"), ("All files", "*.*")],
            confirmoverwrite=False)
        if path:
            self.var_db.set(path)
            self.refresh_stats()
            self.run_search(live=False)

    def open_db_folder(self):
        folder = os.path.dirname(os.path.abspath(self.var_db.get())) or HERE
        open_path(folder)

    def show_syntax(self):
        messagebox.showinfo(
            "Search syntax",
            "Filename mode\n"
            "  budget          matches anywhere in the name\n"
            "  *2024*.pdf      * and ? wildcards\n\n"
            "File contents mode (SQLite FTS5)\n"
            "  invoice payment       both words\n"
            '  "exact phrase"        quoted phrase\n'
            "  quarterly AND revenue\n"
            "  contract NOT draft\n"
            "  budg*                 prefix match\n"
            "  NEAR(risk policy, 10) within 10 words\n\n"
            "Types box: pdf, docx, xlsx, txt  (blank = every indexed type)")

    def show_about(self):
        messagebox.showinfo(
            "findex",
            "findex - local filename and full-text search.\n\n"
            "Index database:\n{}\n\nSettings:\n{}\n\n"
            "Components: {}".format(
                self.var_db.get(), SETTINGS_PATH,
                "all installed" if not missing_packages()
                else "still missing " + ", ".join(missing_packages())
                + " (installed automatically on next launch)"))

    # -- shutdown ----------------------------------------------------------

    def on_close(self):
        if self.proc is not None:
            if not messagebox.askyesno(
                    "Still running",
                    "Indexing is still running. Stop it and quit?"):
                return
            self._kill_tree()
        self.cfg.update({
            "db": portable(self.var_db.get()),
            "roots": [portable(r) for r in self.current_roots()],
            "workers": int(self.var_workers.get() or 0),
            "include_cloud": bool(self.var_cloud.get()),
            "ocr": bool(self.var_ocr.get()),
            "theme": self.var_theme.get(),
            "auto_index": bool(self.var_auto.get()),
            "auto_minutes": int(self.var_auto_mins.get() or 60),
            "limit": int(self.var_limit.get() or 200),
            "mode": self.var_mode.get(),
            "exts": self.var_exts.get(),
            "geometry": self.root.geometry(),
        })
        save_settings(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="findex_gui",
                                 description="Desktop front end for findex.")
    ap.add_argument("--db", default=None, help="index database path")
    args = ap.parse_args(argv)

    settings = load_settings()
    if args.db:
        settings["db"] = os.path.abspath(args.db)

    root = tk.Tk()
    try:
        style = ttk.Style()
        for theme in ("vista", "aqua", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=22)
    except tk.TclError:
        pass

    FindexApp(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
