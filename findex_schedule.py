#!/usr/bin/env python3
"""
findex_schedule - keep the index fresh in the background, with nothing for
the user to set up by hand.

    findex schedule --every 6h      register a refresh every 6 hours
    findex schedule --off           remove it
    findex schedule --status        is it registered, how often, last run
    findex schedule --show          print what would be registered

The refresh is `findex index` with no folders - the ones remembered in the
database - run by the operating system's own scheduler, so it happens
whether or not the desktop app is open:

    Windows   a per-user task in Task Scheduler called "findex index refresh"
              (Task Scheduler Library, top level). Registered with schtasks
              from an XML definition, which - unlike the /SC /TR switches -
              has no 261-character limit on the command, and lets the task
              say: never start a second copy while one is running, catch up
              a missed run when the PC was off, run on battery, run at
              below-normal priority. No admin rights: it runs as the current
              user, when that user is logged on (running while logged out
              would need the account password stored with the task).
    macOS     a LaunchAgent, ~/Library/LaunchAgents/uk.lowther.findex.refresh
              .plist, with a StartInterval; loaded with launchctl. Runs while
              the user is logged in, which on a Mac is nearly always.
    Linux     not offered (a systemd user timer would do it; the toggle says
              "not supported on this OS").

Two runs can never overlap: findex index takes a lock beside the database
(see findex.acquire_index_lock), and the Windows task additionally refuses
to start while an instance is running. The scheduled run is exactly the
command shown by --show, so it can be tried by hand. Its output goes to
findex-refresh.log beside the database (the Windows run has no console; the
Mac one is pointed there by the plist).

Everything that decides WHAT to register is a pure function - the command
line, the task XML, the plist - so it can be checked on any machine; only
enable/disable/status talk to the scheduler.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time

import findex

TASK_NAME = "findex index refresh"          # Windows Task Scheduler name
LABEL = "uk.lowther.findex.refresh"         # macOS launchd label
INTERVALS = (1, 3, 6, 12, 24)               # hours offered by the app
DEFAULT_HOURS = 6


class ScheduleError(Exception):
    """Something the scheduler said no to, in words for the user."""


def supported(platform=None):
    p = platform or sys.platform
    return p.startswith("win") or p == "darwin"


def unsupported_note(platform=None):
    return ("Background refresh is not supported on this OS - it uses "
            "Task Scheduler on Windows and launchd on macOS.")


# ----------------------------------------------------------------------------
# What to register (pure)
# ----------------------------------------------------------------------------

def launcher():
    """How findex is started headlessly on THIS machine: the standalone
    executable's own `engine` mode in a frozen build, otherwise this
    interpreter running findex.py. On Windows the console-less pythonw.exe
    is preferred when it sits beside the python.exe in use, so a refresh
    does not flash a black window every few hours; its output has nowhere
    to go, which findex.main() handles by writing findex-refresh.log."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "engine"]
    exe = sys.executable or "python"
    if os.name == "nt":
        base = os.path.basename(exe).lower()
        if base.startswith("python") and not base.startswith("pythonw"):
            twin = os.path.join(os.path.dirname(exe),
                                base.replace("python", "pythonw", 1))
            if os.path.exists(twin):
                exe = twin
    return [exe, os.path.realpath(findex.__file__)]


def refresh_command(db, launcher_cmd=None, extra=(), default_db=None):
    """The exact command a scheduled refresh runs: the launcher, --db when
    the database is not the default one beside findex, `index` with no
    folders (the remembered ones), and any extra index options (--ocr,
    --include-cloud, --workers N) the caller wants carried over."""
    cmd = list(launcher_cmd if launcher_cmd is not None else launcher())
    db = os.path.abspath(db)
    default = os.path.abspath(default_db or findex.DEFAULT_DB)
    if os.path.normcase(db) != os.path.normcase(default):
        cmd += ["--db", db]
    cmd.append("index")
    cmd += list(extra or ())
    return cmd


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def task_xml(command, hours, start=None, user=None, workdir=None):
    """Task Scheduler definition for `schtasks /Create /XML`. `command` is
    the argv list; `start` an ISO local time for the first run (default:
    one interval from now); `user` the account (DOMAIN\\name) the task runs
    as - the caller when omitted."""
    hours = int(hours)
    if hours < 1:
        raise ValueError("interval must be at least one hour")
    if start is None:
        start = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(time.time() + hours * 3600))
    exe, args = command[0], subprocess.list2cmdline(command[1:])
    principal = "      <LogonType>InteractiveToken</LogonType>\n"
    if user:
        principal = ("      <UserId>{}</UserId>\n".format(_xml_escape(user))
                     + principal)
    workdir_xml = ("      <WorkingDirectory>{}</WorkingDirectory>\n".format(
        _xml_escape(workdir)) if workdir else "")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>findex: refreshes the file search index every "
        "{h} hour(s) - the remembered folders, new and changed files only. "
        "Turned on and off from findex (Index tab, or: findex schedule "
        "--off).</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <TimeTrigger>\n"
        "      <Repetition>\n"
        "        <Interval>PT{h}H</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "      <StartBoundary>{start}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "{principal}"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>{exe}</Command>\n"
        "      <Arguments>{args}</Arguments>\n"
        "{workdir}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    ).format(h=hours, start=_xml_escape(start), principal=principal,
             exe=_xml_escape(exe), args=_xml_escape(args), workdir=workdir_xml)


def plist_xml(command, hours, log_dir, label=LABEL, workdir=None):
    """launchd agent definition: run `command` every `hours` hours, output
    to findex-refresh.log in log_dir, at background priority."""
    hours = int(hours)
    if hours < 1:
        raise ValueError("interval must be at least one hour")
    log = os.path.join(log_dir, findex.REFRESH_LOG)
    data = {
        "Label": label,
        "ProgramArguments": [str(a) for a in command],
        "StartInterval": hours * 3600,
        "RunAtLoad": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "EnvironmentVariables": {"PYTHONIOENCODING": "utf-8"},
    }
    if workdir:
        data["WorkingDirectory"] = workdir
    return plistlib.dumps(data, sort_keys=False).decode("utf-8")


def plist_path(label=LABEL, home=None):
    return os.path.join(home or os.path.expanduser("~"), "Library",
                        "LaunchAgents", label + ".plist")


def parse_hours(text):
    """'6h', '6', '24 hours' -> 6, 6, 24. Whole hours only."""
    m = re.match(r"^\s*(\d+)\s*(h|hr|hrs|hour|hours)?\s*$", str(text), re.I)
    if not m:
        raise ValueError("an interval in hours, please - e.g. 6h or 24h")
    hours = int(m.group(1))
    if hours < 1:
        raise ValueError("the interval must be at least one hour")
    return hours


def hours_from_task_xml(xml):
    """The repetition interval from a Task Scheduler XML export, or None."""
    m = re.search(r"<Interval>P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?"
                  r"</Interval>", xml or "")
    if not m:
        return None
    days, hours, mins = (int(x or 0) for x in m.groups())
    total = days * 24 + hours + mins / 60.0
    return int(total) if total >= 1 and total == int(total) else total or None


def command_from_task_xml(xml):
    exe = re.search(r"<Command>(.*?)</Command>", xml or "", re.S)
    args = re.search(r"<Arguments>(.*?)</Arguments>", xml or "", re.S)
    if not exe:
        return ""
    import html
    return subprocess.list2cmdline([html.unescape(exe.group(1).strip())]) + (
        " " + html.unescape(args.group(1).strip()) if args else "")


# ----------------------------------------------------------------------------
# Talking to the scheduler
# ----------------------------------------------------------------------------

def _decode(data):
    """Console tool output as text. schtasks may answer in the console
    code page or - for /Query /XML - in UTF-16, BOM or not; a wrong guess
    would leave the task looking registered with no interval or command,
    and the app re-registering it on every close."""
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace").lstrip("\ufeff")
    if b"\x00" in data[:256]:
        return data.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    import locale
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _run(cmd, input_text=None):
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000     # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60,
                           input=(input_text.encode("utf-8")
                                  if input_text is not None else None),
                           **kwargs)
    except FileNotFoundError:
        raise ScheduleError("{} was not found on this machine".format(cmd[0]))
    except subprocess.TimeoutExpired:
        raise ScheduleError("{} did not answer".format(cmd[0]))
    return r.returncode, _decode(r.stdout), _decode(r.stderr)


def _windows_user():
    name = os.environ.get("USERNAME")
    if not name:
        return None
    domain = os.environ.get("USERDOMAIN")
    return "{}\\{}".format(domain, name) if domain else name


def _log_dir(db):
    return os.path.dirname(os.path.abspath(db))


def enable(db, hours, extra=(), platform=None):
    """Register (or re-register with a new interval) the background
    refresh. Returns a one-line description of what was done."""
    platform = platform or sys.platform
    hours = int(hours)
    command = refresh_command(db, extra=extra)
    if platform.startswith("win"):
        xml = task_xml(command, hours, user=_windows_user(),
                       workdir=_log_dir(db))
        fd, tmp = tempfile.mkstemp(prefix="findex-task-", suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as fh:
                fh.write(xml)
            rc, out, err = _run(["schtasks", "/Create", "/F", "/TN", TASK_NAME,
                                 "/XML", tmp])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if rc != 0:
            raise ScheduleError("Task Scheduler refused the task: {}".format(
                (err or out).strip() or "exit {}".format(rc)))
        return ("Background refresh on: every {} h, as the Task Scheduler "
                "task \"{}\"".format(hours, TASK_NAME))
    if platform == "darwin":
        path = plist_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # a loaded agent keeps its old definition until it is booted out
        _launchctl_unload(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(plist_xml(command, hours, _log_dir(db),
                               workdir=_log_dir(db)))
        uid = os.getuid()
        rc, out, err = _run(["launchctl", "bootstrap", "gui/{}".format(uid),
                             path])
        if rc != 0:
            rc, out, err = _run(["launchctl", "load", "-w", path])
        if rc != 0:
            try:
                os.remove(path)
            except OSError:
                pass
            raise ScheduleError("launchctl refused the agent: {}".format(
                (err or out).strip() or "exit {}".format(rc)))
        return ("Background refresh on: every {} h, as the LaunchAgent {}"
                .format(hours, path))
    raise ScheduleError(unsupported_note(platform))


def _launchctl_unload(path):
    uid = os.getuid()
    rc, out, err = _run(["launchctl", "bootout", "gui/{}/{}".format(uid, LABEL)])
    if rc != 0 and os.path.exists(path):
        rc, out, err = _run(["launchctl", "unload", "-w", path])
    return rc == 0


def disable(platform=None):
    """Remove the background refresh. Fine to call when there is none."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        rc, out, err = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        # a failure is only a failure if the task is still there (the usual
        # non-zero exit is "no such task", in whichever language Windows
        # speaks)
        if rc != 0 and _task_query()[0] == 0:
            raise ScheduleError("Task Scheduler would not remove the "
                                "task: {}".format((err or out).strip()))
        return "Background refresh off - the Task Scheduler task is gone"
    if platform == "darwin":
        path = plist_path()
        _launchctl_unload(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return "Background refresh off - the LaunchAgent is gone"
    raise ScheduleError(unsupported_note(platform))


def _task_query():
    return _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML", "ONE"])


def status(platform=None):
    """{'supported', 'registered', 'hours', 'command', 'detail'} - what the
    scheduler currently holds. Never raises; an unreachable scheduler reads
    as not registered with the reason in 'detail'."""
    platform = platform or sys.platform
    st = {"supported": supported(platform), "registered": False,
          "hours": None, "command": "", "detail": ""}
    if not st["supported"]:
        st["detail"] = unsupported_note(platform)
        return st
    try:
        if platform.startswith("win"):
            rc, out, err = _task_query()
            if rc != 0:
                st["detail"] = "no Task Scheduler task"
                return st
            st["registered"] = True
            st["hours"] = hours_from_task_xml(out)
            st["command"] = command_from_task_xml(out)
            st["detail"] = "Task Scheduler task \"{}\"".format(TASK_NAME)
        else:
            path = plist_path()
            if not os.path.exists(path):
                st["detail"] = "no LaunchAgent"
                return st
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
            st["hours"] = int(data.get("StartInterval", 0)) // 3600 or None
            st["command"] = subprocess.list2cmdline(
                data.get("ProgramArguments", []))
            rc, out, err = _run(["launchctl", "print",
                                 "gui/{}/{}".format(os.getuid(), LABEL)])
            st["registered"] = rc == 0
            st["detail"] = ("LaunchAgent " + path if rc == 0 else
                            "LaunchAgent file present but not loaded - "
                            "turn the refresh off and on again")
    except ScheduleError as exc:
        st["detail"] = str(exc)
    except Exception as exc:                                   # noqa: BLE001
        st["detail"] = "could not read the scheduler: {}".format(exc)
    return st


def describe(db, st=None):
    """One line for the app's Index tab / --status: registered or not, the
    interval, and when the index was last updated by any run."""
    st = st or status()
    if not st["supported"]:
        return "Background refresh: not supported on this OS"
    if not st["registered"]:
        return "Background refresh: off"
    last = None
    try:
        conn = findex.open_db_ro(db)
        last = findex.get_meta(conn, "last_index")
        conn.close()
    except Exception:                                          # noqa: BLE001
        pass
    when = ""
    if last:
        try:
            when = ", index last updated " + time.strftime(
                "%H:%M %a", time.localtime(float(last)))
        except (TypeError, ValueError, OSError):
            pass
    every = ("every {} h".format(st["hours"]) if st["hours"]
             else "registered")
    return "Background refresh: {}{}".format(every, when)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _extra_from(args):
    extra = []
    if getattr(args, "ocr", False):
        extra.append("--ocr")
    if getattr(args, "include_cloud", False):
        extra.append("--include-cloud")
    if getattr(args, "workers", None):
        extra += ["--workers", str(args.workers)]
    return extra


def cmd_schedule(args):
    try:
        if args.off:
            print(disable())
            return 0
        if args.show:
            hours = parse_hours(args.every) if args.every else DEFAULT_HOURS
            command = refresh_command(args.db, extra=_extra_from(args))
            print("command : " + subprocess.list2cmdline(command))
            print("every   : {} h".format(hours))
            if sys.platform.startswith("win"):
                print("task    : {}\n".format(TASK_NAME))
                print(task_xml(command, hours, user=_windows_user(),
                               workdir=_log_dir(args.db)))
            elif sys.platform == "darwin":
                print("agent   : {}\n".format(plist_path()))
                print(plist_xml(command, hours, _log_dir(args.db),
                                workdir=_log_dir(args.db)))
            else:
                print("note    : " + unsupported_note())
            return 0
        if args.every:
            hours = parse_hours(args.every)
            print(enable(args.db, hours, extra=_extra_from(args)))
            print("First run in {} h; output in {}".format(
                hours, os.path.join(_log_dir(args.db), findex.REFRESH_LOG)))
            return 0
    except (ScheduleError, ValueError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    st = status()
    print(describe(args.db, st))
    if st["command"]:
        print("  runs: " + st["command"])
    if st["detail"]:
        print("  " + st["detail"])
    return 0


def add_commands(sub):
    p = sub.add_parser("schedule", help="keep the index fresh in the "
                       "background: a Task Scheduler task (Windows) or "
                       "LaunchAgent (macOS) that runs 'findex index'")
    p.add_argument("--every", metavar="HOURS",
                   help="register a refresh this often, e.g. 6h or 24h "
                        "(re-registers if one exists)")
    p.add_argument("--off", action="store_true", help="remove the refresh")
    p.add_argument("--status", action="store_true",
                   help="show whether it is registered (the default)")
    p.add_argument("--show", action="store_true",
                   help="print the command and task definition, register "
                        "nothing")
    p.add_argument("--ocr", action="store_true",
                   help="the scheduled run OCRs scans and images too")
    p.add_argument("--include-cloud", action="store_true",
                   help="the scheduled run reads OneDrive online-only files")
    p.add_argument("--workers", type=int, default=None,
                   help="worker processes for the scheduled run")
    p.set_defaults(func=cmd_schedule)


if __name__ == "__main__":
    sys.exit(findex.main())
