"""System tray icon and menu for jacked service mode."""

import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import replace

from jacked import __version__
from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
from jacked.service.process import (
    is_port_available,
    write_pid,
)
from jacked.version_check import check_version_cached

logger = logging.getLogger(__name__)

# PIL and pystray are guarded separately: icon rendering is pure Pillow and
# works headless (the mark renderer lives in jacked.service.icon), while
# pystray needs a display — it resolves its GUI backend at import time, and
# on a headless box (no $DISPLAY) that raises Xlib.error.DisplayNameError,
# not ImportError.
try:
    from PIL import Image, ImageDraw

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import pystray

    _TRAY_AVAILABLE = _PIL_AVAILABLE
except Exception:
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


# Icon fill per service state. The mark is a bare silhouette (no box), so
# one solid color per state is the whole scheme. Green = up, amber =
# starting, gray = stopped.
_ICON_COLORS = {
    "running": (34, 197, 94, 255),
    "starting": (245, 158, 11, 255),
    "stopped": (130, 130, 130, 255),
}

# If Win32 refuses to unregister a console callback, Windows still owns its
# raw address. Root it for the rest of the process so GC can never free it.
_FAILED_WIN_CTRL_HANDLERS: list[object] = []


def check_tray_deps() -> None:
    """Raise with install instructions if tray deps missing."""
    if not _TRAY_AVAILABLE:
        raise SystemExit(
            "Service mode requires the [tray] extra.\n"
            'Install it with: uv tool install "claude-jacked[tray]" --force'
        )


def create_icon_image(state: str, update_available: bool = False) -> "Image.Image":
    """Generate a 64x64 tray icon: the jacked arm mark tinted by state.

    The mark is a bold silhouette (see jacked.service.icon) — bold geometry
    survives the OS downscale to the 16px Windows tray, where the previous
    font-rendered "J" glyph turned into an invisible smudge.

    Args:
        state: One of 'running', 'starting', 'stopped'.
        update_available: When True, draw an update-available badge in the
            top-right — parity with the macOS menu-bar icon
            (menubar_mac.render_status_icon).
    """
    from jacked.service.icon import render_mark

    fill = _ICON_COLORS.get(state, _ICON_COLORS["stopped"])
    size = 64
    img = render_mark(fill, size)
    draw = ImageDraw.Draw(img)

    if update_available:
        # Same white-ring + blue-dot badge as menubar_mac, scaled for the
        # 64px canvas (mac uses a 44px canvas).
        cx, cy, r = size - 13, 13, 10
        ring = r + 3
        draw.ellipse(
            [cx - ring, cy - ring, cx + ring, cy + ring], fill=(255, 255, 255, 255)
        )
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(59, 130, 246, 255))

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
                lambda _: started_at_text_fn(),
                None,
                enabled=False,
            )
        )
    items.extend(
        [
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
    )
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
            items.append(pystray.MenuItem("Check for updates...", on_check_for_updates))

    return pystray.Menu(*items)


class ServiceRunner:
    """Manages the uvicorn server thread and pystray icon."""

    def __init__(self, host: str | None = None, port: int = DEFAULT_PORT):
        # `cli_host` is the one-shot explicit --host override (None = "no flag",
        # let resolve_bind consult the DB / loopback default). `host` stays as a
        # display / back-compat attribute; it is overwritten from the resolved
        # plan's primary_host on every _start_uvicorn, but is initialized here so
        # nothing (menu labels, port guards) reads None before resolution.
        self.cli_host = host
        self.host = host or DEFAULT_HOST
        self.port = port
        # The BindPlan from the most recent _start_uvicorn. None until the first
        # start, so readiness/port-free probes fall back to loopback.
        self.bind_plan = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = (
            threading.RLock()
        )  # reentrant: update click holds through _on_stop
        self._uvicorn_thread: threading.Thread | None = None
        self._uvicorn_server = None
        self._icon: "pystray.Icon | None" = None
        # Last state passed to _apply_icon, so a version-check refresh can
        # re-render the current state with the update badge without knowing
        # whether we're running/starting/stopped.
        self._icon_state: str = "starting"
        self._autostart_enabled = False
        self._version_info: dict | None = None
        # Visible last-checked timestamp + in-progress flag for the tray menu,
        # so users can see the check happened even when system notifications
        # are muted.
        self._last_check_at: float | None = None
        self._version_check_in_progress: bool = False
        # True when the most recent MANUAL check couldn't reach PyPI (info None).
        # Read by the mac menubar to show a "couldn't reach" result, since its
        # rumps path has no pystray notification.
        self._last_check_failed: bool = False
        # Wall-clock timestamp when the current uvicorn thread became ready
        # (passed _wait_for_ready). Used by the tray menu so users can verify
        # a Restart click actually took effect — the timestamp shifts on
        # every successful start/restart.
        self._started_at: float | None = None
        # v2 process ownership. Initialized at run() before any bind attempt;
        # kept optional so low-level unit tests can exercise _start_uvicorn in
        # isolation without touching user state.
        self._ownership = None
        self._service_spec = None
        self._service_environment: dict[str, str] | None = None
        self._control_server = None
        self._server_ready = False
        self._service_state = "starting"
        self._lifecycle_failure: str | None = None
        self._win_ctrl_handler = None

    @staticmethod
    def _resolve_active_legacy_service_pid() -> int | None:
        """Return a live PID only when legacy jacked health corroborates it."""
        from jacked.service.legacy import resolve_active_legacy_service

        evidence = resolve_active_legacy_service(PID_FILE)
        if evidence is None or evidence.pid == os.getpid():
            return None
        return evidence.pid

    def _guard_legacy_pid_or_exit(self) -> None:
        """Stop duplicate launch when a live legacy tray process is already running."""
        pid = self._resolve_active_legacy_service_pid()
        if pid is None:
            return
        logger.info(
            "Another live jacked tray owns the compatibility PID file (pid=%d); "
            "yielding before UI startup",
            pid,
        )
        # A clean exit is essential for launchd/systemd restart-on-failure
        # policies. Treat singleton contention as an idempotent no-op for both
        # managed and direct starters instead of creating a supervisor loop.
        raise SystemExit(0)

    @staticmethod
    def _legacy_jacked_listener_detected(host: str, port: int) -> bool:
        """Recognize old jacked only when private stale PID evidence points here."""

        from jacked.service.legacy import inspect_legacy_pid, probe_legacy_health
        from jacked.service.process import is_v2_pid_evidence

        evidence = inspect_legacy_pid(PID_FILE)
        return bool(
            evidence
            and not is_v2_pid_evidence(PID_FILE)
            and not evidence.alive
            and evidence.port == port
            and probe_legacy_health(host, port)
        )

    def _start_uvicorn(self, cold_start: bool = False) -> threading.Thread:
        """Resolve the bind plan, pre-bind its sockets, and serve in a daemon
        thread.

        Re-resolving here (not in ``__init__``) is deliberate: the tray's
        in-process Restart re-reads the settings DB, so a GUI remote-access
        toggle takes effect on the next restart without a process replacement.

        ``create_sockets`` runs in the CALLING thread so a bind failure
        (EADDRINUSE / a vanished pinned IP) surfaces synchronously instead of
        dying invisibly inside the daemon thread the way ``server.run()`` used
        to. uvicorn then ``listen()``s the pre-bound sockets we hand it.

        On a bind failure: at COLD START (``cold_start=True``, the boot paths)
        we raise ``SystemExit`` with the friendly "port in use" message so the
        service exits cleanly. On a RESTART (default) we raise ``OSError`` so
        ``_on_restart``'s retry loop — which catches ``Exception``, not
        ``BaseException`` — can catch it, retry, and fall back to the "stopped"
        icon instead of the exception escaping and stranding a dead dashboard.
        """
        # Close the gap between the run-level admission check and socket bind.
        # An older jacked process can appear during that window and does not
        # know about the v2 lease, so re-check before reserving any endpoint.
        if cold_start and self._ownership is not None:
            self._guard_legacy_pid_or_exit()

        from jacked.service.bind import (
            create_sockets,
            resolve_bind,
            set_active_plan,
        )

        plan = resolve_bind(self.cli_host, self.port)
        self.bind_plan = plan
        self.host = plan.primary_host
        try:
            socks = create_sockets(plan)
        except OSError as exc:
            if cold_start and self._ownership is not None:
                # A pre-v2 service may have started between the admission check
                # and bind. Prefer its one existing tray over a second v2 tray.
                # The HTTP probe is refusal-only and never controls the listener.
                self._guard_legacy_pid_or_exit()
                if self._legacy_jacked_listener_detected(
                    "127.0.0.1", self.port
                ):
                    logger.info(
                        "A legacy jacked listener already owns port %d; yielding "
                        "before quarantine UI startup",
                        self.port,
                    )
                    raise SystemExit(0) from exc
                # The fixed listener is ambiguous. Never kill its owner and
                # never race it. Bind a loopback-only dynamic port and publish
                # that endpoint in the private manifest for CLI/tray discovery.
                from jacked.service.bind import BindPlan

                quarantine = BindPlan(
                    mode="quarantine",
                    addresses=("127.0.0.1",),
                    port=0,
                    primary_host="127.0.0.1",
                    fallback_reason=(
                        f"Port {self.port} is occupied without v2 ownership proof"
                    ),
                )
                socks = create_sockets(quarantine)
                actual_port = socks[0].getsockname()[1]
                plan = replace(quarantine, port=actual_port)
                self.port = actual_port
                self.bind_plan = plan
                self.host = plan.primary_host
                logger.warning(
                    "Port conflict: started owned service in quarantine on 127.0.0.1:%d",
                    actual_port,
                )
            elif cold_start:
                raise SystemExit(
                    f"Port {self.port} is already in use.\n"
                    "Is another jacked instance running? Check with: jacked service status\n"
                    "Use --port to run on a different port."
                ) from exc
            # Restart path: propagate as OSError so _on_restart's retry loop
            # (except Exception) can catch it, retry, and show the "stopped"
            # state instead of a BaseException escaping the loop.
            else:
                raise

        # Publish only after sockets are successfully reserved. The manifest
        # therefore never advertises a port this process failed to bind.
        set_active_plan(plan)
        os.environ["JACKED_HOST"] = plan.primary_host
        os.environ["JACKED_PORT"] = str(self.port)
        # Keep the two-release legacy diagnostic file from clobbering a live
        # pre-v2 owner that started during our admission window. If another
        # live PID won, release the sockets and yield before publishing v2 state.
        if not write_pid(PID_FILE, self.port):
            if self._resolve_active_legacy_service_pid() is not None:
                for sock in socks:
                    sock.close()
                logger.info(
                    "Compatibility PID publication found a healthy legacy jacked "
                    "process; yielding before UI startup"
                )
                if cold_start:
                    raise SystemExit(0)
                raise OSError("another live jacked process owns the compatibility PID")
            logger.warning(
                "Ignoring uncorroborated compatibility PID evidence; v2 ownership "
                "and reserved sockets remain authoritative"
            )

        if self._ownership is not None:
            from jacked.service.instance import BindIdentity

            self._ownership.publish(
                BindIdentity(
                    host="127.0.0.1",
                    port=self.port,
                    quarantine=plan.mode == "quarantine",
                )
            )
            if self._control_server is None:
                from jacked.service.ipc import create_control_server

                self._control_server = create_control_server(
                    self._ownership.manifest.control_address,
                    manifest_provider=lambda: self._ownership.manifest,
                    handler=self._handle_control_action,
                )
                try:
                    self._control_server.start()
                except BaseException:
                    for sock in socks:
                        sock.close()
                    self._control_server = None
                    raise
        config = uvicorn.Config(
            "jacked.api.main:app",
            host=plan.primary_host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        thread = threading.Thread(
            target=lambda: server.run(sockets=socks),
            name="jacked-uvicorn",
            daemon=True,
        )
        thread.start()
        return thread

    def _wait_for_ready(self, timeout: float = 10.0) -> bool:
        """Poll until the server is accepting connections."""
        import socket
        import time

        probe_host = (
            self.bind_plan.probe_host if self.bind_plan is not None else "127.0.0.1"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((probe_host, self.port), timeout=0.5)
                sock.close()
                return True
            except OSError:
                time.sleep(0.3)
        return False

    def _abort_unready_start(self) -> None:
        """Stop before any tray UI exists when startup never became ready."""

        logger.error("Service did not become ready; aborting before UI startup")
        self._service_state = "degraded"
        self._lifecycle_failure = "initial service start did not become ready"
        self._request_stop()
        if self._uvicorn_thread is not None:
            self._uvicorn_thread.join(timeout=5)
        from jacked.service.spec import SupervisorKind

        if (
            self._service_spec is not None
            and self._service_spec.supervisor is not SupervisorKind.MANUAL
        ):
            raise SystemExit(0)
        raise SystemExit(
            "Jacked did not become ready. Check ~/.claude/jacked-tray.log."
        )

    def _wait_for_port_free(self, timeout: float = 10.0) -> bool:
        """Poll until EVERY plan address is free to bind (old server released).

        After uvicorn's thread joins, the OS sockets may still be in TIME_WAIT
        for a few seconds. Binding too early raises OSError: Address already
        in use. We bind+close a probe socket with SO_REUSEADDR on each address
        the current plan listens on; the port is only "free" when they ALL bind.
        Falls back to loopback when no plan has been resolved yet (never started).

        An address that is no longer assignable on this host (``EADDRNOTAVAIL``)
        is treated as free, NOT as still-in-use: if a pinned Tailscale IP
        vanished (tailnet down, sleep/wake) it cannot be bound and it cannot be
        holding the port, so waiting the full timeout on it would only strand a
        restart that is about to re-resolve to a loopback-only plan. Only
        ``EADDRINUSE`` counts as genuinely occupied.
        """
        import errno
        import socket
        import time

        addresses = (
            self.bind_plan.addresses if self.bind_plan is not None else ("127.0.0.1",)
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probes: list = []
            all_free = True
            for addr in addresses:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((addr, self.port))
                    probes.append(sock)
                except OSError as exc:
                    sock.close()
                    # An unassignable address can't be holding the port; skip it.
                    # Anything else (notably EADDRINUSE) means keep waiting.
                    if exc.errno == errno.EADDRNOTAVAIL:
                        continue
                    all_free = False
                    break
            for sock in probes:
                sock.close()
            if all_free:
                return True
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
                graceful_elapsed,
                ws_client_count,
            )
            self._uvicorn_server.force_exit = True
            self._uvicorn_thread.join(timeout=5)
            if self._uvicorn_thread.is_alive():
                logger.warning(
                    "Uvicorn thread still alive after force_exit "
                    "(total elapsed %.1fs, ws_clients=%d) — proceeding "
                    "anyway; OS will reap the daemon thread",
                    time.monotonic() - graceful_start,
                    ws_client_count,
                )

    def _on_open_dashboard(self):
        # The tray runs on the same machine as uvicorn, and every non-cli plan
        # binds (or, for 0.0.0.0, covers) loopback, so 127.0.0.1 always reaches
        # it. Opening the resolved primary_host (a 100.x or 0.0.0.0) would be
        # wrong or unreachable, so always open loopback.
        webbrowser.open(f"http://127.0.0.1:{self.port}")

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

    def _apply_icon(self, state: str) -> None:
        """Render the tray icon for *state*, adding the update badge when an
        update is available. Centralizes icon assignment so state + badge
        can't drift across the several call sites that set the icon."""
        self._icon_state = state
        if self._icon is not None:
            self._icon.icon = create_icon_image(
                state, update_available=self._version_is_clickable()
            )

    def _on_restart(self):
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Already restarting or stopping
        try:
            self._server_ready = False
            self._service_state = "starting"
            self._lifecycle_failure = None
            if self._icon:
                self._apply_icon("starting")

            self._shutdown_uvicorn()

            if not self._wait_for_port_free(timeout=10):
                logger.error(
                    "Port %d did not free up after uvicorn shutdown — "
                    "another process may be using it. Aborting restart.",
                    self.port,
                )
                if self._icon:
                    self._apply_icon("stopped")
                self._service_state = "degraded"
                self._lifecycle_failure = "port did not become available"
                return

            # Retry the bind+ready cycle in case of transient failures
            # (e.g. lifespan startup hiccup).
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    self._uvicorn_thread = self._start_uvicorn()
                    if self._wait_for_ready(timeout=15):
                        self._server_ready = True
                        self._service_state = "running"
                        self._lifecycle_failure = None
                        self._started_at = time.time()
                        logger.info(
                            "Service ready after restart (pid=%d, port=%d, attempt=%d)",
                            os.getpid(),
                            self.port,
                            attempt + 1,
                        )
                        if self._icon:
                            self._apply_icon("running")
                            self._icon.update_menu()
                        return
                    logger.warning(
                        "Restart attempt %d: server did not become ready "
                        "within timeout",
                        attempt + 1,
                    )
                    self._shutdown_uvicorn()
                    self._wait_for_port_free(timeout=5)
                except Exception as exc:
                    last_err = exc
                    logger.exception(
                        "Restart attempt %d raised; will retry",
                        attempt + 1,
                    )
                    self._shutdown_uvicorn()
                    self._wait_for_port_free(timeout=5)
            logger.error("Restart failed after 3 attempts: %s", last_err)
            self._service_state = "degraded"
            self._lifecycle_failure = "restart failed after 3 attempts"
            if self._icon:
                self._apply_icon("stopped")
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

    def _on_settings_restart(self):
        """Apply a settings-triggered restart, honoring the DB over any launch pin.

        A settings apply (POST /api/settings/remote-access/restart) is an
        explicit request to honor the settings DB. But this process may have
        been LAUNCHED with ``--host X``: a stale launchd in-memory definition
        keeps its old baked-``--host`` argv until reboot even after the plist
        on disk was rewritten host-free, and an old detached spawn's argv
        lingers the same way. That launch-time pin lives in ``self.cli_host``
        and would win over the DB inside ``resolve_bind`` on every in-process
        restart, silently making the GUI toggle inert. Clearing the pin here
        makes the GUI work regardless of how this process was launched.

        The tray MENU's Restart keeps calling ``_on_restart`` directly: an
        operator's one-shot launch pin is deliberate for the life of that
        launch, and a plain restart must not discard it.
        """
        self.cli_host = None
        self._on_restart()

    def _request_stop(self):
        """Signal-safe stop request — only sets flags, no locks or I/O."""
        self._server_ready = False
        self._service_state = "stopping"
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
        if self._win_ctrl_handler is not None:
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

            callback = HandlerRoutine(_handler)
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCtrlHandler.argtypes = [HandlerRoutine, wintypes.BOOL]
            kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            if not kernel32.SetConsoleCtrlHandler(callback, True):
                logger.error("Could not install Windows console Ctrl handler")
                return
            # Keep a strong reference for exactly as long as Windows retains it.
            self._win_ctrl_handler = callback
        except Exception:
            logger.exception("Could not install Windows console Ctrl handler")

    def _uninstall_windows_console_handler(self) -> None:
        """Unregister the Win32 callback before releasing its ctypes object."""
        handler = self._win_ctrl_handler
        if sys.platform != "win32" or handler is None:
            return
        try:
            import ctypes

            if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, False):
                if not any(root is handler for root in _FAILED_WIN_CTRL_HANDLERS):
                    _FAILED_WIN_CTRL_HANDLERS.append(handler)
                logger.error("Could not uninstall Windows console Ctrl handler")
                return
            _FAILED_WIN_CTRL_HANDLERS[:] = [
                root for root in _FAILED_WIN_CTRL_HANDLERS if root is not handler
            ]
            self._win_ctrl_handler = None
        except Exception:
            if not any(root is handler for root in _FAILED_WIN_CTRL_HANDLERS):
                _FAILED_WIN_CTRL_HANDLERS.append(handler)
            logger.exception("Could not uninstall Windows console Ctrl handler")

    def _on_stop(self):
        """Full stop — called from menu or atexit, not from signal handler."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # Already stopping or restarting
        try:
            self._request_stop()
            if self._uvicorn_thread:
                self._uvicorn_thread.join(timeout=5)
            if self._icon:
                self._icon.stop()
        finally:
            self._lifecycle_lock.release()

    def _on_toggle_autostart(self):
        from jacked.service.lifecycle import (
            default_service_paths,
            install_native_owned,
            provision_service_contract,
            uninstall_native_owned,
        )
        from jacked.service.autostart import AutostartState
        from jacked.service.platform import inspect_autostart

        paths = default_service_paths()
        try:
            spec, environment = provision_service_contract(paths=paths)
            before = inspect_autostart()
            if not before.toggle_safe:
                message = (
                    f"Start-on-Login is {before.state.value}: {before.reason}. "
                    "Run `jacked service recover`."
                )
                logger.warning(message)
                if self._icon is not None:
                    self._icon.notify(message, "Jacked")
                return
            was_enabled = before.state is AutostartState.OWNED_ENABLED
            if was_enabled:
                result = uninstall_native_owned(
                    spec, environment=environment, paths=paths
                )
            else:
                result = install_native_owned(
                    spec, environment=environment, paths=paths
                )
        except (OSError, ValueError) as exc:
            message = (
                f"Start-on-Login failed safely: {type(exc).__name__}. "
                "Run `jacked service recover`."
            )
            logger.warning(message)
            if self._icon is not None:
                self._icon.notify(message, "Jacked")
            return
        if not result.ok:
            logger.warning("Start-on-Login toggle refused: %s", result.reason)
            if self._icon is not None:
                self._icon.notify(result.reason, "Jacked")
            return
        after = inspect_autostart()
        self._autostart_enabled = after.enabled
        expected_state = (
            AutostartState.ABSENT
            if was_enabled
            else AutostartState.OWNED_ENABLED
        )
        if after.state is not expected_state:
            message = (
                "Start-on-Login did not reach the expected manager state: "
                f"{after.state.value}. Run `jacked service recover`."
            )
            logger.warning(message)
            if self._icon is not None:
                self._icon.notify(message, "Jacked")

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
                info = check_version_cached(
                    __version__, force=force if first else False
                )
                if info is not None:
                    self._version_info = info
                # Always stamp the last-checked time (even on PyPI failure) so
                # the tray menu can reflect that a check just ran.
                self._last_check_at = _time.time()
                if self._icon and not self._stop_event.is_set():
                    try:
                        self._apply_icon(self._icon_state)
                        self._icon.update_menu()
                    except Exception:
                        pass  # some pystray backends fail during shutdown
            except Exception:
                logger.exception("Version check failed")
            first = False
            # Twice-daily background cadence (matches version_check CACHE_TTL) so
            # the update badge appears within ~12h of a release; manual "Check for
            # updates" fires this method in a one-shot thread that exits after this wait.
            if self._stop_event.wait(timeout=43200):
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
            self._last_check_failed = info is None
            self._version_check_in_progress = False

            if self._icon and not self._stop_event.is_set():
                try:
                    self._apply_icon(self._icon_state)
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
                            f"Update available: v{latest}",
                            "Jacked",
                        )
                    else:
                        self._icon.notify(
                            f"You're up to date (v{__version__})",
                            "Jacked",
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
                    self._apply_icon("starting")
                    self._icon.notify(
                        f"Updating jacked to v{latest}. If this fails, "
                        f"see ~/.claude/jacked-update-failed.txt",
                        "Jacked Update",
                    )
                except Exception:
                    logger.exception("Icon update during update-click failed")

            # Open a file:// bootstrap page that hosts /update.html in a
            # self-healing iframe. The tray stops the service moments from now,
            # so a direct browser GET to the port would race the dying server
            # and land on the browser's own dead error page (no JS, never
            # recovers). A local file cannot die; it pings 127.0.0.1 and
            # (re)loads the iframe once the new service binds.
            try:
                from jacked.api.main import WEB_DIR
                from jacked.service import CLAUDE_DIR

                template = (WEB_DIR / "update_bootstrap.html").read_text(
                    encoding="utf-8"
                )
                CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
                progress_path = CLAUDE_DIR / "jacked-update-progress.html"
                progress_path.write_text(
                    template.replace("__JACKED_PORT__", str(self.port)),
                    encoding="utf-8",
                )
                webbrowser.open(progress_path.as_uri())
            except Exception:
                # Template missing or unwritable — fall back to the port
                # directly (old behavior, minus the pointless pre-warm).
                logger.exception(
                    "Could not open update bootstrap page; using direct URL"
                )
                try:
                    webbrowser.open(f"http://127.0.0.1:{self.port}/update.html")
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
        if self._started_at is not None:
            self._apply_icon("running")
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
            self._apply_icon("stopped")
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
        # Probe loopback, not the eventual bind host: this guard runs BEFORE
        # resolve_bind, and every plan except a cli-pinned specific IP includes
        # or covers loopback. A cli specific-IP run still conflict-checks cleanly
        # at create_sockets time (which raises SystemExit with this same message).
        if self._ownership is None and not is_port_available("127.0.0.1", self.port):
            raise SystemExit(
                f"Port {self.port} is already in use.\n"
                "Is another jacked instance running? Check with: jacked service status\n"
                "Use --port to run on a different port."
            )

        # Signal-safe: just set the stop event; the agent's stop-watch timer
        # bridges that to a clean uvicorn shutdown + quit on the main thread.
        signal.signal(signal.SIGTERM, lambda *_: self._request_stop())
        signal.signal(signal.SIGINT, lambda *_: self._request_stop())

        from jacked.service.platform import detect_autostart

        self._autostart_enabled = detect_autostart()

        # Start the one uvicorn (daemon thread) and health-check it before the
        # status item appears, so the first pill reflects real data.
        self._uvicorn_thread = self._start_uvicorn(cold_start=True)
        if self._wait_for_ready():
            self._server_ready = True
            self._service_state = "running"
            self._lifecycle_failure = None
            self._started_at = time.time()
            logger.info(
                "Service ready — macOS menu-bar agent (pid=%d, port=%d)",
                os.getpid(),
                self.port,
            )
        else:
            self._abort_unready_start()

        MacMenuBarApp(self).run()

    def run(self) -> None:
        """Start the service: tray icon on main thread, uvicorn in background."""
        self._install_tray_file_logger()

        from jacked.service.lifecycle import (
            claim_service_ownership,
            provision_service_contract,
        )

        candidate, environment = provision_service_contract()
        if os.environ.get("JACKED_SERVICE_GENERATION") == candidate.generation:
            self._service_spec, self._service_environment = candidate, environment
        else:
            # A direct CLI/manual start has no exact supervisor generation
            # marker and must never claim native-manager provenance.
            from jacked.service.spec import SupervisorKind

            self._service_spec, self._service_environment = provision_service_contract(
                supervisor=SupervisorKind.MANUAL
            )
        from jacked.service.instance import ServiceLeaseBusy, ServiceOwnershipInvalid

        try:
            self._ownership = claim_service_ownership(self._service_spec)
        except ServiceLeaseBusy as exc:
            logger.info("Service starter lost the singleton lease; yielding: %s", exc)
            raise SystemExit(0) from exc
        except ServiceOwnershipInvalid as exc:
            logger.error("Service ownership recovery is required: %s", exc)
            from jacked.service.spec import SupervisorKind

            if self._service_spec.supervisor is not SupervisorKind.MANUAL:
                raise SystemExit(0) from exc
            raise SystemExit(
                "Jacked ownership is invalid. Run `jacked service recover`, then retry."
            ) from exc

        try:
            # The v2 lease is acquired first so v2-v2 contention never consults
            # a mutable compatibility file. Legacy admission follows while the
            # lease is held, preventing any new v2 tray from racing us to UI.
            self._guard_legacy_pid_or_exit()

            # Register the in-process restart handler for the WHOLE run lifetime,
            # so the settings API's POST /remote-access/restart can apply a bind
            # change (same PID, tray survives) instead of execv. Registered as
            # _on_settings_restart, NOT _on_restart: a settings apply must clear
            # any launch-time --host pin (stale launchd in-memory argv) so the DB
            # actually wins — see _on_settings_restart's docstring.
            # Shared across both backends below; the finally unregisters on exit.
            from jacked.service.restart import set_restart_handler

            set_restart_handler(self._on_settings_restart)
            try:
                return self._run()
            finally:
                set_restart_handler(None)
        finally:
            self._uninstall_windows_console_handler()
            if self._control_server is not None:
                self._control_server.close()
                self._control_server = None
            if self._ownership is not None:
                self._ownership.close()
                self._ownership = None

    def _handle_control_action(self, action):
        """Dispatch an authenticated native-control action."""
        from jacked.service.ipc import ControlAction

        if action is ControlAction.STATUS:
            manifest = self._ownership.manifest if self._ownership else None
            return {
                "state": self._service_state if manifest is not None else "stopping",
                "failure": self._lifecycle_failure,
                "instance_id": manifest.instance_id if manifest else None,
                "generation": manifest.generation if manifest else None,
                "build_version": manifest.build_version if manifest else None,
                "protocol_version": manifest.protocol_version if manifest else None,
                "port": manifest.bind.port if manifest else None,
                "quarantine": manifest.bind.quarantine if manifest else None,
            }
        if action is ControlAction.SHUTDOWN:
            self._request_stop()
            return {"accepted": True}
        if action is ControlAction.RESTART_HANDOFF:
            # A handoff must exit this interpreter so an upgraded package can
            # actually load. Tray/menu restart remains the in-process path.
            self._request_stop()
            return {"accepted": True}
        raise ValueError("unsupported control action")

    def _run(self) -> None:
        """Backend dispatch for :meth:`run` (wrapped there for handler setup)."""
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

        # Probe loopback, not the eventual bind host: this guard runs BEFORE
        # resolve_bind, and every plan except a cli-pinned specific IP includes
        # or covers loopback. A cli specific-IP run still conflict-checks cleanly
        # at create_sockets time (which raises SystemExit with this same message).
        if self._ownership is None and not is_port_available("127.0.0.1", self.port):
            raise SystemExit(
                f"Port {self.port} is already in use.\n"
                "Is another jacked instance running? Check with: jacked service status\n"
                "Use --port to run on a different port."
            )

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

        # Complete admission, socket reservation, manifest publication, and
        # health verification before constructing any tray object. A losing
        # mixed-version starter therefore cannot flash or strand a second icon.
        self._uvicorn_thread = self._start_uvicorn(cold_start=True)
        if self._wait_for_ready():
            self._server_ready = True
            self._service_state = "running"
            self._lifecycle_failure = None
            self._started_at = time.time()
            logger.info(
                "Service ready (pid=%d, port=%d, autostart=%s)",
                os.getpid(),
                self.port,
                self._autostart_enabled,
            )
        else:
            self._abort_unready_start()

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
        self._icon.run(setup=self._setup)
