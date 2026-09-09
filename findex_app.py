#!/usr/bin/env python3
"""Entry point for the OPTIONAL standalone build of findex (see
build-exe.bat / build-app.command). The normal way to run findex is
findex-gui.bat / findex-gui.command - this file changes nothing about that.

    findex             opens the desktop app
    findex engine ...  runs the indexing/search engine (used internally)
    findex selftest    prints what the build can and cannot do, and exits

A windowed build has nowhere to print to: if it fails while starting up, the
window never appears and macOS or Windows simply closes it. So anything that
goes wrong here is written to findex-crash.log beside the app and, where Tk
still works, shown in a dialog.
"""

import os
import sys
import traceback
from multiprocessing import freeze_support


def _crash_log_path():
    """Somewhere writable to record a start-up failure."""
    try:
        import findex
        folder = findex.HERE
        if os.access(folder, os.W_OK):
            return os.path.join(folder, "findex-crash.log")
    except Exception:
        pass
    import tempfile
    return os.path.join(tempfile.gettempdir(), "findex-crash.log")


def _report(exc):
    """Record a start-up failure and, if possible, show it."""
    text = "".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__))
    path = _crash_log_path()
    try:
        import datetime
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n===== {} =====\npython {}\nexecutable {}\n{}\n".format(
                datetime.datetime.now().isoformat(timespec="seconds"),
                sys.version.replace("\n", " "), sys.executable, text))
    except Exception:
        path = "(could not be written)"
    if sys.stderr:                     # None in a windowed build
        try:
            sys.stderr.write(text)
        except Exception:
            pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "findex could not start",
            "{}\n\nFull details: {}".format(text.strip().splitlines()[-1], path))
        root.destroy()
    except Exception:
        pass


def selftest():
    """Report what this build actually has. Run by the build scripts.

    A windowed build has no console, so the report is also written to
    findex-selftest.txt beside the app - that file is the thing to send on
    when a build misbehaves.
    """
    lines = []

    def print(*args):                      # noqa: A001 - capture as well as show
        text = " ".join(str(a) for a in args)
        lines.append(text)
        sys.__stdout__ and sys.__stdout__.write(text + "\n")

    print("executable : {}".format(sys.executable))
    print("python     : {}".format(sys.version.replace("\n", " ")))
    print("frozen     : {}".format(bool(getattr(sys, "frozen", False))))
    ok = True

    try:
        import tkinter
        root = tkinter.Tk()
        print("tk         : {} (patch {})".format(
            tkinter.TkVersion, root.tk.call("info", "patchlevel")))
        root.destroy()
    except Exception as exc:
        ok = False
        print("tk         : FAILED - {}".format(exc))

    try:
        import findex
        print("engine     : ok, data folder {}".format(findex.HERE))
        print("            writable: {}".format(os.access(findex.HERE, os.W_OK)))
        for name, flag in (("pymupdf (PDF text)", "HAVE_FITZ"),
                           ("mutagen (media tags)", "HAVE_MUTAGEN"),
                           ("extract-msg (Outlook)", "HAVE_MSG")):
            print("  {:<24} {}".format(
                name, "yes" if getattr(findex, flag, False) else "no"))
    except Exception as exc:
        ok = False
        print("engine     : FAILED - {}".format(exc))

    for label, mod in (("watchdog (live updates)", "watchdog.observers"),
                       ("Vision (macOS OCR)", "Vision")):
        if mod == "Vision" and sys.platform != "darwin":
            continue
        try:
            __import__(mod)
            print("  {:<24} yes".format(label))
        except Exception:
            print("  {:<24} no".format(label))

    try:
        import findex_gui  # noqa: F401
        print("desktop app: imports cleanly")
    except Exception as exc:
        ok = False
        print("desktop app: FAILED - {}".format(exc))

    print("RESULT: {}".format("ok" if ok else "PROBLEMS FOUND"))

    try:
        import findex
        report = os.path.join(findex.HERE, "findex-selftest.txt")
    except Exception:
        report = "findex-selftest.txt"
    try:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if ok else 1


def main():
    freeze_support()    # lets worker processes start inside a frozen build

    # macOS gives a Finder-launched .app an extra "-psn_0_12345" argument.
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]

    if argv and argv[0] == "engine":
        import findex
        return findex.main(argv[1:])
    if argv and argv[0] == "selftest":
        return selftest()

    try:
        import findex_gui
        return findex_gui.main(argv)
    except SystemExit:
        raise
    except BaseException as exc:      # noqa: BLE001 - last chance to say why
        _report(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
