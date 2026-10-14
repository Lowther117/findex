#!/usr/bin/env python3
"""
findex_volumes - follow a drive when its letter (or mount point) changes.

Windows hands out drive letters in the order things are plugged in, so the
external drive that was D: last week is E: today, and every path the index
holds for it is wrong by one letter. Re-indexing a whole drive to fix a
letter is absurd, so each remembered root is stamped with the identity of
the VOLUME it lives on - the filesystem's own serial number on Windows, the
volume UUID on macOS, both of which travel with the drive to any machine -
and the mount point it had at the time. Before a run (and when the desktop
app opens) any root whose path is gone is looked for by volume identity
among the drives connected right now; when it has simply moved, every path
under it is rewritten in one transaction and nothing is re-read.

What is rewritten: every text column of every ordinary table in the
database that holds a path under the old location - files, roots, the
journal, rename and organise batches, summaries - so the tool modules need
no knowledge of this. The full-text and trigram tables key on row ids, not
paths, and are untouched. A journal row ("remapped") records each move.

This module imports nothing from findex, so findex can import it at the
top: PyInstaller then compiles it into the standalone builds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# Columns the remap never touches even when they are text, and tables it
# never looks at (FTS5 virtual tables and their shadow tables - their path
# is the row id - plus SQLite's own).
_SKIP_COLS = {"event", "source", "status", "kind", "ext", "name", "op",
              "error", "key", "format", "volume_id", "volume_label",
              "mount"}
JOURNAL_EVENT = "remapped"

_cache = {}      # mount point -> (volume_id, label), for this process


# ----------------------------------------------------------------------------
# What is connected, and what each root sits on
# ----------------------------------------------------------------------------

def _sep_of(path):
    return "\\" if "\\" in path or (len(path) > 1 and path[1] == ":") else "/"


def list_mounts():
    """Mount points of every volume connected right now: drive roots on
    Windows ("C:\\", "D:\\"), "/" plus /Volumes/* on macOS, the usual
    media folders on Linux."""
    out = []
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            bitmask = k32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    root = "{}:\\".format(chr(65 + i))
                    # 2 removable, 3 fixed, 6 RAM disk. Not CD-ROM (an
                    # empty tray is probed slowly every time) and not a
                    # mapped network letter: asking a share that has gone
                    # away for its serial blocks for many seconds, and
                    # a share's letter is set by hand anyway.
                    if k32.GetDriveTypeW(root) in (2, 3, 6):
                        out.append(root)
        except Exception:                                      # noqa: BLE001
            pass
        return out
    out.append("/")
    if sys.platform == "darwin":
        folders = ["/Volumes"]
    else:
        home = os.path.basename(os.path.expanduser("~"))
        folders = ["/mnt", "/media", os.path.join("/media", home),
                   os.path.join("/run/media", home)]
    for folder in folders:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            p = os.path.join(folder, name)
            if (not name.startswith(".") and os.path.isdir(p)
                    and os.path.realpath(p) != "/" and os.path.ismount(p)):
                out.append(p)
    return out


def _win_volume(root):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    maxlen = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    # SEM_FAILCRITICALERRORS: an empty card reader must not pop up an
    # "insert a disk" dialog
    old = k32.SetErrorMode(1)
    try:
        ok = k32.GetVolumeInformationW(
            root, label, 261, ctypes.byref(serial), ctypes.byref(maxlen),
            ctypes.byref(flags), fs, 261)
    finally:
        k32.SetErrorMode(old)
    if not ok or not serial.value:
        return None, None
    return "win:{:08X}".format(serial.value), label.value or ""


def _mac_volume(mount):
    import plistlib
    r = subprocess.run(["diskutil", "info", "-plist", mount],
                       capture_output=True, timeout=20)
    if r.returncode != 0 or not r.stdout:
        return None, None
    info = plistlib.loads(r.stdout)
    uuid = info.get("VolumeUUID") or info.get("DiskUUID")
    if not uuid:
        return None, None
    return "mac:" + str(uuid).upper(), str(info.get("VolumeName") or "")


def _linux_volume(mount):
    r = subprocess.run(["findmnt", "-no", "UUID,LABEL", "--target", mount],
                       capture_output=True, text=True, timeout=10)
    parts = (r.stdout or "").split(None, 1)
    if r.returncode != 0 or not parts or not parts[0]:
        return None, None
    return "lin:" + parts[0].upper(), parts[1].strip() if len(parts) > 1 \
        else ""


def volume_info(mount):
    """(volume_id, label) for the volume mounted at `mount`, or
    (None, None) when it cannot be told. Cached for this process."""
    if mount in _cache:
        return _cache[mount]
    vid, label = None, None
    try:
        if os.name == "nt":
            vid, label = _win_volume(mount)
        elif sys.platform == "darwin":
            vid, label = _mac_volume(mount)
        else:
            vid, label = _linux_volume(mount)
    except Exception:                                          # noqa: BLE001
        vid, label = None, None
    _cache[mount] = (vid, label)
    return vid, label


def mount_of(path):
    """The mount point the path lives on: its drive root on Windows (None
    for a UNC share), the nearest mounted ancestor elsewhere."""
    path = os.path.abspath(path)
    if os.name == "nt":
        drive, _ = os.path.splitdrive(path)
        if len(drive) == 2 and drive[1] == ":":
            return drive.upper() + "\\"
        return None
    p = path
    while True:
        try:
            if os.path.ismount(p):
                return p
        except OSError:
            pass
        parent = os.path.dirname(p)
        if parent == p:
            return "/"
        p = parent


def _norm(p):
    """Paths as stored are already absolute; normcase only, so a database
    built on Windows compares the same way when opened on a Mac."""
    return os.path.normcase(p).rstrip("\\/")


def _rel(path, mount):
    """What is left of `path` once its mount point is taken off."""
    rest = path[len(mount.rstrip("\\/")):]
    return rest.lstrip("\\/")


def connected():
    """{volume_id: (mount, label)} for every connected volume findex can
    identify."""
    out = {}
    for m in list_mounts():
        vid, label = volume_info(m)
        if vid and vid not in out:
            out[vid] = (m, label)
    return out


# ----------------------------------------------------------------------------
# The database side
# ----------------------------------------------------------------------------

def ensure_columns(conn):
    """roots gains volume_id, volume_label and mount; older files are
    stamped the next time their location is seen."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(roots)")}
    for col in ("volume_id", "volume_label", "mount"):
        if col not in have:
            conn.execute("ALTER TABLE roots ADD COLUMN {} TEXT".format(col))


def stamp(conn, roots=None, commit=True, force=False):
    """Record which volume each root sits on, for those present right now.
    roots=None stamps every remembered root. A root already stamped with a
    DIFFERENT volume is left alone (another drive has its letter - see
    plan()) unless force=True, which is for `findex roots --restamp` after
    a drive has been reformatted or replaced on purpose. Returns how many
    changed."""
    ensure_columns(conn)
    if roots is None:
        rows = conn.execute("SELECT path, volume_id, mount FROM roots").fetchall()
    else:
        want = {_norm(r) for r in roots}
        rows = [r for r in conn.execute(
            "SELECT path, volume_id, mount FROM roots") if _norm(r[0]) in want]
    present = {_norm(m) for m in list_mounts()}
    changed = 0
    for path, old_vid, old_mount in rows:
        if not os.path.isdir(path):
            continue
        mount = mount_of(path)
        if not mount or _norm(mount) not in present:
            continue
        vid, label = volume_info(mount)
        if not vid:
            continue
        if old_vid and vid != old_vid and not force:
            continue
        if vid != old_vid or _norm(mount) != _norm(old_mount or ""):
            conn.execute("UPDATE roots SET volume_id=?, volume_label=?, "
                         "mount=? WHERE path=?", (vid, label, mount, path))
            changed += 1
    if changed and commit:
        conn.commit()
    return changed


_TOP = "\U0010ffff"      # sorts after every character: the end of a prefix range


def _path_columns(conn):
    """[(table, column)] for every text column the remap should rewrite:
    every ordinary table, every TEXT (or untyped) column, bar the few that
    hold words rather than paths. A column that never holds a path is
    simply never matched - the cost is one indexed range scan."""
    virtual = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND sql LIKE 'CREATE VIRTUAL TABLE%'")}
    cols = []
    for (table, sql) in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table in virtual or any(table.startswith(v + "_") for v in virtual):
            continue                            # FTS tables and their shadows
        for _cid, name, ctype, _nn, _dflt, _pk in conn.execute(
                "PRAGMA table_info({})".format(_q(table))):
            if name in _SKIP_COLS:
                continue
            if (ctype or "").upper() not in ("TEXT", "VARCHAR", "STRING",
                                             "CHAR", "CLOB", ""):
                continue
            cols.append((table, name))
    return cols


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _prefix(path):
    """'D:\\' -> 'D:\\', 'D:\\Photos' -> 'D:\\Photos\\', '/x' -> '/x/'."""
    return path.rstrip("\\/") + _sep_of(path)


def _move_prefix(cur, cols, old, new):
    """Rewrite old -> new at the start of every path under old (and old
    itself), in every (table, column) given. Returns rows touched."""
    old_pfx, new_pfx = _prefix(old), _prefix(new)
    old_bare, new_bare = old.rstrip("\\/"), new.rstrip("\\/")
    n = 0
    for table, col in cols:
        t, c = _q(table), _q(col)
        # a range on the column (index-friendly, case-exact) for everything
        # beneath the location, plus the location itself written without
        # its trailing separator (how a root or a journal row names it)
        cur.execute(
            "UPDATE {t} SET {c} = ? || substr({c}, ?) WHERE {c} >= ? AND "
            "{c} < ?".format(t=t, c=c),
            (new_pfx, len(old_pfx) + 1, old_pfx, old_pfx + _TOP))
        n += cur.rowcount
        if old_bare and new_bare:
            cur.execute("UPDATE {t} SET {c} = ? WHERE {c} = ?".format(
                t=t, c=c), (new_bare, old_bare))
            n += cur.rowcount
    return n


def _drop_under(cur, pfx_root):
    """Delete index rows under a location (files + their text), and roots
    entries there. Returns files removed."""
    pfx, bare = _prefix(pfx_root), pfx_root.rstrip("\\/")
    ids = [r[0] for r in cur.execute(
        "SELECT id FROM files WHERE (path >= ? AND path < ?) OR path = ?",
        (pfx, pfx + _TOP, bare))]
    for s in range(0, len(ids), 500):
        chunk = ids[s:s + 500]
        marks = ",".join("?" * len(chunk))
        cur.execute("DELETE FROM docs WHERE rowid IN ({})".format(marks), chunk)
        cur.execute("DELETE FROM files WHERE id IN ({})".format(marks), chunk)
    cur.execute("DELETE FROM roots WHERE (path >= ? AND path < ?) OR path = ?",
                (pfx, pfx + _TOP, bare))
    return len(ids)


def _fix_meta(cur, old, new):
    """meta holds two things by path: last_roots (one per line) and the
    extract_version:<prefix> keys, by normcase prefix."""
    try:
        row = cur.execute("SELECT value FROM meta WHERE key='last_roots'"
                          ).fetchone()
    except Exception:                                          # noqa: BLE001
        return
    if row and row[0]:
        lines = row[0].split("\n")
        fixed = [new if _norm(ln) == _norm(old) else ln for ln in lines]
        if fixed != lines:
            cur.execute("UPDATE meta SET value=? WHERE key='last_roots'",
                        ("\n".join(fixed),))
    old_key = "extract_version:" + _norm(old) + os.sep
    new_key = "extract_version:" + _norm(new) + os.sep
    row = cur.execute("SELECT value FROM meta WHERE key=?", (old_key,)
                      ).fetchone()
    if row:
        cur.execute("DELETE FROM meta WHERE key=?", (new_key,))
        cur.execute("UPDATE meta SET key=? WHERE key=?", (new_key, old_key))


def plan(conn, mounted=None):
    """Work out what has moved. Returns (moves, away, missing):
    moves   [(old_root, new_root, label)] - the volume is connected, at a
            different place
    away    [old_root] - the path exists but it is ANOTHER volume sitting
            there now; this root's own volume is not connected
    missing [old_root] - not connected, nowhere to be found (nothing to do)
    A root that is where it was, on the volume it was on, is in none."""
    ensure_columns(conn)
    if mounted is None:
        mounted = connected()
    present = {_norm(m) for m in list_mounts()}
    moves, away, missing = [], [], []
    rows = conn.execute(
        "SELECT path, volume_id, mount FROM roots ORDER BY added, path"
    ).fetchall()
    for path, vid, old_mount in rows:
        here = os.path.isdir(path)
        if not vid:
            if not here:
                missing.append(path)
            continue                    # never stamped: nothing to go on
        now_mount = mount_of(path) if here else None
        if now_mount and _norm(now_mount) not in present:
            now_mount = None            # a network letter: never probed
        now_vid = volume_info(now_mount)[0] if now_mount else None
        if here and now_vid in (vid, None):
            continue                    # where it was, on the drive it was
        if vid not in mounted:
            (away if here else missing).append(path)
            continue
        new_mount, label = mounted[vid]
        base = old_mount or (mount_of(path) if os.name == "nt" else None)
        if not base:
            missing.append(path)        # cannot tell which part was the drive
            continue
        if _norm(new_mount) == _norm(base):
            missing.append(path)        # same drive, the folder itself is gone
            continue
        rel = _rel(path, base)
        new_path = _prefix(new_mount) + rel if rel else new_mount
        moves.append((path, new_path, label or ""))
    return moves, away, missing


def apply(conn, moves):
    """Rewrite the index for these moves, as one transaction. Returns
    {old_root: (new_root, rows_rewritten, stale_rows_dropped)}."""
    if not moves:
        return {}
    ensure_columns(conn)
    cols = _path_columns(conn)
    cur = conn.cursor()
    cur.execute("BEGIN")
    out = {}
    try:
        temps = []
        # 1. everything that is moving goes to a private temporary prefix
        #    first, so two drives that swapped letters cannot overwrite one
        #    another part-way through
        for i, (old, new, _label) in enumerate(moves):
            tmp = "\x01findex-remap-{}{}".format(i, _sep_of(old))
            _move_prefix(cur, cols, old, tmp)
            temps.append((old, new, tmp))
        # 2. whatever is still sitting at a destination belongs to a drive
        #    that is NOT connected (its own volume would have moved out in
        #    step 1) and whose letter has been reassigned: it goes, or the
        #    two sets would be mixed under one path. All of the clearing
        #    happens before any move lands, so a destination inside another
        #    (E:\ and E:\Photos) cannot clear rows that have just arrived.
        dropped = {old: _drop_under(cur, new) for old, new, _tmp in temps}
        for old, new, tmp in temps:
            n = _move_prefix(cur, cols, tmp, new)
            _fix_meta(cur, old, new)
            out[old] = (new, n, dropped[old])
        now = time.time()
        for old, new, _tmp in temps:
            cur.execute(
                "INSERT INTO journal(ts, event, path, old_path, size, is_dir, "
                "source) VALUES (?, ?, ?, ?, NULL, 1, 'remap')",
                (now, JOURNAL_EVENT, new, old))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # the new mount points, labels and ids for the moved roots
    present = {_norm(m) for m in list_mounts()}
    for old, (new, _n, _d) in out.items():
        m = mount_of(new)
        if m and _norm(m) in present:
            vid, label = volume_info(m)
            if vid:
                conn.execute("UPDATE roots SET volume_id=?, volume_label=?, "
                             "mount=? WHERE path=?", (vid, label, m, new))
    conn.commit()
    return out


def remap(conn, out=None, mounted=None):
    """The whole thing: stamp what is present, find what moved, rewrite
    it. Prints a line per move to `out` when given. Returns (moved, away)
    where moved = {old_root: new_root} and away = [root paths that exist
    but are another drive's now - do NOT index them this run]."""
    try:
        stamp(conn)
        moves, away, _missing = plan(conn, mounted)
        done = apply(conn, moves) if moves else {}
        # a root whose letter a moved drive has just taken over was dropped
        # by apply() - it is no longer a root, so not "away" either
        dests = [_prefix(new) for _o, new, _l in moves]
        away = [p for p in away if not any(
            _norm(p) == _norm(d) or _norm(p).startswith(_norm(d) + _sep_of(d))
            for d in dests)]
    except Exception as exc:                                   # noqa: BLE001
        if out is not None:
            out.write("drive check skipped: {}: {}\n".format(
                type(exc).__name__, exc))
        return {}, []
    if out is not None:
        for old, new, label in moves:
            new_root, n, dropped = done.get(old, (new, 0, 0))
            out.write("Drive moved: {} is now {}{} - {:,} index entries "
                      "followed it{}\n".format(
                          old, new_root,
                          " ({})".format(label) if label else "", n,
                          ", {:,} stale entries for a drive that is not "
                          "connected were dropped from {}".format(
                              dropped, new_root) if dropped else ""))
        for path in away:
            out.write("Not the same drive: {} exists but it is a different "
                      "volume now - the one that was indexed there is not "
                      "connected, so it is left alone\n".format(path))
    return {old: new for old, (new, _n, _d) in done.items()}, away


def translate(moved, roots):
    """Given {old: new} from remap(), the roots a caller passed in with any
    moved ones replaced."""
    if not moved:
        return list(roots)
    by_norm = {_norm(o): n for o, n in moved.items()}
    return [by_norm.get(_norm(os.path.abspath(r)), r) for r in roots]


def describe_roots(conn):
    """[(path, last_index, label, state)] for a listing; state is one of
    'ok', 'not connected', 'connected elsewhere' (remap() would move it),
    'different drive here' (another volume has its letter)."""
    ensure_columns(conn)
    mounted = connected()
    present = {_norm(m) for m in list_mounts()}
    rows = conn.execute(
        "SELECT path, last_index, volume_id, volume_label FROM roots "
        "ORDER BY added, path").fetchall()
    out = []
    for path, last, vid, label in rows:
        here = os.path.isdir(path)
        if not vid:
            state = "ok" if here else "not connected"
        elif here:
            now = mount_of(path)
            if now and _norm(now) not in present:
                now = None
            now_vid = volume_info(now)[0] if now else None
            state = "ok" if now_vid in (vid, None) else "different drive here"
        else:
            state = "connected elsewhere" if vid in mounted else "not connected"
        out.append((path, last, label or "", state))
    return out
