"""Bring a browser sign-in window to the front on Windows.

The tray service launches the OAuth browser from a background process, and
Windows refuses foreground activation to a window whose creator does not own
the foreground. The window opens *behind* the dashboard, so the user sees
nothing happen, clicks the dashboard's own fallback link, and authorizes with
whatever account that browser is already signed in to. That is the bug this
module exists to prevent.

Everything here is best effort. A focus attempt that fails is a cosmetic loss,
never a reason an OAuth flow cannot finish, so nothing raises: the caller gets
one of "foreground", "restored", "flashed", "not_found" or "unsupported".

Kept out of browser_launch.py so neither file carries two concepts (and so the
ctypes block does not push that module past the size guardrail).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Optional

logger = logging.getLogger("jacked.oauth.browser")

# ShowWindow commands.
SW_MINIMIZE = 6
SW_RESTORE = 9

# FlashWindowEx: flash caption and taskbar button until the window comes to the
# foreground. The taskbar button is the only signal left when activation is
# denied outright.
FLASHW_ALL = 0x3
FLASHW_TIMERNOFG = 0xC

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# A window owned by the process we spawned is matched immediately. Chrome may
# instead hand the URL to an already-running instance for that profile and
# exit, leaving no window under our pid, so after this long we also accept a
# Claude-titled window belonging to the same executable.
_EXE_MATCH_AFTER_SECONDS = 3.0
_POLL_INTERVAL_SECONDS = 0.25


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
        ("uCount", ctypes.c_uint32),
        ("dwTimeout", ctypes.c_uint32),
    ]


def _process_image_path(pid: int) -> Optional[str]:
    """Full executable path for ``pid``, or None when it cannot be read."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_uint32(2048)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return None
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _visible_windows() -> list[tuple[int, int, str]]:
    """``(hwnd, owning pid, title)`` for every visible titled top-level window."""
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, str]] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((int(hwnd), int(pid.value), buf.value))
        return True

    user32.EnumWindows(enum_proc(_collect), 0)
    return found


def match_window(
    windows: list[tuple[int, int, str]],
    pid: int,
    exe_path: str,
    allow_exe_match: bool,
    image_of=None,
) -> Optional[int]:
    """The sign-in window among ``windows``, or None.

    An exact pid match always wins. ``allow_exe_match`` opens the fallback:
    a window titled for Claude that belongs to the same executable. That
    fallback needs a title check, or the first unrelated Chrome window on the
    desktop would be dragged to the front instead.

    >>> rows = [(11, 7, "Sign in - Claude"), (12, 9, "Inbox - Chrome")]
    >>> match_window(rows, 7, "C:/chrome.exe", False)
    11
    >>> match_window(rows, 99, "C:/chrome.exe", False) is None
    True
    >>> match_window(rows, 99, "C:/CHROME.EXE", True, lambda p: "c:/chrome.exe")
    11
    >>> rows2 = [(12, 9, "Inbox - Chrome")]
    >>> match_window(rows2, 99, "C:/chrome.exe", True, lambda p: "c:/chrome.exe")
    >>> match_window(rows, 99, "C:/other.exe", True, lambda p: "c:/chrome.exe")
    """
    for hwnd, window_pid, _title in windows:
        if window_pid == pid:
            return hwnd
    if not allow_exe_match or not exe_path:
        return None
    lookup = image_of or _process_image_path
    target = exe_path.lower()
    for hwnd, window_pid, title in windows:
        if "claude" not in title.lower():
            continue
        image = lookup(window_pid)
        if image and image.lower() == target:
            return hwnd
    return None


def _await_window(pid: int, exe_path: str, timeout: float) -> Optional[int]:
    """Poll for the sign-in window until ``timeout`` seconds have passed."""
    started = time.monotonic()
    deadline = started + timeout
    while True:
        allow_exe = time.monotonic() - started >= _EXE_MATCH_AFTER_SECONDS
        hwnd = match_window(_visible_windows(), pid, exe_path, allow_exe)
        if hwnd is not None:
            return hwnd
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)


def _is_foreground(hwnd: int) -> bool:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return user32.GetForegroundWindow() == hwnd


def _flash(hwnd: int) -> None:
    info = _FLASHWINFO(
        ctypes.sizeof(_FLASHWINFO),
        ctypes.c_void_p(hwnd),
        FLASHW_ALL | FLASHW_TIMERNOFG,
        0,
        0,
    )
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


def _focus_window(hwnd: int) -> str:
    """Raise ``hwnd``, escalating until something works."""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if _is_foreground(hwnd):
        return "foreground"
    # A minimize/restore cycle re-asserts activation rights for the window's
    # own thread, which sometimes clears the block a background creator hit.
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(0.15)
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.3)
    if _is_foreground(hwnd):
        return "restored"
    _flash(hwnd)
    return "flashed"


def bring_to_front_windows(pid: int, exe_path: str, timeout: float = 8.0) -> str:
    """Find and raise the sign-in window this launch created.

    Blocks for up to ``timeout`` seconds, so callers run it on a thread.
    """
    if sys.platform != "win32":
        return "unsupported"
    result = "not_found"
    try:
        hwnd = _await_window(pid, exe_path, timeout)
        if hwnd is not None:
            result = _focus_window(hwnd)
    except Exception as e:  # cosmetic path; never break an OAuth flow
        logger.debug("OAuth browser: could not focus the sign-in window: %s", e)
        result = "not_found"
    logger.info("OAuth browser: sign-in window %s", result)
    return result
