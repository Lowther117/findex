#!/usr/bin/env python3
"""
findex_gui - desktop front end for findex.

Run it:
    Windows   double-click findex-gui.bat
    Any OS    python findex_gui.py        or        python findex.py gui

Search tab  one Everything-style box, live as you type: names by default,
            content:word for text inside files, C:\\ path scopes, ext: filters,
            ! exclusions - plus a duplicate-file finder.
Index tab   pick folders, run an index, watch progress, auto re-index on a
            timer, or turn on live updates (real-time watching).

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

# Where this file sits. Used only to find findex.py alongside it; in a frozen
# build the engine is compiled in, and HERE may be any user folder, so nothing
# is added to sys.path from it.
_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if not getattr(sys, "frozen", False) and _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import findex  # noqa: E402
import theme   # noqa: E402  - the shared light/dark palettes + ttk styling

# One source of truth for "the folder findex owns" - beside the scripts, or
# beside findex.exe / findex.app in a standalone build. See findex._app_dir.
HERE = findex.HERE

SETTINGS_PATH = os.path.join(HERE, "findex_gui.json")
FINDEX_PY = os.path.join(_SCRIPT_DIR, "findex.py")
DEFAULT_DB = os.path.join(HERE, "findex.db")

POLL_MS = 100            # queue drain interval
LIVE_SEARCH_MS = 120     # debounce for search-as-you-type
AUTO_CHECK_MS = 20000    # how often the auto re-index timer is checked
RES_TICK_MS = 2000       # how often the CPU / memory readout refreshes
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
    "dark": True,
    "auto_index": False,
    "auto_minutes": 60,
    "watch": False,
    "limit": 0,
    "exts": "",
    "save_dir": "",
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
            # settings written by the old three-way Appearance menu
            if "dark" not in saved and saved.get("theme") == "light":
                data["dark"] = False
    except (OSError, ValueError):
        pass
    data["dark"] = bool(data.get("dark", True))
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


def save_setting(key, value):
    """Change one key in findex_gui.json right away (the rest is written on
    close), so the engine and the next launch see it too."""
    saved = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        pass
    saved[key] = value
    save_settings(saved)


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


# The Windows built-in OCR engine is reached through these maintained,
# wheel-only packages (the old all-in-one winsdk package stopped shipping
# wheels for new Python versions and tries to compile itself instead).
# Lower bounds for everything the app pip-installs.
#
# These are not fussiness. Asked for a bare "extract-msg", pip walks backwards
# through forty-odd releases looking for one whose unbounded requirements can
# be satisfied, re-checking pymupdf, chardet, beautifulsoup4 and oletools
# against each - and on Python 3.14 gives up with "resolution-too-deep",
# installing nothing. A floor on each package cuts that search dead.
PIP_PINS = {
    "pymupdf": "pymupdf>=1.26",
    "mutagen": "mutagen>=1.47",
    "extract-msg": "extract-msg>=0.54",
    "watchdog": "watchdog>=6.0",
    "psutil": "psutil>=6.0",
    "pyobjc-framework-Vision": "pyobjc-framework-Vision>=10.0",
}


# extract-msg needs red-black-tree-mod, published as a source tarball only.
# --only-binary :all: refuses it and the resolve fails as impossible, so
# sdists are allowed for that one package - pure Python, nothing compiles.
PIP_EXTRA_FLAGS = {
    "extract-msg": ["--no-binary", "red-black-tree-mod"],
}


def pip_specs(names):
    """Package names as pip should be asked for them."""
    return [PIP_PINS.get(n, n) for n in names]


WINRT_PACKAGES = [
    "winrt-runtime",
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
    "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging",
    "winrt-Windows.Media.Ocr",
    "winrt-Windows.Storage.Streams",
]

# Type-dropdown groups: pick "Images" and every extension in the family is
# included. Counts shown against each group come from the index itself.
TYPE_GROUPS = {
    "images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif",
               ".tiff", ".heic", ".heif", ".svg", ".ico", ".raw", ".cr2",
               ".nef", ".arw", ".dng", ".psd"],
    "videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".webm",
               ".flv", ".mpg", ".mpeg", ".ts", ".3gp", ".vob"],
    "audio": [".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus",
              ".wma", ".wav", ".aiff", ".mid", ".midi"],
    "documents": [".pdf", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm",
                  ".ppt", ".pptx", ".pptm", ".odt", ".ods", ".odp", ".rtf",
                  ".txt", ".md", ".epub", ".pages", ".numbers", ".key",
                  ".csv"],
    "compressed": [".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".tar",
                   ".cbz", ".cbr", ".iso", ".dmg"],
    "code": [".py", ".js", ".ts", ".html", ".htm", ".css", ".c", ".h",
             ".cpp", ".cs", ".java", ".sql", ".sh", ".bat", ".cmd", ".ps1",
             ".json", ".xml", ".yml", ".yaml", ".ini", ".cfg", ".lua",
             ".gd"],
    "programs": [".exe", ".msi", ".app", ".dll", ".apk", ".deb", ".pkg"],
    "emails": [".eml", ".msg"],
}

def palette_extras(c):
    """findex's own colours - alternate result rows, the search-hit
    highlight, tooltips - derived from the shared palette so the two modes
    stay one family with the other apps."""
    return {
        "row_alt": c["code"],
        "hit_bg": c["accent"], "hit_fg": c["accent_text"],
        "tip_bg": c["hint"], "tip_fg": c["text"],
    }


try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:                 # the readout simply hides itself
    HAVE_PSUTIL = False


def human_bytes(n):
    """340 MB, 1.2 GB - two significant-ish figures, no clutter."""
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            val = n / size
            return "{:.1f} {}".format(val, unit) if val < 10 \
                else "{:.0f} {}".format(val, unit)
    return "{:.0f} B".format(n)


class ResourceMonitor:
    """CPU and memory for findex AND everything it has started.

    An index run is a child process with a pool of workers under it, and
    those are where the load actually is - a readout for this window alone
    would sit near zero through the very thing worth watching. They are all
    descendants of this process, so the whole tree is counted.

    CPU is reported as a share of the whole machine rather than psutil's
    sum-across-cores, which reads as 780% on a 32-worker run and means
    nothing to anyone.
    """

    def __init__(self):
        self.ok = HAVE_PSUTIL
        self._cores = 1
        self._procs = {}      # pid -> Process, kept between samples so
                              # cpu_percent has a previous reading to use
        if self.ok:
            try:
                self._me = psutil.Process(os.getpid())
                self._cores = psutil.cpu_count() or 1
            except Exception:                              # noqa: BLE001
                self.ok = False

    def sample(self):
        """(cpu_share_percent, rss_bytes, process_count), or None."""
        if not self.ok:
            return None
        try:
            live = [self._me] + self._me.children(recursive=True)
        except Exception:                                  # noqa: BLE001
            return None
        cpu, rss, seen = 0.0, 0, {}
        for proc in live:
            proc = self._procs.get(proc.pid, proc)
            seen[proc.pid] = proc
            try:
                cpu += proc.cpu_percent(None)
                rss += proc.memory_info().rss
            except Exception:                              # noqa: BLE001
                continue          # it exited between being listed and asked
        self._procs = seen
        return min(100.0, cpu / self._cores), rss, len(seen)


def missing_packages():
    """Python components the app wants but this environment lacks."""
    if getattr(sys, "frozen", False):
        return []       # a standalone build ships with everything baked in
    import importlib.util as iu
    missing = []
    if iu.find_spec("pymupdf") is None and iu.find_spec("fitz") is None:
        missing.append("pymupdf")          # PDF text extraction
    if iu.find_spec("mutagen") is None:
        missing.append("mutagen")          # music/video tags
    if iu.find_spec("extract_msg") is None:
        missing.append("extract-msg")      # Outlook .msg emails
    if iu.find_spec("watchdog") is None:
        missing.append("watchdog")         # live index updates
    if iu.find_spec("psutil") is None:
        missing.append("psutil")           # CPU / memory readout
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


PIP_DRIVER = r"""
import subprocess, sys
failed = []
for label, args in PIP_JOBS:
    print("-- pip install " + label, flush=True)
    # One package per call. A single call asking for all of them lets one
    # awkward dependency graph take the whole set down with it.
    if subprocess.call([sys.executable, "-m", "pip", "install",
                        "--only-binary", ":all:"] + PIP_EXTRA + args):
        failed.append(label)
if failed:
    print("-- could not install: " + ", ".join(failed), flush=True)
sys.exit(1 if failed else 0)
"""


def pip_install_cmd(names, extra=None):
    """Install these packages one at a time, in one child process.

    Separate calls matter: pip resolves everything it is given in one go, so
    a package with sloppy version ranges can exhaust the resolver and leave
    the others uninstalled as collateral.
    """
    jobs = [(n, PIP_EXTRA_FLAGS.get(n, []) + [PIP_PINS.get(n, n)])
            for n in names]
    driver = "PIP_JOBS = {!r}\nPIP_EXTRA = {!r}\n".format(
        jobs, list(extra or [])) + PIP_DRIVER
    return [child_python(), "-c", driver]


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


def engine_command():
    """How to start the engine as a child process: the script through the
    venv's python normally, or this same executable in a frozen build."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "engine"]
    return [child_python(), "-u", FINDEX_PY]


def no_window():
    """Keep a console window from flashing up on Windows."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def child_env():
    """Environment for the engine process. Its output is read here as UTF-8,
    so make it write UTF-8: a Windows child writing to a pipe otherwise
    picks cp1252 and a folder name outside that set would end the run."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def int_of(var, default=0):
    """An IntVar's value, or default when its Spinbox has been left blank or
    half-typed (IntVar.get raises TclError then, which used to stop the
    window from closing)."""
    try:
        return int(var.get())
    except (tk.TclError, ValueError, TypeError):
        return default


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


def connected_drives():
    """Every storage drive/volume attached to this computer."""
    drives = []
    if os.name == "nt":
        import ctypes
        DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                root = "{}:\\".format(chr(65 + i))
                kind = ctypes.windll.kernel32.GetDriveTypeW(root)
                if kind in (DRIVE_FIXED, DRIVE_REMOVABLE):
                    drives.append(root)
    elif sys.platform == "darwin":
        drives.append("/")
        try:
            for name in sorted(os.listdir("/Volumes")):
                p = os.path.join("/Volumes", name)
                if (not name.startswith(".") and os.path.isdir(p)
                        and os.path.realpath(p) != "/"):
                    drives.append(p)
        except OSError:
            pass
    else:
        drives.append("/")
    return drives


def _path_under(path, root):
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


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
        self.watch_proc = None
        self._watch_refresh = 0.0
        self.last_index_finished = time.time()
        self.sort_col = None
        self.sort_desc = False
        self._tip_after = None
        self._tip_win = None
        self._setup_retried = False
        self._pending_index = False
        self.pal = dict(theme.DARK, **palette_extras(theme.DARK))
        self._menus = []                 # classic tk menus, recoloured on toggle
        self._spins = []                 # classic tk Spinboxes (Tk 8.5 fallback)
        self._clip = {"paths": [], "move": False}
        self._render_gen = 0
        self._progress_est = 0

        root.title("findex")
        root.geometry(self.cfg.get("geometry") or DEFAULTS["geometry"])
        root.minsize(820, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if sys.platform == "darwin":
            # Cmd+Q and the Dock's Quit bypass WM_DELETE_WINDOW: without
            # this the settings are lost and the live-updates process is
            # left running with no window
            try:
                root.createcommand("::tk::mac::Quit", self.on_close)
            except tk.TclError:
                pass

        import tkinter.font as tkfont
        self.ui_size = 13 if sys.platform == "darwin" else 10
        self.ui_family = theme.ui_family(root)
        self.mono_family = theme.mono_family(root)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=self.ui_family,
                                                  size=self.ui_size)
            except tk.TclError:
                pass
        self.search_font = tkfont.Font(family=self.ui_family,
                                       size=self.ui_size + 3)

        self._build_vars()
        self._build_menu()
        self._build_layout()
        self.apply_theme()
        # Ctrl+D toggles dark mode. Entry/Text widgets have an emacs-style
        # Ctrl+D (delete a character) class binding, which is replaced so the
        # toggle is the only thing that happens.
        toggle = lambda e: (self.toggle_dark(), "break")[1]   # noqa: E731
        for cls in ("Entry", "TEntry", "TCombobox", "Spinbox", "TSpinbox",
                    "Text"):
            self.root.bind_class(cls, "<Control-d>", toggle)
        self.root.bind_all("<Control-d>", lambda e: self.toggle_dark())

        self._ensure_schema()            # older index gains the new columns
        self._load_roots()               # the folder list lives in the index
        self.res = ResourceMonitor()
        self.root.after(POLL_MS, self._pump)
        self.root.after(AUTO_CHECK_MS, self._auto_tick)
        self.root.after(RES_TICK_MS, self._res_tick)
        self.refresh_stats()
        self.run_search(live=False)      # fill the list on launch
        self.root.after(800, self._auto_setup)
        self.root.after(2500, self._maybe_start_watch)
        self.query_entry.focus_set()

    def _ensure_schema(self):
        """Open the index read-write once, so a database built by an older
        findex gains the new columns before any search touches them."""
        try:
            findex.open_db(self.var_db.get()).close()
        except Exception:                                      # noqa: BLE001
            pass

    def _load_roots(self):
        """Fill the folder list from the index. The list is stored in the
        database so it travels with it: open an existing findex.db and the
        folders come back with it. A database that has none yet - built by an
        older findex - takes the list from the settings file once, and keeps
        it from then on."""
        roots = []
        try:
            conn = findex.open_db_ro(self.var_db.get())
            roots = findex.saved_roots(conn)
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass
        if not roots:
            roots = list(self.cfg.get("roots", []))
            if roots:
                self._save_roots(roots)
        self.roots_list.delete(0, "end")
        for r in roots:
            self.roots_list.insert("end", r)

    def _save_roots(self, roots=None):
        """Write the folder list into the index. A short timeout on purpose:
        if an index run holds the write lock, this simply gives up and the
        list is saved again when the run finishes (and the settings file
        still has a copy meanwhile)."""
        if roots is None:
            roots = self.current_roots()
        try:
            conn = findex.open_db(self.var_db.get(), timeout=2)
            findex.set_roots(conn, roots)
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass

    # -- variables ---------------------------------------------------------

    def _build_vars(self):
        c = self.cfg
        self.var_db = tk.StringVar(value=c["db"])
        self.var_query = tk.StringVar()
        self.var_exts = tk.StringVar(value=c.get("exts", ""))
        self.var_exts.trace_add("write", lambda *_: self.on_query_changed())
        self.var_limit = tk.IntVar(value=int(c.get("limit", 0)))
        self.var_status = tk.StringVar(value="Ready")
        self.var_hint = tk.StringVar(value="")
        self.var_res = tk.StringVar(value="")
        self.var_progress = tk.StringVar(value="")
        self.var_workers = tk.IntVar(value=int(c.get("workers", 0)))
        self.var_rebuild = tk.BooleanVar(value=bool(c.get("rebuild", False)))
        self.var_cloud = tk.BooleanVar(value=bool(c.get("include_cloud", False)))
        self.var_ocr = tk.BooleanVar(value=bool(c.get("ocr", False)))
        self.var_dark = tk.BooleanVar(value=bool(c.get("dark", True)))
        self.var_auto = tk.BooleanVar(value=bool(c.get("auto_index", False)))
        self.var_auto_mins = tk.IntVar(value=int(c.get("auto_minutes", 60)))
        self.var_auto_next = tk.StringVar(value="")
        self.var_watch = tk.BooleanVar(value=bool(c.get("watch", False)))
        self.var_watch_note = tk.StringVar(value="")
        self.var_counts = tk.StringVar(value="Idle")
        self.var_stats = tk.StringVar(value="")

        self.var_query.trace_add("write", lambda *_: self.on_query_changed())

    # -- menu --------------------------------------------------------------

    def _build_menu(self):
        bar = tk.Menu(self.root)
        self._menus.append(bar)

        m = tk.Menu(bar, tearoff=0)
        self._menus.append(m)
        m.add_command(label="Choose index database...", command=self.choose_db)
        m.add_command(label="Open index folder", command=self.open_db_folder)
        m.add_separator()
        m.add_command(label="Default save folder...",
                      command=self.choose_save_dir)
        m.add_command(label="Reset to Downloads", command=self.reset_save_dir)
        m.add_separator()
        m.add_command(label="Quit", command=self.on_close)
        bar.add_cascade(label="File", menu=m)

        accel = "Cmd" if sys.platform == "darwin" else "Ctrl"
        m = tk.Menu(bar, tearoff=0)
        self._menus.append(m)
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
        self._menus.append(m)
        m.add_checkbutton(label="Dark mode", accelerator="Ctrl+D",
                          variable=self.var_dark, command=self.apply_theme)
        bar.add_cascade(label="Appearance", menu=m)

        m = tk.Menu(bar, tearoff=0)
        self._menus.append(m)
        m.add_command(label="Search syntax", command=self.show_syntax)
        m.add_command(label="About findex", command=self.show_about)
        bar.add_cascade(label="Help", menu=m)

        self.root.config(menu=bar)

    # -- appearance --------------------------------------------------------

    def toggle_dark(self):
        self.var_dark.set(not self.var_dark.get())
        self.apply_theme()

    def apply_theme(self):
        """One coherent, high-contrast look drawn entirely from the shared
        palette (theme.py) - dark by default, light on request, remembered
        between runs. The ttk styles come from theme.apply; everything that
        is a classic tk widget is recoloured here to match."""
        dark = bool(self.var_dark.get())
        c = theme.apply(self.root, dark)
        pal = dict(c, **palette_extras(c))
        self.pal = pal
        style = ttk.Style(self.root)
        ui = self.ui_family
        # macOS Tk measures fonts differently: keep the size the window has
        # always used there, the house size everywhere else
        style.configure(".", font=(ui, self.ui_size))
        style.configure("Treeview", font=(ui, self.ui_size))
        style.configure("TRadiobutton", background=c["bg"],
                        foreground=c["text"])
        style.map("TRadiobutton", background=[("active", c["bg"])],
                  foreground=[("disabled", c["dim"])])
        style.configure("TLabelframe", background=c["bg"],
                        bordercolor=c["border"], lightcolor=c["bg"],
                        darkcolor=c["bg"], padding=10)
        style.configure("TLabelframe.Label", background=c["bg"],
                        foreground=c["dim"])
        style.configure("Search.TEntry", padding=7)
        style.configure("TSpinbox", fieldbackground=c["field"],
                        foreground=c["field_text"], insertcolor=c["accent"],
                        background=c["panel"], arrowcolor=c["accent"],
                        bordercolor=c["field_border"],
                        lightcolor=c["field_border"],
                        darkcolor=c["field_border"], padding=4,
                        relief="solid")
        style.map("TSpinbox",
                  bordercolor=[("focus", c["accent"])],
                  lightcolor=[("focus", c["accent"])],
                  darkcolor=[("focus", c["accent"])],
                  fieldbackground=[("readonly", c["panel"]),
                                   ("disabled", c["bg"])],
                  foreground=[("disabled", c["dim"])])
        style.configure("Treeview.Heading", padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", c["panel"])])
        style.configure("Horizontal.TProgressbar", background=c["accent"],
                        troughcolor=c["panel"], bordercolor=c["border"],
                        lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure("Accent.TLabel", background=c["bg"],
                        foreground=c["accent"])
        # plain-tk widgets follow the same palette: anything you can type in
        # or click is a different shade from its surroundings, with a border
        # and an accent ring when focused
        self.preview.configure(background=c["panel"], foreground=c["text"],
                               insertbackground=c["accent"],
                               selectbackground=c["sel"],
                               selectforeground=c["text"],
                               font=(ui, self.ui_size),
                               relief="flat", highlightthickness=1,
                               highlightbackground=c["border"],
                               highlightcolor=c["accent"])
        self.preview.tag_configure("hit", background=pal["hit_bg"],
                                   foreground=pal["hit_fg"])
        self.preview.tag_configure("path", foreground=c["accent"])
        self.tree.tag_configure("odd", background=pal["row_alt"])
        self.roots_list.configure(background=c["field"],
                                  foreground=c["field_text"],
                                  selectbackground=c["accent"],
                                  selectforeground=c["accent_text"],
                                  font=(ui, self.ui_size),
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=c["field_border"],
                                  highlightcolor=c["accent"])
        self.log.configure(background=c["panel"], foreground=c["text"],
                           insertbackground=c["accent"],
                           selectbackground=c["sel"],
                           selectforeground=c["text"],
                           font=(self.mono_family, self.ui_size),
                           relief="flat", highlightthickness=1,
                           highlightbackground=c["border"],
                           highlightcolor=c["accent"])
        for m in self._menus:
            m.configure(background=c["panel"], foreground=c["text"],
                        activebackground=c["sel"],
                        activeforeground=c["text"])
        for s in self._spins:               # classic tk.Spinbox (Tk 8.5)
            s.configure(background=c["field"], foreground=c["field_text"],
                        insertbackground=c["accent"],
                        buttonbackground=c["panel"],
                        highlightthickness=1,
                        highlightbackground=c["field_border"],
                        highlightcolor=c["accent"])


    def _track_spin(self, spin):
        """Remember a classic tk.Spinbox (the Tk 8.5 fallback) so the palette
        can be applied to it - ttk ones are covered by the TSpinbox style."""
        if not isinstance(spin, ttk.Widget):
            self._spins.append(spin)

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
        self.tab_changes = ttk.Frame(self.nb)
        self.nb.add(self.tab_changes, text="  Changes  ")

        self._build_search_tab()
        self._build_index_tab()
        self._build_changes_tab()
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom", padx=12, pady=8)
        ttk.Label(bar, textvariable=self.var_status).pack(side="left")
        ttk.Label(bar, textvariable=self.var_hint,
                  style="Dim.TLabel").pack(side="left", padx=14)
        # Right-hand end, packed right to left: bar, its percentage, then
        # the resource readout. The bar mirrors the one on the Index tab so
        # a run can be watched from the Search tab.
        self.busy = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.busy.pack(side="right")
        ttk.Label(bar, textvariable=self.var_progress,
                  style="Dim.TLabel").pack(side="right", padx=(0, 8))
        self.lbl_res = ttk.Label(bar, textvariable=self.var_res,
                                 style="Dim.TLabel")
        self.lbl_res.pack(side="right", padx=(0, 18))
        self.tip(self.lbl_res,
                 "CPU and memory used by findex and every worker process it "
                 "has started. CPU is a share of the whole machine, so 100% "
                 "means every core is busy.")

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
                 "One box searches everything, live as you type. Bare words "
                 "match file and folder names; content:word searches inside "
                 "files; C:\\ limits to a drive or folder; ext:pdf limits the "
                 "type; !word leaves results out. Help > Search syntax has "
                 "the full list. Esc clears; the down arrow jumps into the "
                 "results.", popup=False)

        btn = ttk.Button(top, text="Search", width=10,
                         style="Accent.TButton",
                         command=lambda: self.run_search(live=False))
        btn.pack(side="left", padx=(8, 0))
        self.tip(btn, "Run the search now - the same as pressing Enter.")

        btn = ttk.Button(top, text="Duplicates", width=11,
                         command=self.find_dupes)
        btn.pack(side="left", padx=(6, 0))
        self.tip(btn, "List files that share the same name AND size - the "
                      "classic duplicate candidates - grouped together, "
                      "biggest first, with a total of the space you could "
                      "get back. The Type box narrows it; any search brings "
                      "the normal list back.")

        self.type_box = ttk.Combobox(top, textvariable=self.var_exts,
                                     width=17, height=28,
                                     values=["All types"],
                                     postcommand=self._refresh_types)
        self.type_box.pack(side="left", padx=(8, 0))
        self.tip(self.type_box,
                 "Filter by type. Groups cover a whole family - Images, "
                 "Videos, Audio, Documents, Compressed... - and below them "
                 "every file type actually in your index, with counts, plus "
                 "a 'folders' entry. Pick one or type your own list like: "
                 "pdf, docx. 'All types' or an empty box means everything. "
                 "The list refreshes itself each time it opens.")

        opts = ttk.Frame(self.tab_search)
        opts.pack(fill="x", padx=12, pady=(0, 4))

        hint = ttk.Label(opts, style="Dim.TLabel",
                         text="names as you type  ·  content:word  ·  C:\\  ·"
                              "  ext:pdf  ·  !leave-out  ·  folder:")
        hint.pack(side="left", padx=(0, 20))
        self.tip(hint, "The search understands Everything-style filters, "
                       "combined freely - e.g.  C: content:dan ext:pdf "
                       "!draft  finds PDFs on C: containing 'dan' whose name "
                       "doesn't contain 'draft'. Help > Search syntax has "
                       "the full list.")

        lbl = ttk.Label(opts, text="Max results:")
        lbl.pack(side="left", padx=(20, 4))
        spin = Spinbox(opts, from_=0, to=1000000, increment=100, width=7,
                       textvariable=self.var_limit)
        spin.pack(side="left")
        self._track_spin(spin)
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
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_preview())
        self.tree.bind("<Button-3>", self.popup_menu)
        self.tree.bind("<Button-2>", self.popup_menu)      # mac right-click
        if sys.platform == "darwin":
            # Ctrl-click is a right-click on a Mac; anywhere else it is
            # the add-to-selection click, and must stay one
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
                               padx=8, pady=6)
        pvsb = ttk.Scrollbar(prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pvsb.set, state="disabled")
        self.preview.pack(side="left", fill="x", expand=True)
        pvsb.pack(side="right", fill="y")
        self.tip(self.preview,
                 "The full path of the selected file, and in contents mode the "
                 "matching text with your search terms highlighted.",
                 popup=False)

        self.ctx = tk.Menu(self.root, tearoff=0)
        self._menus.append(self.ctx)
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

    # -- Changes tab: the journal ------------------------------------------

    JOURNAL_SINCE = (("Last hour", "1h"), ("Today", "today"),
                     ("Last 7 days", "7d"), ("Last 30 days", "30d"),
                     ("Everything", None))
    JOURNAL_LIMIT = 2000

    def _build_changes_tab(self):
        top = ttk.Frame(self.tab_changes)
        top.pack(fill="x", padx=12, pady=(10, 4))

        self.var_jtext = tk.StringVar()
        self.var_jtext.trace_add("write", lambda *_: self._journal_debounce())
        e = ttk.Entry(top, textvariable=self.var_jtext, font=self.search_font)
        e.pack(side="left", fill="x", expand=True)
        self.tip(e, "Show only changes whose path contains this.")

        self.var_jtype = tk.StringVar(value="all")
        cb = ttk.Combobox(top, textvariable=self.var_jtype, width=10,
                          state="readonly",
                          values=("all",) + findex.JOURNAL_EVENTS)
        cb.pack(side="left", padx=(8, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_journal())

        self.var_jsince = tk.StringVar(value=self.JOURNAL_SINCE[2][0])
        cb = ttk.Combobox(top, textvariable=self.var_jsince, width=13,
                          state="readonly",
                          values=[k for k, _ in self.JOURNAL_SINCE])
        cb.pack(side="left", padx=(8, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_journal())

        b = ttk.Button(top, text="Refresh", width=9,
                       command=self.refresh_journal)
        b.pack(side="left", padx=(8, 0))
        b = ttk.Button(top, text="Clear journal...", width=15,
                       command=self.clear_journal_ui)
        b.pack(side="left", padx=(8, 0))
        self.tip(b, "Empty the journal. The index itself is untouched.")

        hint = ttk.Label(self.tab_changes, style="Dim.TLabel", text=(
            "Every change findex has noticed inside the indexed folders: "
            "files and folders added, modified, renamed or deleted. Index "
            "runs record what differs since the last run; Live updates "
            "records changes as they happen, including renames. The first "
            "run over a new location is not journaled."))
        hint.pack(fill="x", padx=12, pady=(0, 6))

        holder = ttk.Frame(self.tab_changes)
        holder.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        cols = ("when", "change", "name", "folder", "size", "detail")
        self.jtree = ttk.Treeview(holder, columns=cols, show="headings",
                                  selectmode="extended")
        for key, text, width, anchor, stretch in (
                ("when", "When", 140, "w", False),
                ("change", "Change", 80, "w", False),
                ("name", "Name", 240, "w", False),
                ("folder", "Folder", 360, "w", True),
                ("size", "Size", 80, "e", False),
                ("detail", "Detail", 260, "w", False)):
            self.jtree.heading(key, text=text)
            self.jtree.column(key, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.jtree.yview)
        self.jtree.configure(yscrollcommand=vsb.set)
        self.jtree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.jtree.bind("<Double-1>", lambda e: self._journal_open())
        self.jtree.bind("<Return>", lambda e: self._journal_open())
        self.jtree.bind("<Button-3>", self._journal_popup)
        self.jtree.bind("<Button-2>", self._journal_popup)
        if sys.platform == "darwin":
            self.jtree.bind("<Control-Button-1>", self._journal_popup)
        self.jctx = tk.Menu(self.root, tearoff=0)
        self._menus.append(self.jctx)
        self.jctx.add_command(label="Open", command=self._journal_open)
        self.jctx.add_command(label="Show in folder",
                              command=self._journal_reveal)
        self.jctx.add_command(label="Copy path", command=self._journal_copy)

        self.var_jstatus = tk.StringVar(value="")
        ttk.Label(self.tab_changes, textvariable=self.var_jstatus,
                  style="Dim.TLabel").pack(fill="x", padx=12, pady=(0, 8))
        self._jpaths = {}                  # tree iid -> path
        self._jafter = None
        self._jloaded = False

    def _on_tab_changed(self, _event=None):
        try:
            if self.nb.tab(self.nb.select(), "text").strip() == "Changes":
                self.refresh_journal()
        except tk.TclError:
            pass

    def _journal_debounce(self):
        if self._jafter:
            self.root.after_cancel(self._jafter)
        self._jafter = self.root.after(200, self.refresh_journal)

    def _journal_since(self):
        label = self.var_jsince.get()
        code = dict(self.JOURNAL_SINCE).get(label)
        if code is None:
            return None
        if code == "today":
            t = time.localtime()
            return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0,
                                0, 0, -1))
        return findex.parse_since(code)

    def refresh_journal(self):
        """Fill the Changes list from the journal (read-only connection, so
        it never waits behind an index run)."""
        self._jafter = None
        try:
            conn = findex.open_db_ro(self.var_db.get())
            rows = findex.journal_rows(
                conn, since=self._journal_since(),
                event=self.var_jtype.get(),
                text=self.var_jtext.get().strip() or None,
                limit=self.JOURNAL_LIMIT)
            conn.close()
        except Exception:                                      # noqa: BLE001
            rows = []
        self.jtree.delete(*self.jtree.get_children())
        self._jpaths = {}
        for _, ts, event, path, old_path, size, is_dir, source in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            change = event + ("  (folder)" if is_dir else "")
            detail = ""
            if old_path:
                detail = "was " + (os.path.basename(old_path)
                                   if os.path.dirname(old_path)
                                   == os.path.dirname(path) else old_path)
            if source == "watch":
                detail = (detail + "   " if detail else "") + "live"
            iid = self.jtree.insert("", "end", values=(
                when, change, os.path.basename(path) or path,
                os.path.dirname(path),
                "" if is_dir or size is None else findex.human(size),
                detail))
            self._jpaths[iid] = path
        self._jloaded = True
        n = len(rows)
        self.var_jstatus.set(
            "{:,} change(s){}".format(
                n, " - showing the newest {:,}".format(self.JOURNAL_LIMIT)
                if n >= self.JOURNAL_LIMIT else "")
            if n else "No changes recorded for that.")

    def _journal_selected_path(self):
        sel = self.jtree.selection()
        return self._jpaths.get(sel[0]) if sel else None

    def _journal_popup(self, event):
        iid = self.jtree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.jtree.selection():
            self.jtree.selection_set(iid)
        self.jctx.tk_popup(event.x_root, event.y_root)

    def _journal_open(self):
        path = self._journal_selected_path()
        if not path:
            return
        if os.path.exists(path):
            open_path(path)
        else:                       # deleted or moved on: show where it was
            reveal_path(os.path.dirname(path))
            self.var_status.set("No longer there - opened its folder")

    def _journal_reveal(self):
        path = self._journal_selected_path()
        if path:
            reveal_path(path if os.path.exists(path)
                        else os.path.dirname(path))

    def _journal_copy(self):
        paths = [self._jpaths[i] for i in self.jtree.selection()
                 if i in self._jpaths]
        if paths:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))
            self.var_status.set("Path copied" if len(paths) == 1
                                else "{:,} paths copied".format(len(paths)))

    def clear_journal_ui(self):
        if not messagebox.askyesno(
                "Clear the journal?",
                "Forget every recorded change? The index and your files are "
                "untouched - only the history of what changed is emptied."):
            return
        try:
            conn = findex.open_db(self.var_db.get(), timeout=5)
            findex.journal_clear(conn)
            conn.close()
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not clear", str(exc))
            return
        self.refresh_journal()

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
                 "stored in the index itself, so you set it once and it "
                 "follows the database wherever it goes.", popup=False)

        btns = ttk.Frame(row)
        btns.pack(side="left", padx=8)
        b = ttk.Button(btns, text="Add folder...", width=14, command=self.add_root)
        b.pack(pady=2)
        self.tip(b, "Choose a folder - or a whole drive such as D:\\ - to add "
                    "to the list.")
        b = ttk.Button(btns, text="Add all drives", width=14,
                       command=self.add_all_drives)
        b.pack(pady=2)
        self.tip(b, "Add every drive connected to this computer - internal "
                    "and removable. Network drives are left out; add those "
                    "with Add folder if you want them. Folders already listed "
                    "that live on an added drive are folded in, so nothing "
                    "gets indexed twice.")
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
        self._track_spin(spin)
        note = ttk.Label(line, text="(0 = one per CPU core)",
                         style="Dim.TLabel")
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
        self._track_spin(spin)
        self.tip(spin, "Minutes between automatic runs. 60 is a sensible "
                       "starting point; re-runs only read what changed.")
        ttk.Label(auto, text="minutes while the app is open").pack(side="left")
        ttk.Label(auto, textvariable=self.var_auto_next,
                  style="Accent.TLabel").pack(side="left", padx=12)

        live = ttk.Frame(opts)
        live.pack(fill="x", padx=8, pady=(0, 8))
        chk = ttk.Checkbutton(live, text="Live updates - watch these folders "
                                         "and index changes as they happen",
                              variable=self.var_watch,
                              command=self.toggle_watch)
        chk.pack(side="left")
        self.tip(chk, "Everything-style real-time indexing: while the app is "
                      "open, new, changed, renamed and deleted files show up "
                      "in search within seconds - no waiting for the next "
                      "indexing run. Uses the folder list above. Runs "
                      "quietly alongside normal indexing and stops when the "
                      "app closes.")
        ttk.Label(live, textvariable=self.var_watch_note,
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
                           padx=8, pady=6)
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
        b = ttk.Button(foot, text="Export file tree...",
                       command=self.export_tree_ui)
        b.pack(side="right", padx=(0, 6))
        self.tip(b, "Write everything the index knows - every folder and "
                    "file under every indexed location - to a file. A .txt "
                    "is the classic tree drawing with folder totals; .csv is "
                    "one row per path with size and date; .json is nested.")
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

    def _type_filter(self):
        """The Type box: extensions, group names (Images, Videos...) which
        expand to their whole family, and the special 'folders' entry which
        becomes a kind filter rather than an extension."""
        exts = parse_exts(self.var_exts.get())
        kind = None
        if exts:
            expanded = []
            for e in exts:
                if e in ("folder", "folders", "dir"):
                    kind = "folder"
                elif e in TYPE_GROUPS:
                    expanded += [x.lstrip(".") for x in TYPE_GROUPS[e]]
                else:
                    expanded.append(e)
            exts = expanded or None
        return exts, kind

    def run_search(self, live=False):
        text = self.var_query.get().strip()
        self.search_gen += 1
        gen = self.search_gen
        db = self.var_db.get()
        try:
            limit = max(0, int(self.var_limit.get()))
        except (tk.TclError, ValueError):
            limit = 0
        exts, kind = self._type_filter()
        self.var_status.set("Searching...")
        threading.Thread(target=self._search_worker,
                         args=(gen, db, text, limit, exts, kind, live),
                         daemon=True).start()

    def _search_worker(self, gen, db, text, limit, exts, kind, live=False):
        conn = None
        try:
            try:
                conn = findex.open_db_ro(db)
            except sqlite3.Error:
                conn = findex.open_db(db)   # index file not created yet
            total = None
            if not text:
                # Empty box = browse the whole index, newest first. Typing
                # narrows it down; the status bar shows the true total.
                total = conn.execute(
                    "SELECT COUNT(*) FROM files").fetchone()[0]
            raw = findex.query_rows(conn, text, limit, exts=exts, kind=kind,
                                    live=live)
            rows = [{"path": r[0], "name": os.path.basename(r[0]),
                     "size": r[1], "mtime": r[2], "snippet": r[3],
                     "is_dir": bool(r[4])} for r in raw]
            self.msgs.put(("results", gen, rows, total))
        except sqlite3.OperationalError as exc:
            self.msgs.put(("search_error", gen, str(exc)))
        except Exception as exc:                               # noqa: BLE001
            self.msgs.put(("search_error", gen,
                           "{}: {}".format(type(exc).__name__, exc)))
        finally:
            if conn is not None:
                conn.close()

    def find_dupes(self):
        """Fill the list with duplicate candidates: files sharing name AND
        size, grouped together, biggest first."""
        self.search_gen += 1
        gen = self.search_gen
        db = self.var_db.get()
        try:
            limit = max(0, int(self.var_limit.get()))
        except (tk.TclError, ValueError):
            limit = 0
        exts, _kind = self._type_filter()
        self.var_status.set("Looking for duplicates...")
        threading.Thread(target=self._dupes_worker,
                         args=(gen, db, limit, exts), daemon=True).start()

    def _dupes_worker(self, gen, db, limit, exts):
        conn = None
        try:
            try:
                conn = findex.open_db_ro(db)
            except sqlite3.Error:
                conn = findex.open_db(db)
            groups, files, wasted = findex.dupe_summary(conn, exts)
            raw = findex.dupe_rows(conn, limit, exts)
            rows = [{"path": r[0], "name": os.path.basename(r[0]),
                     "size": r[1], "mtime": r[2], "is_dir": False,
                     "snippet": "{:,} files share this name and size - keep "
                                "the one you want, the rest are duplicate "
                                "candidates".format(r[3])}
                    for r in raw]
            self.msgs.put(("results", gen, rows, None))
            if groups:
                self.msgs.put(("status",
                               "{:,} duplicate set(s) - {:,} files, {} to be "
                               "had back if each set kept one copy".format(
                                   groups, files, findex.human(wasted))))
            else:
                self.msgs.put(("status",
                               "No duplicates found (matched by name + size)"))
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
                   values=(row["name"],
                           "folder" if row.get("is_dir")
                           else human(row["size"]),
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
                                "The list shows your indexed files and "
                                "folders, newest first. Type to narrow it - "
                                "names by default, content:word for text "
                                "inside files, C:\\ for a drive, !word to "
                                "leave things out.")
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
            initialdir=self.save_dir())
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
                elif os.path.isdir(p):      # folders are results too
                    shutil.copytree(p, target)
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
                # the row itself - and, if it was a folder, everything the
                # index holds underneath it
                like = findex.like_escape(p.rstrip("\\/")) + os.sep + "%"
                for (fid,) in cur.execute(
                        "SELECT id FROM files WHERE path=? "
                        "OR path LIKE ? ESCAPE '!'", (p, like)).fetchall():
                    cur.execute("DELETE FROM docs WHERE rowid=?", (fid,))
                    cur.execute("DELETE FROM files WHERE id=?", (fid,))
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
                self._save_roots()

    def remove_root(self):
        for i in reversed(self.roots_list.curselection()):
            self.roots_list.delete(i)
        self._save_roots()

    def clear_roots(self):
        self.roots_list.delete(0, "end")
        self._save_roots()

    def add_all_drives(self):
        """List every connected drive, folding in any existing roots that an
        added drive already covers."""
        drives = connected_drives()
        if not drives:
            self.var_status.set("No drives found")
            return
        current = self.current_roots()
        new = [d for d in drives if d not in current]
        extras = [r for r in current if r not in drives]
        surviving = [r for r in extras
                     if not any(_path_under(r, d) for d in drives)]
        folded = len(extras) - len(surviving)
        self.roots_list.delete(0, "end")
        for r in drives + surviving:
            self.roots_list.insert("end", r)
        self._save_roots()
        msg = ("Added {:,} drive(s)".format(len(new)) if new
               else "All connected drives were already listed")
        if folded > 0:
            msg += " - {:,} folder(s) folded in (already covered)".format(
                folded)
        self.var_status.set(msg)

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
        cmd = engine_command() + ["--db", self.var_db.get(),
                                  "index"] + roots + ["--progress"]
        if self.var_rebuild.get():
            cmd.append("--rebuild")
        if self.var_cloud.get():
            cmd.append("--include-cloud")
        if self.var_ocr.get():
            cmd.append("--ocr")
        workers = int_of(self.var_workers)
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
        self.launch(pip_install_cmd(pkgs), "setup",
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
        elif sys.platform == "darwin":
            manager = "Homebrew"
            found = (shutil.which("brew")
                     or next((p for p in ("/opt/homebrew/bin/brew",
                                          "/usr/local/bin/brew")
                              if os.path.exists(p)), None))
            cmd = [found, "install", "tesseract"] if found else None
        else:
            manager = "a package manager findex knows"   # Linux: hands off
            cmd = None
        if cmd is None:
            messagebox.showinfo(
                "Tesseract needed",
                "OCR needs the tesseract program, and {} was not found to "
                "install it with.\n\nInstall tesseract manually (e.g. "
                "sudo apt install tesseract-ocr), or untick OCR."
                .format(manager))
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
                self.launch(pip_install_cmd(
                                pkgs, ["--user", "--break-system-packages"]),
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
        # The live-updates process holds the database open. On macOS/Linux
        # deleting an open file leaves the watcher writing into a ghost copy
        # nobody can read, so it is stopped first and started again after.
        watching = self.watch_proc is not None
        if watching:
            self.stop_watch()
            time.sleep(0.5)
        try:
            result = findex.clear_index(self.var_db.get())
        except Exception as exc:                               # noqa: BLE001
            messagebox.showerror("Could not clear", str(exc))
            return
        finally:
            if watching:
                self.root.after(1500, self._maybe_start_watch)
        self.log_line("-- " + result + " --")
        self._save_roots()           # a fresh index, but the same folders
        self.var_status.set("Index cleared - ready to start fresh")
        self.refresh_stats()
        self.run_search(live=False)

    def export_tree_ui(self):
        """Save the whole index as a file tree. The export runs in the
        engine process like a vacuum does, so a 300k-row index does not
        freeze the window while it is drawn."""
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        path = filedialog.asksaveasfilename(
            title="Export file tree",
            initialdir=findex.downloads_dir(),
            initialfile="findex-tree-{}.txt".format(time.strftime("%Y-%m-%d")),
            defaultextension=".txt",
            filetypes=[("Tree drawing (text)", "*.txt"),
                       ("One row per path (CSV)", "*.csv"),
                       ("Nested (JSON)", "*.json")])
        if not path:
            return
        self._tree_out = path
        cmd = engine_command() + ["--db", self.var_db.get(), "tree", "-o", path]
        self.launch(cmd, "tree", "Exporting the file tree...")

    def run_vacuum(self):
        if self.proc is not None:
            messagebox.showinfo("Busy", "Something is already running.")
            return
        cmd = engine_command() + ["--db", self.var_db.get(), "vacuum"]
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
                errors="replace", bufsize=1, cwd=HERE, env=child_env(),
                **kwargs)
        except Exception as exc:                               # noqa: BLE001
            self.proc = None
            messagebox.showerror("Could not start", str(exc))
            return
        self.proc_kind = kind
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._progress_start()
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
        self._kill_proc_tree(self.proc)
        self.root.after(3000, self._ensure_stopped)

    def _kill_proc_tree(self, proc, force=False):
        """End a background run AND every worker process it started.
        Terminating only the parent left the workers running - which is why
        Stop used to say 'stopping' and nothing happened."""
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid),
                                "/T", "/F"],
                               capture_output=True, **no_window())
            else:
                import signal
                sig = signal.SIGKILL if force else signal.SIGTERM
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
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
        self._kill_proc_tree(proc, force=True)

    # -- live updates (watch) ----------------------------------------------

    def toggle_watch(self):
        if self.var_watch.get():
            self._maybe_start_watch()
        else:
            self.stop_watch()

    def _maybe_start_watch(self):
        """Start the live-updates process when it should be running and
        is not: the box is ticked, folders exist, nothing is installing."""
        if not self.var_watch.get() or self.watch_proc is not None:
            return
        if self.proc_kind == "setup":
            return          # watchdog may still be installing - retried after
        roots = [r for r in self.current_roots() if os.path.isdir(r)]
        if not roots:
            return
        cmd = engine_command() + ["--db", self.var_db.get(), "watch"] + roots
        if self.var_cloud.get():
            cmd.append("--include-cloud")
        if self.var_ocr.get() and findex.have_ocr_backend():
            cmd.append("--ocr")
        try:
            kwargs = no_window()
            if os.name != "nt":
                kwargs["start_new_session"] = True   # own process group
            self.watch_proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=HERE, env=child_env(),
                **kwargs)
        except Exception as exc:                               # noqa: BLE001
            self.watch_proc = None
            self.log_line("could not start live updates: {}".format(exc))
            return
        self.var_watch_note.set("watching for changes")
        threading.Thread(target=self._watch_reader,
                         args=(self.watch_proc,), daemon=True).start()

    def _watch_reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.msgs.put(("watch", line))
        except Exception:                                      # noqa: BLE001
            pass
        finally:
            code = proc.wait()
            self.msgs.put(("watch_end", proc, code))

    def stop_watch(self):
        proc = self.watch_proc
        self.watch_proc = None
        self.var_watch_note.set("")
        if proc is not None and proc.poll() is None:
            self._kill_proc_tree(proc)
            self.log_line("-- live updates off --")

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
                elif kind == "status":
                    self.var_status.set(msg[1])
                elif kind == "log":
                    self.log_line(msg[1])
                elif kind == "watch":
                    self.log_line("watch: " + msg[1])
                    if time.time() - self._watch_refresh > 15:
                        self._watch_refresh = time.time()
                        self.refresh_stats()
                        self.run_search(live=True)
                        if getattr(self, "_jloaded", False):
                            self.refresh_journal()
                elif kind == "watch_end":
                    _, proc, code = msg
                    if self.watch_proc is proc:
                        # it stopped on its own (missing component, bad root)
                        self.watch_proc = None
                        self.var_watch_note.set("")
                        self.log_line("-- live updates stopped "
                                      "(exit {}) --".format(code))
                elif kind == "progress":
                    p = msg[1]
                    pct_txt = ""
                    est = self._progress_est
                    if est > 0:
                        pct = min(99.0, 100.0 * p.get("seen", 0) / est)
                        self._progress_set(pct)
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
        self._progress_stop()
        self._progress_est = 0
        self._save_roots()           # a folder added during the run
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if self.proc_kind == "index":
            self.last_index_finished = time.time()
            self.update_auto_label()
        if self.proc_kind == "setup" and self._handle_setup_end(code):
            return
        word = "finished" if code == 0 else "stopped (exit {})".format(code)
        self.var_status.set("{} {}".format(self.proc_kind.title(), word))
        if self.proc_kind == "tree" and code == 0 \
                and getattr(self, "_tree_out", None):
            self.var_status.set("File tree exported to " + self._tree_out)
            reveal_path(self._tree_out)
        self.log_line("-- {} {} --\n".format(self.proc_kind, word))
        self.proc_kind = ""
        self.refresh_stats()
        self.run_search(live=False)      # refresh the visible list
        if getattr(self, "_jloaded", False):
            self.refresh_journal()
        self._maybe_start_watch()        # live updates waiting on setup/run

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
        mins = max(5, int_of(self.var_auto_mins, 60) or 60)
        due = self.last_index_finished + mins * 60
        self.var_auto_next.set("next run " + time.strftime("%H:%M",
                                                           time.localtime(due)))

    # -- progress + resources ----------------------------------------------

    def _progress_start(self):
        """Both bars sweeping: the Index tab's and the status strip's."""
        for widget in (self.bar, self.busy):
            widget.configure(mode="indeterminate", value=0)
            widget.start(12)
        self.var_progress.set("")

    def _progress_set(self, pct):
        """Both bars to a real percentage, switching out of sweep mode."""
        for widget in (self.bar, self.busy):
            if str(widget.cget("mode")) != "determinate":
                widget.stop()
                widget.configure(mode="determinate", maximum=100)
            widget.configure(value=pct)
        self.var_progress.set("{:.0f}%".format(pct))

    def _progress_stop(self):
        for widget in (self.bar, self.busy):
            widget.stop()
            widget.configure(mode="indeterminate", value=0)
        self.var_progress.set("")

    def _res_tick(self):
        try:
            sample = self.res.sample()
            if sample is None:
                self.var_res.set("")
            else:
                cpu, rss, count = sample
                self.var_res.set("CPU {:.0f}%   RAM {}{}".format(
                    cpu, human_bytes(rss),
                    "   {} procs".format(count) if count > 1 else ""))
        except Exception:                                      # noqa: BLE001
            self.var_res.set("")
        self.root.after(RES_TICK_MS, self._res_tick)

    def _auto_tick(self):
        try:
            if (self.var_auto.get() and self.proc is None
                    and self.current_roots()):
                mins = max(5, int_of(self.var_auto_mins, 60) or 60)
                if time.time() - self.last_index_finished >= mins * 60:
                    self.log_line("-- auto re-index --")
                    self.start_index()
            self.update_auto_label()
        except Exception:                                      # noqa: BLE001
            pass
        self.root.after(AUTO_CHECK_MS, self._auto_tick)

    # -- stats + database --------------------------------------------------

    def _build_type_values(self, kinds, dirs):
        """Dropdown entries: groups with counts first (only those with files
        in the index), then every indexed type individually."""
        counts = {e: n for e, n in kinds}
        values = ["All types"]
        if dirs:
            values.append("folders ({:,})".format(dirs))
        for group, exts in TYPE_GROUPS.items():
            n = sum(counts.get(e, 0) for e in exts)
            if n:
                values.append("{} ({:,})".format(group.capitalize(), n))
        values += ["{} ({:,})".format(e.lstrip("."), n) for e, n in kinds]
        return values

    def _refresh_types(self):
        """Rebuild the Type dropdown from the live index. Runs every time the
        dropdown opens, so the list can never be stale or empty."""
        try:
            conn = findex.open_db_ro(self.var_db.get())
            kinds = conn.execute(
                "SELECT ext, COUNT(*) FROM files "
                "WHERE ext IS NOT NULL AND ext != '' "
                "GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 500").fetchall()
            dirs = 0
            try:
                dirs = conn.execute("SELECT COUNT(*) FROM files "
                                    "WHERE is_dir=1").fetchone()[0]
            except sqlite3.OperationalError:
                pass
            conn.close()
            self.type_box["values"] = self._build_type_values(kinds, dirs)
        except Exception:                                      # noqa: BLE001
            pass    # keep whatever the box already lists

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
            dirs = 0
            try:
                dirs = conn.execute("SELECT COUNT(*) FROM files "
                                    "WHERE is_dir=1").fetchone()[0]
            except sqlite3.OperationalError:
                pass    # index from an older findex - healed on next open
            last = findex.get_meta(conn, "last_index")
            summary = findex.get_meta(conn, "last_summary")
            kinds = conn.execute(
                "SELECT ext, COUNT(*) FROM files "
                "WHERE ext IS NOT NULL AND ext != '' "
                "GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 500").fetchall()
            conn.close()
            self.type_box["values"] = self._build_type_values(kinds, dirs)
            size = os.path.getsize(db) if os.path.exists(db) else 0
            text = ("{:,} files{} indexed   |   {} of text   |   database {}  "
                    " |   {:,} errors".format(
                        total - dirs,
                        " + {:,} folders".format(dirs) if dirs else "",
                        findex.human(chars), findex.human(size), errs))
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
            self._ensure_schema()
            self._load_roots()
            self.refresh_stats()
            self.run_search(live=False)

    # -- default save folder -----------------------------------------------

    def save_dir(self):
        """Where copies/moves default to: the chosen folder, else Downloads."""
        return findex.default_save_dir(self.cfg.get("save_dir") or None)

    def choose_save_dir(self):
        folder = filedialog.askdirectory(
            title="Default save folder (where copies and moves go)",
            initialdir=self.save_dir())
        if folder:
            folder = os.path.abspath(folder)
            self.cfg["save_dir"] = folder
            save_setting("save_dir", portable(folder))
            self.var_status.set("Default save folder: " + folder)

    def reset_save_dir(self):
        self.cfg["save_dir"] = ""
        save_setting("save_dir", "")
        self.var_status.set("Default save folder: " + findex.downloads_dir()
                            + " (Downloads)")

    def open_db_folder(self):
        folder = os.path.dirname(os.path.abspath(self.var_db.get())) or HERE
        open_path(folder)

    def show_syntax(self):
        messagebox.showinfo(
            "Search syntax",
            "One box does it all - combine anything, in any order:\n\n"
            "  budget report     both words in the file/folder NAME\n"
            "  *2024*.pdf        * and ? wildcards in the name\n"
            '  "two words"       a name term with the space kept\n'
            "  content:invoice   word inside the file's text\n"
            '  content:"exact phrase"\n'
            "  C:   D:\\Photos    only results under that drive/folder\n"
            "  ext:pdf;docx      only those types\n"
            "  folder:   file:   only folders / only files\n"
            "  !draft            leave out names containing draft\n"
            "  !ext:tmp  !C:\\Windows   ...works on filters too\n\n"
            "Example:  C: content:dan ext:pdf !draft\n\n"
            "content: accepts full FTS5 syntax when quoted at the box level:\n"
            "  content:budg*   prefix    |   content:\"risk policy\"  phrase\n\n"
            "Name results come back best first: exact name, then names\n"
            "starting with the term, then newest. Content results are\n"
            "relevance-ranked.\n\n"
            "Types box: pdf, docx, xlsx - or the folders entry. Blank = all.")

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
            self._kill_proc_tree(self.proc)
        if self.watch_proc is not None:
            self._kill_proc_tree(self.watch_proc)
            self.watch_proc = None
        self.cfg.update({
            "db": portable(self.var_db.get()),
            "roots": [portable(r) for r in self.current_roots()],
            "workers": int_of(self.var_workers),
            "include_cloud": bool(self.var_cloud.get()),
            "ocr": bool(self.var_ocr.get()),
            "dark": bool(self.var_dark.get()),
            "auto_index": bool(self.var_auto.get()),
            "auto_minutes": int_of(self.var_auto_mins, 60) or 60,
            "watch": bool(self.var_watch.get()),
            "limit": max(0, int_of(self.var_limit)),
            "exts": self.var_exts.get(),
            "save_dir": portable(self.cfg.get("save_dir") or ""),
            "geometry": self.root.geometry(),
        })
        save_settings(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="findex_gui",
                                 description="Desktop front end for findex.")
    ap.add_argument("--db", default=None, help="index database path")
    # macOS hands a launched .app a "-psn_0_12345" process-serial argument.
    # A strict parser exits(2) on it, which in a windowed build means the app
    # closes instantly with no visible reason - so drop it, and ignore any
    # other stray argument rather than refusing to start.
    if argv is None:
        argv = sys.argv[1:]
    argv = [a for a in argv if not a.startswith("-psn_")]
    args, _unknown = ap.parse_known_args(argv)

    settings = load_settings()
    if args.db:
        settings["db"] = os.path.abspath(args.db)

    root = tk.Tk()
    FindexApp(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
