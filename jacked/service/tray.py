"""System tray icon and menu for jacked service mode."""

import os
import signal
import sys
import threading
import webbrowser
from pathlib import Path

from jacked import __version__
from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
from jacked.service.process import (
    is_port_available,
    remove_pid,
    write_pid,
)

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
) -> "pystray.Menu":
    """Build the tray right-click menu.

    Args:
        autostart_check: Callable returning bool — evaluated each time
            the menu is shown, so toggle changes are reflected live.
    """
    return pystray.Menu(
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
        pystray.MenuItem(f"v{version}", None, enabled=False),
    )


class ServiceRunner:
    """Manages the uvicorn server thread and pystray icon."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._stop_event = threading.Event()
        self._uvicorn_thread: threading.Thread | None = None
        self._uvicorn_server = None
        self._icon: "pystray.Icon | None" = None
        self._autostart_enabled = False

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
        if self._icon:
            self._icon.icon = create_icon_image("starting")
        # Stop existing server
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._uvicorn_thread:
            self._uvicorn_thread.join(timeout=5)
        # Restart with error handling
        try:
            self._uvicorn_thread = self._start_uvicorn()
            if self._wait_for_ready():
                if self._icon:
                    self._icon.icon = create_icon_image("running")
            else:
                if self._icon:
                    self._icon.icon = create_icon_image("stopped")
        except Exception:
            if self._icon:
                self._icon.icon = create_icon_image("stopped")

    def _on_stop(self):
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._uvicorn_thread:
            self._uvicorn_thread.join(timeout=5)
        remove_pid(PID_FILE)
        self._stop_event.set()
        if self._icon:
            self._icon.stop()

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

    def _setup(self, icon: "pystray.Icon"):
        """pystray setup callback — runs after icon appears."""
        icon.visible = True
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

        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, lambda *_: self._on_stop())

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
        )

        self._icon = pystray.Icon(
            name="jacked",
            icon=create_icon_image("starting"),
            title="Jacked",
            menu=menu,
        )
        self._icon.run(setup=self._setup)
