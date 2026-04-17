"""System tray icon and menu for jacked service mode."""

import logging
import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

from jacked import __version__
from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
from jacked.service.process import (
    is_port_available,
    remove_pid,
    write_pid,
)
from jacked.version_check import check_version_cached

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont

    _TRAY_AVAILABLE = True
except ImportError:
    _TRAY_AVAILABLE = False

try:
    import uvicorn

    _UVICORN_AVAILABLE = True
except ImportError:
    _UVICORN_AVAILABLE = False


# Icon color schemes per state
_ICON_COLORS = {
    "running": ("#6366f1", "#8b5cf6"),  # Purple gradient
    "starting": ("#f59e0b", "#d97706"),  # Amber
    "stopped": ("#555555", "#666666"),  # Gray
}


def check_tray_deps() -> None:
    """Raise with install instructions if tray deps missing."""
    if not _TRAY_AVAILABLE:
        raise SystemExit(
            "Service mode requires the [tray] extra.\n"
            'Install it with: uv tool install "claude-jacked[tray]" --force'
        )


def create_icon_image(state: str) -> "Image.Image":
    """Generate a 64x64 tray icon with a J glyph.

    Args:
        state: One of 'running', 'starting', 'stopped'.
    """
    colors = _ICON_COLORS.get(state, _ICON_COLORS["stopped"])
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle background
    draw.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)],
        radius=12,
        fill=colors[0],
    )
    # Slight gradient effect — smaller inner rect
    draw.rounded_rectangle(
        [(2, 2), (size - 3, size // 2)],
        radius=10,
        fill=colors[1],
    )

    # Draw "J" glyph centered
    try:
        font = ImageFont.truetype("Arial", 36)
    except (OSError, IOError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "J", font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), "J", fill="white", font=font)

    return img


def build_menu(
    port: int,
    version: str,
    autostart_check,
    on_open_dashboard,
    on_restart,
    on_stop,
    on_toggle_autostart,
    version_text_fn=None,
    version_click_fn=None,
    version_enabled_fn=None,
    on_check_for_updates=None,
    last_check_text_fn=None,
    check_in_progress_fn=None,
) -> "pystray.Menu":
    """Build the tray right-click menu.

    Args:
        autostart_check: Callable returning bool — evaluated each time
            the menu is shown, so toggle changes are reflected live.
        version_text_fn: Optional callable returning the version menu
            label (e.g. "v0.41.0" or "Update to v0.42.0 ->").
        version_click_fn: Optional callback invoked when the user clicks
            the version menu item.
        version_enabled_fn: Optional callable returning bool — gates
            whether the version item is clickable (outdated only).
        on_check_for_updates: Optional callback for a "Check for updates"
            menu item that forces a fresh PyPI poll bypassing the cache.
        last_check_text_fn: Optional callable returning a human-readable
            "Last checked: Xm ago" / "Checking PyPI..." / "Last checked:
            never" label. Shown under the version item so users with
            system notifications muted can still see the check happened.
        check_in_progress_fn: Optional callable returning bool — True
            while a manual PyPI check is in flight. Disables "Check for
            updates..." during the check so double-clicks don't pile up.
    """
    if version_text_fn is not None:
        version_item = pystray.MenuItem(
            lambda _: version_text_fn(),
            version_click_fn,
            enabled=lambda _: version_enabled_fn(),
        )
    else:
        version_item = pystray.MenuItem(f"v{version}", None, enabled=False)

    items = [
        pystray.MenuItem("JACKED", None, enabled=False),
        pystray.MenuItem(f"Running on :{port}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Dashboard", on_open_dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Stop", on_stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start on Login",
            on_toggle_autostart,
            checked=lambda _: autostart_check(),
        ),
        pystray.Menu.SEPARATOR,
        version_item,
    ]
    if last_check_text_fn is not None:
        items.append(
            pystray.MenuItem(lambda _: last_check_text_fn(), None, enabled=False)
        )
    if on_check_for_updates is not None:
        if check_in_progress_fn is not None:
            items.append(
                pystray.MenuItem(
                    "Check for updates...",
                    on_check_for_updates,
                    enabled=lambda _: not check_in_progress_fn(),
                )
            )
        else:
            items.append(
                pystray.MenuItem("Check for updates...", on_check_for_updates)
            )

    return pystray.Menu(*items)


class ServiceRunner:
    """Manages the uvicorn server thread and pystray icon."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()  # reentrant: update click holds through _on_stop
        self._uvicorn_thread: threading.Thread | None = None
        self._uvicorn_server = None
        self._icon: "pystray.Icon | None" = None
        self._autostart_enabled = False
        self._version_info: dict | None = None
        # Visible last-checked timestamp + in-progress flag for the tray menu,
        # so users can see the check happened even when system notifications
        # are muted.
        self._last_check_at: float | None = None
        self._version_check_in_progress: bool = False

    def _start_uvicorn(self) -> threading.Thread:
        """Start uvicorn in a daemon thread."""
        os.environ["JACKED_HOST"] = self.host
        os.environ["JACKED_PORT"] = str(self.port)

        config = uvicorn.Config(
            "jacked.api.main:app",
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        thread = threading.Thread(target=server.run, name="jacked-uvicorn", daemon=True)
        thread.start()
        return thread

    def _wait_for_ready(self, timeout: float = 10.0) -> bool:
        """Poll until the server is accepting connections."""
        import socket
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=0.5)
                sock.close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def _on_open_dashboard(self):
        webbrowser.open(f"http://{self.host}:{self.port}")

    def _on_restart(self):
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Already restarting or stopping
        try:
            if self._icon:
                self._icon.icon = create_icon_image("starting")
            if self._uvicorn_server is not None:
                self._uvicorn_server.should_exit = True
            if self._uvicorn_thread:
                self._uvicorn_thread.join(timeout=5)
            try:
                self._uvicorn_thread = self._start_uvicorn()
                if self._wait_for_ready():
                    if self._icon:
                        self._icon.icon = create_icon_image("running")
                else:
                    if self._icon:
                        self._icon.icon = create_icon_image("stopped")
            except Exception:
                logger.exception("Restart failed")
                if self._icon:
                    self._icon.icon = create_icon_image("stopped")
        finally:
            self._lifecycle_lock.release()

    def _request_stop(self):
        """Signal-safe stop request — only sets flags, no locks or I/O."""
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        self._stop_event.set()

    def _install_windows_console_handler(self) -> None:
        """Register a Win32 SetConsoleCtrlHandler so Ctrl+C kills the service.

        Why this is needed: pystray's Windows backend runs an AppKit-equivalent
        native Win32 message pump on the main thread. Python's signal module
        on Windows only delivers SIGINT between bytecode ops, which never
        happen while the message pump is blocked in `GetMessage`. Before
        this handler, `Ctrl+C` in a console running `jacked service start`
        was swallowed. The OS-level handler runs in a dedicated thread
        spawned by Windows and can set our stop event regardless of what
        the main thread is doing.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _handler(ctrl_type):  # noqa: ARG001 — all control types trigger stop
                try:
                    self._request_stop()
                    if self._icon is not None:
                        # Let pystray exit its own message pump cleanly.
                        try:
                            self._icon.stop()
                        except Exception:
                            pass
                except Exception:
                    pass
                return True  # we handled it — don't chain to default handler

            # Keep a strong reference so the callback isn't GC'd.
            self._win_ctrl_handler = HandlerRoutine(_handler)
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler.argtypes = [HandlerRoutine, wintypes.BOOL]
            kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            kernel32.SetConsoleCtrlHandler(self._win_ctrl_handler, True)
        except Exception:
            logger.exception("Could not install Windows console Ctrl handler")

    def _on_stop(self):
        """Full stop — called from menu or atexit, not from signal handler."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Already stopping or restarting
        try:
            self._request_stop()
            if self._uvicorn_thread:
                self._uvicorn_thread.join(timeout=5)
            remove_pid(PID_FILE)
            if self._icon:
                self._icon.stop()
        finally:
            self._lifecycle_lock.release()

    def _on_toggle_autostart(self):
        from jacked.service.platform import (
            detect_autostart,
            install_autostart,
            uninstall_autostart,
        )

        if detect_autostart():
            uninstall_autostart()
            self._autostart_enabled = False
        else:
            install_autostart(self.host, self.port)
            self._autostart_enabled = True

    def _check_version(self, force: bool = False) -> None:
        """Poll PyPI for latest version and refresh the menu.

        When called as a background-thread target (no args), runs in a loop
        once per day. When called via the tray's "Check for updates" menu
        item with `force=True`, does a single forced PyPI hit and returns.
        """
        import time as _time
        first = True
        while not self._stop_event.is_set():
            try:
                # First iteration honors the `force` arg from the caller.
                # Subsequent loops are always cache-respecting.
                info = check_version_cached(__version__, force=force if first else False)
                if info is not None:
                    self._version_info = info
                # Always stamp the last-checked time (even on PyPI failure) so
                # the tray menu can reflect that a check just ran.
                self._last_check_at = _time.time()
                if self._icon and not self._stop_event.is_set():
                    try:
                        self._icon.update_menu()
                    except Exception:
                        pass  # some pystray backends fail during shutdown
            except Exception:
                logger.exception("Version check failed")
            first = False
            # Daily background cadence; manual "Check for updates" fires
            # this method in a one-shot thread which exits after this wait.
            if self._stop_event.wait(timeout=86400):
                return

    def _on_check_for_updates(self) -> None:
        """Tray menu handler: force a fresh PyPI check now."""
        # Guard: ignore if a check is already running (double-click, etc.).
        if self._version_check_in_progress:
            return

        self._version_check_in_progress = True
        # Immediately refresh the menu so the "Checking PyPI..." label shows
        # even for users who have macOS notifications muted.
        if self._icon:
            try:
                self._icon.update_menu()
            except Exception:
                pass
            try:
                self._icon.notify("Checking PyPI for updates...", "Jacked")
            except Exception:
                pass

        # Fire a one-shot thread so we don't block the menu callback.
        def _once():
            import time as _time
            try:
                info = check_version_cached(__version__, force=True)
            except Exception:
                logger.exception("Manual version check failed")
                info = None
            if info is not None:
                self._version_info = info
            # Stamp last-checked regardless of PyPI reachability — the user
            # asked for a check; failure or success both happened "just now".
            self._last_check_at = _time.time()
            self._version_check_in_progress = False

            if self._icon and not self._stop_event.is_set():
                try:
                    self._icon.update_menu()
                except Exception:
                    pass
                try:
                    if info is None:
                        self._icon.notify(
                            "Couldn't reach PyPI. Try again later.",
                            "Jacked Update Check",
                        )
                    elif info.get("outdated"):
                        latest = info.get("latest", "?")
                        self._icon.notify(
                            f"Update available: v{latest}", "Jacked",
                        )
                    else:
                        self._icon.notify(
                            f"You're up to date (v{__version__})", "Jacked",
                        )
                except Exception:
                    pass

        threading.Thread(
            target=_once, name="jacked-manual-version-check", daemon=True
        ).start()

    def _last_check_menu_text(self) -> str:
        """Human-readable summary for the 'Last checked' menu item.

        Shown even when the user has macOS notifications muted so they can
        see the check actually happened.
        """
        if self._version_check_in_progress:
            return "Checking PyPI..."
        if self._last_check_at is None:
            return "Last checked: never"

        import time as _time
        delta = max(0, int(_time.time() - self._last_check_at))
        if delta < 60:
            return "Last checked: just now"
        if delta < 3600:
            return f"Last checked: {delta // 60}m ago"
        if delta < 86400:
            return f"Last checked: {delta // 3600}h ago"
        return f"Last checked: {delta // 86400}d ago"

    def _version_menu_text(self) -> str:
        # Always anchor on __version__ (what this running process actually is),
        # not on the cached "latest" from PyPI — the latter can be stale or
        # even older than the running version when the user is ahead.
        if self._version_info and self._version_info.get("outdated"):
            latest = self._version_info.get("latest", "?")
            return f"v{__version__} -> v{latest} (update)"
        return f"v{__version__}"

    def _version_is_clickable(self) -> bool:
        return bool(self._version_info and self._version_info.get("outdated"))

    def _on_update_click(self):
        """User clicked 'Update to vX.Y.Z' in the tray menu.

        Uses the reentrant _lifecycle_lock throughout the spawn+stop transition.
        """
        if not self._version_is_clickable():
            return
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # already updating/stopping

        latest = (self._version_info or {}).get("latest", "?")
        try:
            if self._icon:
                try:
                    self._icon.icon = create_icon_image("starting")
                    self._icon.notify(
                        f"Updating jacked to v{latest}. If this fails, "
                        f"see ~/.claude/jacked-update-failed.txt",
                        "Jacked Update",
                    )
                except Exception:
                    logger.exception("Icon update during update-click failed")

            try:
                from jacked.service.updater import spawn_updater_from_tray
                spawn_updater_from_tray(parent_pid=os.getpid(), extras="tray")
            except Exception:
                logger.exception("Failed to spawn updater")
                if self._icon:
                    try:
                        self._icon.notify(
                            "Could not start update. See jacked-update.log",
                            "Jacked Update Failed",
                        )
                    except Exception:
                        pass
                return

            # RLock reentrancy: _on_stop reacquires the same lock from the same thread.
            # No release gap — a concurrent click stays blocked.
            self._on_stop()
        finally:
            self._lifecycle_lock.release()

    def _stop_monitor(self):
        """Background thread that watches _stop_event and triggers full stop."""
        self._stop_event.wait()
        self._on_stop()

    def _setup(self, icon: "pystray.Icon"):
        """pystray setup callback — runs after icon appears."""
        icon.visible = True

        # Surface a prior failed update if recovery file exists.
        try:
            from jacked.service.updater import RECOVERY_FILE
            if RECOVERY_FILE.exists():
                icon.notify(
                    "Previous update failed. See "
                    "~/.claude/jacked-update-failed.txt for recovery steps.",
                    "Jacked Update Failed Earlier",
                )
        except Exception:
            logger.exception("Could not check update recovery file")

        # Monitor thread bridges signal-safe _request_stop to full _on_stop
        threading.Thread(
            target=self._stop_monitor, name="jacked-stop-monitor", daemon=True
        ).start()
        threading.Thread(
            target=self._check_version, name="jacked-version-check", daemon=True
        ).start()
        self._uvicorn_thread = self._start_uvicorn()
        if self._wait_for_ready():
            icon.icon = create_icon_image("running")
        else:
            icon.icon = create_icon_image("stopped")
            remove_pid(PID_FILE)
            icon.notify("Jacked failed to start", "Jacked Service")

    def run(self) -> None:
        """Start the service: tray icon on main thread, uvicorn in background."""
        check_tray_deps()

        if not _UVICORN_AVAILABLE:
            raise SystemExit(
                "Service mode requires uvicorn.\n"
                'Install it with: uv tool install "claude-jacked" --force'
            )

        if not is_port_available(self.host, self.port):
            raise SystemExit(
                f"Port {self.port} is already in use.\n"
                "Is another jacked instance running? Check with: jacked service status\n"
                "Use --port to run on a different port."
            )

        write_pid(PID_FILE, self.port)

        # Signal handlers are signal-safe — they just set the stop event.
        # The stop-monitor thread bridges that to the full _on_stop() cleanup.
        if sys.platform == "win32":
            # Windows: pystray runs a native GUI message pump that blocks
            # Python bytecode. Installing SIGINT still lets Ctrl+C in the
            # console deliver a KeyboardInterrupt, and SIGBREAK covers
            # Ctrl+Break explicitly. Without these, `jacked service start`
            # in a console ignored Ctrl+C entirely.
            try:
                signal.signal(signal.SIGINT, lambda *_: self._request_stop())
            except (ValueError, OSError):
                pass  # not a main thread (shouldn't happen here, but harmless)
            if hasattr(signal, "SIGBREAK"):
                try:
                    signal.signal(signal.SIGBREAK, lambda *_: self._request_stop())
                except (ValueError, OSError):
                    pass
            # Console Ctrl-events are delivered by the OS to a separate
            # handler chain; wire that too so the user's Ctrl+C isn't
            # swallowed by the GUI message pump before Python sees it.
            self._install_windows_console_handler()
        else:
            signal.signal(signal.SIGTERM, lambda *_: self._request_stop())
            signal.signal(signal.SIGINT, lambda *_: self._request_stop())

        from jacked.service.platform import detect_autostart

        self._autostart_enabled = detect_autostart()

        menu = build_menu(
            port=self.port,
            version=__version__,
            autostart_check=lambda: self._autostart_enabled,
            on_open_dashboard=self._on_open_dashboard,
            on_restart=self._on_restart,
            on_stop=self._on_stop,
            on_toggle_autostart=self._on_toggle_autostart,
            version_text_fn=self._version_menu_text,
            version_click_fn=self._on_update_click,
            version_enabled_fn=self._version_is_clickable,
            on_check_for_updates=self._on_check_for_updates,
            last_check_text_fn=self._last_check_menu_text,
            check_in_progress_fn=lambda: self._version_check_in_progress,
        )

        self._icon = pystray.Icon(
            name="jacked",
            icon=create_icon_image("starting"),
            title="Jacked",
            menu=menu,
        )
        try:
            self._icon.run(setup=self._setup)
        finally:
            # Ensure PID file is cleaned up even if pystray crashes
            remove_pid(PID_FILE)
