"""System tray icon and menu for jacked service mode."""

import logging
import os
import signal
import sys
import threading
import time
import webbrowser

from jacked import __version__
from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
from jacked.service.process import (
    is_port_available,
    remove_pid,
    write_pid,
)
from jacked.version_check import check_version_cached

logger = logging.getLogger(__name__)

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


def select_menubar_backend(platform: str, mac_available: bool) -> str:
    """Choose the status-bar backend for a platform.

    Returns ``"mac"`` only on darwin when the rumps/pyobjc agent is importable;
    every other case (non-darwin, or darwin without the deps) returns
    ``"pystray"`` so Windows/Linux behavior is unchanged and macOS degrades
    gracefully if the native deps somehow aren't present.

    >>> select_menubar_backend("darwin", True)
    'mac'
    >>> select_menubar_backend("darwin", False)
    'pystray'
    >>> select_menubar_backend("win32", True)
    'pystray'
    >>> select_menubar_backend("linux", True)
    'pystray'
    """
    if platform == "darwin" and mac_available:
        return "mac"
    return "pystray"


def _mac_menubar_available() -> bool:
    """True when the macOS menu-bar agent (rumps + pyobjc) can be used."""
    if sys.platform != "darwin":
        return False
    try:
        from jacked.service.menubar_mac import RUMPS_AVAILABLE

        return RUMPS_AVAILABLE
    except Exception:
        return False


# Icon color schemes per state
_ICON_COLORS = {
    "running": ("#6366f1", "#8b5cf6"),  # Purple gradient
    "starting": ("#f59e0b", "#d97706"),  # Amber
    "stopped": ("#555555", "#666666"),  # Gray
}

# Font files to try, in order, for the tray "J" glyph. Bare family names
# like "Arial" ONLY resolve on macOS — Pillow on Windows/Linux raises
# OSError('cannot open resource') unless given the actual filename. That
# was the long-standing Windows bug: the J fell back to the ~10px bitmap
# default and shrank to an invisible speck in the system tray. DejaVuSans
# ships bundled inside Pillow, so it's the last truetype attempt.
_GLYPH_FONT_CANDIDATES = (
    "arial.ttf",            # Windows (C:\Windows\Fonts), also macOS
    "Arial.ttf",
    "Arial",                # macOS family-name resolution
    "Helvetica.ttc",        # macOS
    "DejaVuSans-Bold.ttf",  # Linux + Pillow-bundled
    "DejaVuSans.ttf",
)


def _load_glyph_font(size: int) -> "ImageFont.FreeTypeFont":
    """Load a scalable TrueType font for the tray glyph at *size* px.

    The font MUST be a real scalable face at the requested size. The legacy
    argless ``ImageFont.load_default()`` returns a ~10px bitmap font, which
    downscales to nothing in the tray — the exact reason the "J" was
    invisible on Windows. We try real font filenames first, then fall back
    to Pillow's bundled DejaVu at the requested size (Pillow >= 10.1).
    """
    for name in _GLYPH_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    # Pillow >= 10.1 honors `size` here and returns a scalable DejaVu face.
    # Older Pillow ignores it (10px bitmap) — still better than crashing.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


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
    font = _load_glyph_font(36)

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
    started_at_text_fn=None,
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
    ]
    if started_at_text_fn is not None:
        items.append(
            pystray.MenuItem(
                lambda _: started_at_text_fn(), None, enabled=False,
            )
        )
    items.extend([
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
    ])
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
        # Wall-clock timestamp when the current uvicorn thread became ready
        # (passed _wait_for_ready). Used by the tray menu so users can verify
        # a Restart click actually took effect — the timestamp shifts on
        # every successful start/restart.
        self._started_at: float | None = None

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

    def _wait_for_port_free(self, timeout: float = 10.0) -> bool:
        """Poll until the port is available for binding (old server fully released).

        After uvicorn's thread joins, the OS socket may still be in TIME_WAIT
        for a few seconds. Binding too early raises OSError: Address already
        in use. We bind+close a probe socket with SO_REUSEADDR to prove the
        port is genuinely free.
        """
        import socket
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.close()
                return True
            except OSError:
                sock.close()
                time.sleep(0.2)
        return False

    def _shutdown_uvicorn(self) -> None:
        """Stop uvicorn with graceful-then-force fallback.

        uvicorn's ``should_exit`` only triggers shutdown between requests
        and waits for active connections (notably WebSockets) to close on
        their own. Dashboard tabs hold WS subscribers open indefinitely,
        which means a graceful shutdown can hang past any reasonable
        timeout. After 3s of grace we set ``force_exit = True`` so uvicorn
        cancels the lifespan + drops connections; after another 5s we
        give up on the join (the thread is daemon and the OS will reap
        it when the new server binds).

        Logs include the active WS client count + actual elapsed time so
        operators reading ``~/.claude/jacked-tray.log`` can tell whether
        the force-exit was needed because of dashboard tabs (typical) or
        a hung lifespan (rare).
        """
        if self._uvicorn_server is None:
            return
        # Snapshot client count before shutdown for the log line.
        ws_client_count = 0
        try:
            app = getattr(self._uvicorn_server.config, "loaded_app", None)
            ws_registry = getattr(getattr(app, "state", None), "ws_registry", None)
            if ws_registry is not None:
                ws_client_count = getattr(ws_registry, "client_count", 0)
                if callable(ws_client_count):
                    ws_client_count = ws_client_count()
        except Exception:
            pass

        graceful_start = time.monotonic()
        self._uvicorn_server.should_exit = True
        if self._uvicorn_thread is None:
            return
        self._uvicorn_thread.join(timeout=3)
        graceful_elapsed = time.monotonic() - graceful_start
        if self._uvicorn_thread.is_alive():
            logger.info(
                "Uvicorn graceful shutdown timed out after %.1fs (ws_clients=%d) "
                "— forcing exit",
                graceful_elapsed, ws_client_count,
            )
            self._uvicorn_server.force_exit = True
            self._uvicorn_thread.join(timeout=5)
            if self._uvicorn_thread.is_alive():
                logger.warning(
                    "Uvicorn thread still alive after force_exit "
                    "(total elapsed %.1fs, ws_clients=%d) — proceeding "
                    "anyway; OS will reap the daemon thread",
                    time.monotonic() - graceful_start, ws_client_count,
                )

    def _on_open_dashboard(self):
        webbrowser.open(f"http://{self.host}:{self.port}")

    def _started_text(self) -> str:
        """Format the "Started ..." menu label.

        Re-evaluated each time the tray menu opens, so the relative-uptime
        portion stays fresh without manual refresh.
        """
        if self._started_at is None:
            return "Started: —"
        local = time.localtime(self._started_at)
        clock = time.strftime("%H:%M:%S", local)
        elapsed = max(0, int(time.time() - self._started_at))
        if elapsed < 60:
            rel = f"{elapsed}s ago"
        elif elapsed < 3600:
            rel = f"{elapsed // 60}m {elapsed % 60}s ago"
        elif elapsed < 86400:
            rel = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m ago"
        else:
            rel = f"{elapsed // 86400}d {(elapsed % 86400) // 3600}h ago"
        return f"Started {clock} ({rel})"

    def _on_restart(self):
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Already restarting or stopping
        try:
            if self._icon:
                self._icon.icon = create_icon_image("starting")

            self._shutdown_uvicorn()

            if not self._wait_for_port_free(timeout=10):
                logger.error(
                    "Port %d did not free up after uvicorn shutdown — "
                    "another process may be using it. Aborting restart.",
                    self.port,
                )
                if self._icon:
                    self._icon.icon = create_icon_image("stopped")
                return

            # Retry the bind+ready cycle in case of transient failures
            # (e.g. lifespan startup hiccup).
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    self._uvicorn_thread = self._start_uvicorn()
                    if self._wait_for_ready(timeout=15):
                        self._started_at = time.time()
                        logger.info(
                            "Service ready after restart "
                            "(pid=%d, port=%d, attempt=%d)",
                            os.getpid(), self.port, attempt + 1,
                        )
                        if self._icon:
                            self._icon.icon = create_icon_image("running")
                            self._icon.update_menu()
                        return
                    logger.warning(
                        "Restart attempt %d: server did not become ready "
                        "within timeout", attempt + 1,
                    )
                    self._shutdown_uvicorn()
                    self._wait_for_port_free(timeout=5)
                except Exception as exc:
                    last_err = exc
                    logger.exception(
                        "Restart attempt %d raised; will retry", attempt + 1,
                    )
                    self._shutdown_uvicorn()
                    self._wait_for_port_free(timeout=5)
            logger.error("Restart failed after 3 attempts: %s", last_err)
            if self._icon:
                self._icon.icon = create_icon_image("stopped")
        finally:
            self._lifecycle_lock.release()
            # Re-check stop signal in case Ctrl+C / Stop fired during
            # the restart and was rejected by acquire(blocking=False).
            # Without this, the user expects shutdown but gets a
            # successful restart with no feedback.
            if self._stop_event.is_set():
                logger.info(
                    "Stop signal fired during restart — honoring after "
                    "restart completes"
                )
                self._on_stop()

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

        # Pre-flight: refuse editable / pip installs BEFORE we kill the tray.
        # Without this the detached updater would later fail silently (or with
        # ModuleNotFoundError on editable dev-clone venvs that don't ship pip)
        # and the user would be left with a dead tray and no tray to retry from.
        from jacked.install_method import can_auto_upgrade as _can_upgrade
        _ok, _reason = _can_upgrade()
        if not _ok:
            if self._icon:
                try:
                    self._icon.notify(_reason, "Jacked auto-update disabled")
                except Exception:
                    logger.exception("Failed to notify on update refusal")
            try:
                from jacked.service.updater import RECOVERY_FILE
                RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
                RECOVERY_FILE.write_text(_reason + "\n")
            except Exception:
                logger.exception("Could not write recovery file on refusal")
            try:
                from jacked.service import update_status as _us
                _us.clear_status(_us.UPDATE_STATUS_FILE)
            except Exception:
                logger.exception("Could not clear stale status file on refusal")
            return

        if not self._lifecycle_lock.acquire(blocking=False):
            return  # already updating/stopping

        latest = (self._version_info or {}).get("latest", "?")

        # Breadcrumb FIRST — proves the click reached us even if everything
        # downstream fails. Uses the same log file the detached updater
        # appends to.
        try:
            import time as _time_mod
            from jacked.service.updater import UPDATE_LOG as _bc_log
            _bc_log.parent.mkdir(parents=True, exist_ok=True)
            with open(_bc_log, "a", encoding="utf-8") as _lf:
                _lf.write(
                    f"[{_time_mod.strftime('%Y-%m-%d %H:%M:%S')}] tray: update clicked "
                    f"(tray PID {os.getpid()}, target v{latest})\n"
                )
        except Exception:
            logger.exception("Could not write tray-click breadcrumb")

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

            # Pre-warm /update.html against loopback regardless of self.host —
            # self.host may be 0.0.0.0 (bind-all), which clients can't route
            # to on Linux. The service is always reachable on 127.0.0.1 after
            # bind.
            _url = f"http://127.0.0.1:{self.port}/update.html"
            try:
                import urllib.request as _ur
                with _ur.urlopen(_url, timeout=2.0):
                    pass
            except Exception:
                logger.exception("Pre-warm of update.html failed (continuing)")
            try:
                import webbrowser as _wb
                _wb.open(_url)
            except Exception:
                logger.exception("Failed to open update progress page")

            # init_status BEFORE the spawn so the status file exists from t=0.
            # If the detached child dies before its own init_status, the UI
            # still has something to show.
            try:
                from jacked.service import update_status as _us
                from jacked.service.updater import UPDATE_LOG as _upd_log
                from jacked.install_method import detect_install_method as _det
                from jacked import __version__ as _cv
                _us.init_status(
                    _us.UPDATE_STATUS_FILE,
                    from_version=_cv,
                    to_version=latest if latest and latest != "?" else "next",
                    method=_det(),
                    log_path=str(_upd_log),
                )
            except Exception as _e:
                # LockBusy or other — just log and continue. The updater
                # will handle its own init.
                logger.info("Pre-spawn init_status didn't write: %s", _e)

            try:
                from jacked.service.updater import spawn_updater_from_tray
                spawn_updater_from_tray(
                    parent_pid=os.getpid(),
                    extras="tray",
                    target_version=(self._version_info or {}).get("latest"),
                    port=self.port,
                )
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
                # Tray stays alive — don't _on_stop if the updater didn't spawn
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
            self._started_at = time.time()
            logger.info(
                "Service ready (pid=%d, port=%d, autostart=%s)",
                os.getpid(), self.port, self._autostart_enabled,
            )
            icon.icon = create_icon_image("running")
            # Force a menu rebuild so the dynamic "Started ..." item
            # picks up the just-set timestamp on first open. Without
            # this, pystray's macOS backend can show "Started: —"
            # because the menu was constructed before _setup populated
            # the field.
            try:
                icon.update_menu()
            except Exception:
                logger.debug("update_menu after start failed", exc_info=True)
        else:
            icon.icon = create_icon_image("stopped")
            remove_pid(PID_FILE)
            icon.notify("Jacked failed to start", "Jacked Service")

    def _install_tray_file_logger(self) -> None:
        """Route `jacked` logger output to ~/.claude/jacked-tray.log.

        Detached launchd/systemd services pipe stderr to /dev/null by default,
        so without a file handler any `logger.exception` during tray click
        handling is lost.
        """
        try:
            import logging as _logging
            from jacked.service import CLAUDE_DIR
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            tray_log_path = CLAUDE_DIR / "jacked-tray.log"
            handler = _logging.FileHandler(tray_log_path, encoding="utf-8")
            handler.setFormatter(
                _logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
            )
            _logger = _logging.getLogger("jacked")
            # Avoid double-adding if run() is called twice in tests.
            already = any(
                isinstance(h, _logging.FileHandler)
                and getattr(h, "baseFilename", None) == str(tray_log_path)
                for h in _logger.handlers
            )
            if not already:
                _logger.addHandler(handler)
                _logger.setLevel(_logging.INFO)
        except Exception:
            pass  # best-effort; fall back to stderr

    def _run_mac_menubar(self) -> None:
        """macOS path: rumps menu-bar agent on the main thread, uvicorn in a
        daemon thread. Mirrors the pystray path's preconditions (uvicorn check,
        port guard, PID file, signal handlers) but hands the main thread to the
        rumps run loop instead of pystray. The agent reuses THIS runner's
        uvicorn lifecycle — it never starts a second server."""
        from jacked.service.menubar_mac import MacMenuBarApp

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

        # Signal-safe: just set the stop event; the agent's stop-watch timer
        # bridges that to a clean uvicorn shutdown + quit on the main thread.
        signal.signal(signal.SIGTERM, lambda *_: self._request_stop())
        signal.signal(signal.SIGINT, lambda *_: self._request_stop())

        from jacked.service.platform import detect_autostart

        self._autostart_enabled = detect_autostart()

        # Start the one uvicorn (daemon thread) and health-check it before the
        # status item appears, so the first pill reflects real data.
        self._uvicorn_thread = self._start_uvicorn()
        if self._wait_for_ready():
            self._started_at = time.time()
            logger.info(
                "Service ready — macOS menu-bar agent (pid=%d, port=%d)",
                os.getpid(), self.port,
            )
        else:
            logger.error("Service did not become ready; menu-bar pill will show degraded")

        try:
            MacMenuBarApp(self).run()
        finally:
            remove_pid(PID_FILE)

    def run(self) -> None:
        """Start the service: tray icon on main thread, uvicorn in background."""
        self._install_tray_file_logger()

        # macOS gets the native menu-bar agent; every other platform keeps the
        # existing pystray tray below, unchanged.
        if select_menubar_backend(sys.platform, _mac_menubar_available()) == "mac":
            return self._run_mac_menubar()

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
            started_at_text_fn=self._started_text,
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
