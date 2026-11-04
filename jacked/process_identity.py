"""What the process table says about a process: its parent, its start, its token.

The session tracker (``jacked.session_ancestry``) reads these facts to tell
whether a tracked session still runs, and which session runs a command.
Every read fails soft: an id whose fact cannot be read is absent (or None).

POSIX only for ancestry: Linux reads ``/proc``, other systems ask ``ps``.
Start times and tokens work on Windows too (the kernel's creation time).
Each batch read costs one pass: ``/proc`` on Linux, the kernel on Windows,
one ``ps`` for all ids elsewhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Enough for claude -> shell -> launcher -> python and a few wrappers; the
# walk also stops at pid 1 and on a loop.
_MAX_DEPTH = 16
_PS_TIMEOUT_SECONDS = 2.0


def _proc_stat(pid: int) -> Optional[list[str]]:
    """Fields 3 and later of ``/proc/<pid>/stat``; None without /proc or access.

    Any OSError counts as "cannot read": with ``hidepid`` a root-owned
    ancestor raises PermissionError, which ``Path.exists`` does not swallow.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if ")" not in data:
        return None
    # The command name (field 2) can hold spaces and ")".
    return data[data.rindex(")") + 2:].split()


def _ps(pid: int, field: str) -> Optional[str]:
    """One ``ps`` output field for *pid*, or None when ps cannot answer."""
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _ps_starts(pids: list[int]) -> dict[int, float]:
    """Start times of *pids* from ONE ``ps`` run; ids ps does not list are absent."""
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,etime=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    now = time.time()
    starts: dict[int, float] = {}
    for line in (result.stdout or "").splitlines():
        pid_text, _, etime = line.strip().partition(" ")
        seconds = _etime_seconds(etime)
        if pid_text.isdigit() and seconds is not None:
            starts[int(pid_text)] = now - seconds
    return starts


def _parent_of(pid: int) -> Optional[int]:
    """The parent pid of *pid*, or None when it cannot be read."""
    fields = _proc_stat(pid)
    text = fields[1] if fields and len(fields) > 1 else _ps(pid, "ppid")
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _etime_seconds(text: Optional[str]) -> Optional[int]:
    """Seconds in a ``ps`` elapsed time, ``[[dd-]hh:]mm:ss``, or None.

    >>> _etime_seconds("05:03"), _etime_seconds("01:00:00"), _etime_seconds("2-00:00:01")
    (303, 3600, 172801)
    >>> _etime_seconds("soon"), _etime_seconds(None), _etime_seconds("1:2:3:4")
    (None, None, None)
    """
    if not text:
        return None
    days, _, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    try:
        values = [int(days or 0)] + [0] * (3 - len(parts)) + [int(p) for p in parts]
    except ValueError:
        return None
    if len(values) != 4:
        return None
    d, h, m, s = values
    return ((d * 24 + h) * 60 + m) * 60 + s


def _linux_boot_time() -> Optional[float]:
    """The ``btime`` line of ``/proc/stat`` (epoch seconds), read on each call.

    The kernel moves ``btime`` when the wall clock steps. A cached value
    gives starts in the clock of the first read, which do not agree with
    ``time.time()`` after a step.
    """
    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
        return next(float(x.split()[1]) for x in lines if x.startswith("btime "))
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _stat_ticks(fields: list[str]) -> Optional[int]:
    """Field 22 of ``/proc/<pid>/stat``: the start in clock ticks since boot."""
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _stat_start(fields: list[str], boot: Optional[float]) -> Optional[float]:
    """The start time in a ``/proc/<pid>/stat`` field list, epoch seconds."""
    ticks = _stat_ticks(fields)
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return None
    if boot is None or ticks is None or hz <= 0:
        return None
    return boot + ticks / hz


def _on_windows() -> bool:
    """True on Windows (a seam for tests; ``os.name`` itself is not patched)."""
    return os.name == "nt"


# Seconds between 1601-01-01 (the FILETIME epoch) and 1970-01-01.
_FILETIME_EPOCH_OFFSET = 11_644_473_600


def _windows_process_start(pid: int) -> Optional[float]:
    """The creation time of a Windows process, epoch seconds, or None."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    # Explicit argtypes/restype: default int marshalling truncates a 64-bit HANDLE.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return created / 10_000_000 - _FILETIME_EPOCH_OFFSET if created else None
    finally:
        kernel32.CloseHandle(handle)


def _wanted(pids) -> list[int]:
    """The usable process ids in *pids*, sorted, without duplicates."""
    return sorted({
        p for p in pids if isinstance(p, int) and not isinstance(p, bool) and p > 0
    })


def creation_id(pid) -> Optional[str]:
    """A marker of when *pid* started, or None when it cannot be read.

    The marker is the start that the kernel keeps for the process, so a
    wall-clock step does not change it, and a newer process that reuses the
    id gets a different marker. It is read in-process on every platform,
    never through a subprocess: Linux and Windows use
    ``jacked.service.instance_storage.process_identity`` (``/proc``, the
    Windows API). macOS reads the start with ``proc_pidinfo``: the ``ps``
    that ``process_identity`` runs there costs a subprocess, and it prints
    the start in the locale and the time zone of the caller.
    The update status file and the session tracker store these markers, so
    the text format must not change.

    >>> import os
    >>> marker = creation_id(os.getpid())
    >>> marker is None or marker == creation_id(os.getpid())
    True
    >>> creation_id(True), creation_id(0), creation_id("7")
    (None, None, None)
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_start_marker(pid)
        from jacked.service.instance_storage import process_identity

        return process_identity(pid).creation_id
    except Exception:  # noqa: BLE001 - an unreadable identity is simply unknown
        return None


def _darwin_start_marker(pid: int) -> Optional[str]:
    """``proc_pidinfo(PROC_PIDTBSDINFO)``: the struct proc_bsdinfo start time
    (sys/proc_info.h), in-process."""
    import ctypes
    import ctypes.util

    class _BsdInfo(ctypes.Structure):
        _fields_ = [
            *[(name, ctypes.c_uint32) for name in (
                "flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
                "ruid", "rgid", "svuid", "svgid", "rfu_1")],
            ("comm", ctypes.c_char * 16),
            ("name", ctypes.c_char * 32),
            *[(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")],
            ("nice", ctypes.c_int32),
            ("start_tvsec", ctypes.c_uint64),
            ("start_tvusec", ctypes.c_uint64),
        ]

    libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
    info = _BsdInfo()
    size = libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info) or info.pid != pid:
        return None
    return f"darwin-start:{info.start_tvsec}.{info.start_tvusec:06d}"


def process_tokens(pids) -> dict[int, str]:
    """The ``creation_id`` of each process in *pids*; unreadable ids are absent.

    The session tracker stores this token with each row. Two reads of one
    process give the same token, a newer process that reuses the id gives a
    different token, and a wall-clock step changes neither.

    >>> import os
    >>> token = process_tokens([os.getpid(), -1, True]).get(os.getpid())
    >>> token is None or token == process_token(os.getpid())
    True
    """
    tokens: dict[int, str] = {}
    for pid in _wanted(pids):
        if (token := creation_id(pid)) is not None:
            tokens[pid] = token
    return tokens


def process_token(pid: int) -> Optional[str]:
    """The token of *pid* (see ``process_tokens``), or None when it cannot be read."""
    return process_tokens([pid]).get(pid)


def process_starts(pids) -> dict[int, float]:
    """When each process in *pids* started, epoch seconds, read in one pass.

    Linux reads ``/proc`` (no subprocess). Windows asks the kernel for each
    id. Other POSIX systems run ONE ``ps`` for every id that ``/proc`` did
    not answer. An id whose start cannot be read is absent from the result.

    >>> import os, time
    >>> start = process_starts([os.getpid(), -1, True]).get(os.getpid())
    >>> start is None or start <= time.time() + 1
    True
    """
    wanted = _wanted(pids)
    starts: dict[int, float] = {}
    if _on_windows():
        for pid in wanted:
            try:
                start = _windows_process_start(pid)
            except Exception:  # noqa: BLE001 - ctypes errors: the start is unknown
                start = None
            if start is not None:
                starts[pid] = start
        return starts
    unanswered: list[int] = []
    boot: Optional[float] = None
    for pid in wanted:
        fields = _proc_stat(pid)
        if fields is None:
            unanswered.append(pid)
            continue
        boot = boot if boot is not None else _linux_boot_time()  # once per pass
        if (start := _stat_start(fields, boot)) is not None:
            starts[pid] = start
    if unanswered:
        starts.update(_ps_starts(unanswered))
    return starts


def process_start(pid: int) -> Optional[float]:
    """When *pid* started, in epoch seconds, or None when it cannot be read.

    >>> import os, time
    >>> start = process_start(os.getpid())
    >>> start is None or start <= time.time() + 1
    True
    """
    return process_starts([pid]).get(pid)


def ancestor_pids(pid: Optional[int] = None, max_depth: int = _MAX_DEPTH) -> list[int]:
    """The pids of this process's ancestors, nearest first.

    *pid* is the first ancestor to report (default: this process's parent).
    Empty on a platform without POSIX process ids.

    >>> import os
    >>> pids = ancestor_pids()
    >>> os.name != "posix" or os.getppid() <= 1 or pids[0] == os.getppid()
    True
    >>> ancestor_pids(pid=1)
    []
    """
    if os.name != "posix":
        return []
    current = os.getppid() if pid is None else pid
    out: list[int] = []
    for _ in range(max_depth):
        if current is None or current <= 1 or current in out:
            break
        out.append(current)
        current = _parent_of(current)
    return out
