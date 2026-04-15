# Service Mode: System Tray + Auto-Start — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `jacked service` command group that provides system tray icon + auto-start on login for macOS and Windows.

**Architecture:** New `jacked/service/` package with three modules — process management (PID file, port checks), tray icon (pystray + Pillow), and platform auto-start (launchd/Windows startup folder). CLI group in `cli.py` with lazy imports. New `[tray]` optional dependency.

**Tech Stack:** pystray>=0.19, Pillow>=9.0, launchd (macOS), VBScript startup shortcut (Windows)

---

## File Structure

```
jacked/service/
  __init__.py       — Package marker, shared constants (PID_FILE, SERVICE_LOG, DEFAULT_PORT)
  process.py        — PID file read/write/check/remove, port check, process signaling
  tray.py           — Icon rendering, menu construction, main loop with uvicorn thread
  platform.py       — macOS launchd + Windows startup folder install/uninstall/detect

tests/unit/service/
  __init__.py
  test_process.py   — PID file lifecycle, port checks, stale detection
  test_tray.py      — Icon generation, menu items, state transitions
  test_platform.py  — Plist generation, VBS generation, detection logic
```

CLI additions go directly in `jacked/cli.py` (following existing pattern of groups like `gatekeeper`, `profiles`).

---

### Task 1: Dependencies and Package Scaffold

**Files:**
- Modify: `pyproject.toml`
- Create: `jacked/service/__init__.py`
- Create: `tests/unit/service/__init__.py`

- [ ] **Step 1: Add `[tray]` optional dependency to pyproject.toml**

In `pyproject.toml`, add the tray extra and include it in `[all]`:

```toml
[project.optional-dependencies]
search = [
    "qdrant-client>=1.7.0",
]
security = []  # backwards compat — anthropic is now a core dependency
web = []
tray = [
    "pystray>=0.19",
    "Pillow>=9.0",
]
all = [
    "qdrant-client>=1.7.0",
    "pystray>=0.19",
    "Pillow>=9.0",
]
```

- [ ] **Step 2: Create `jacked/service/__init__.py` with shared constants**

```python
"""Service mode: system tray + auto-start for jacked webux."""

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PID_FILE = CLAUDE_DIR / "jacked-service.pid"
SERVICE_LOG = CLAUDE_DIR / "jacked-service.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
LAUNCHD_LABEL = "ai.hank.jacked"
```

- [ ] **Step 3: Create test package marker**

```python
# tests/unit/service/__init__.py
```

Empty file.

- [ ] **Step 4: Install tray deps locally for development**

Run: `uv pip install pystray Pillow`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml jacked/service/__init__.py tests/unit/service/__init__.py
git commit -m "feat(service): scaffold service package and add tray dependencies"
```

---

### Task 2: Process Management (`jacked/service/process.py`)

**Files:**
- Create: `jacked/service/process.py`
- Create: `tests/unit/service/test_process.py`

- [ ] **Step 1: Write failing tests for PID file management**

```python
# tests/unit/service/test_process.py
"""Tests for jacked.service.process module."""

import os
import signal
from unittest.mock import patch

import pytest


class TestWritePid:
    """Tests for write_pid()."""

    def test_writes_pid_to_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        from jacked.service.process import write_pid

        write_pid(pid_file, port=8321)

        content = pid_file.read_text().strip()
        lines = content.split("\n")
        assert lines[0] == str(os.getpid())
        assert lines[1] == "8321"

    def test_creates_parent_dirs(self, tmp_path):
        pid_file = tmp_path / "sub" / "dir" / "test.pid"
        from jacked.service.process import write_pid

        write_pid(pid_file, port=8321)
        assert pid_file.exists()

    def test_overwrites_existing(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999\n1234")
        from jacked.service.process import write_pid

        write_pid(pid_file, port=5555)

        lines = pid_file.read_text().strip().split("\n")
        assert lines[0] == str(os.getpid())
        assert lines[1] == "5555"


class TestReadPid:
    """Tests for read_pid()."""

    def test_reads_valid_pid_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321")
        from jacked.service.process import read_pid

        result = read_pid(pid_file)
        assert result == {"pid": 12345, "port": 8321}

    def test_returns_none_for_missing_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import read_pid

        assert read_pid(pid_file) is None

    def test_returns_none_for_corrupt_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not a number")
        from jacked.service.process import read_pid

        assert read_pid(pid_file) is None

    def test_handles_pid_only_no_port(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        from jacked.service.process import read_pid

        result = read_pid(pid_file)
        assert result == {"pid": 12345, "port": 8321}


class TestRemovePid:
    """Tests for remove_pid()."""

    def test_removes_existing_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321")
        from jacked.service.process import remove_pid

        remove_pid(pid_file)
        assert not pid_file.exists()

    def test_no_error_on_missing_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import remove_pid

        remove_pid(pid_file)  # should not raise


class TestIsProcessAlive:
    """Tests for is_process_alive()."""

    def test_current_process_is_alive(self):
        from jacked.service.process import is_process_alive

        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self):
        from jacked.service.process import is_process_alive

        assert is_process_alive(999999999) is False


class TestCheckPort:
    """Tests for is_port_available()."""

    def test_unused_port_is_available(self):
        from jacked.service.process import is_port_available

        # Port 0 lets the OS pick a free port — but we test a high ephemeral port
        assert is_port_available("127.0.0.1", 59999) is True

    def test_used_port_is_not_available(self):
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
        try:
            from jacked.service.process import is_port_available

            assert is_port_available("127.0.0.1", port) is False
        finally:
            sock.close()


class TestStopProcess:
    """Tests for stop_process()."""

    def test_returns_false_for_no_pid_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import stop_process

        assert stop_process(pid_file) is False

    def test_removes_stale_pid_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("999999999\n8321")
        from jacked.service.process import stop_process

        result = stop_process(pid_file)
        assert result is False
        assert not pid_file.exists()

    @patch("os.kill")
    def test_sends_sigterm_on_unix(self, mock_kill, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(f"{os.getpid()}\n8321")
        from jacked.service.process import stop_process

        with patch("jacked.service.process.is_process_alive", return_value=True):
            with patch("sys.platform", "darwin"):
                result = stop_process(pid_file)

        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        assert result is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_process.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.service.process'`

- [ ] **Step 3: Implement `jacked/service/process.py`**

```python
"""PID file management, port checking, and process lifecycle."""

import os
import signal
import socket
import sys
from pathlib import Path

from jacked.service import DEFAULT_PORT


def write_pid(pid_file: Path, port: int = DEFAULT_PORT) -> None:
    """Write current PID and port to the PID file."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n{port}")


def read_pid(pid_file: Path) -> dict | None:
    """Read PID and port from PID file. Returns None if missing/corrupt."""
    if not pid_file.exists():
        return None
    try:
        text = pid_file.read_text().strip()
        lines = text.split("\n")
        pid = int(lines[0])
        port = int(lines[1]) if len(lines) > 1 else DEFAULT_PORT
        return {"pid": pid, "port": port}
    except (ValueError, IndexError):
        return None


def remove_pid(pid_file: Path) -> None:
    """Remove PID file if it exists."""
    pid_file.unlink(missing_ok=True)


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)  # Signal 0 = just check existence
        return True
    except (OSError, ProcessLookupError):
        return False


def is_port_available(host: str, port: int) -> bool:
    """Check if a TCP port is available for binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def stop_process(pid_file: Path) -> bool:
    """Stop the service by reading PID file and sending signal.

    Returns True if a signal was sent, False if no process found.
    Removes stale PID files.
    """
    info = read_pid(pid_file)
    if info is None:
        return False

    pid = info["pid"]
    if not is_process_alive(pid):
        remove_pid(pid_file)
        return False

    if sys.platform == "win32":
        # On Windows, use taskkill
        import subprocess

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
        )
    else:
        os.kill(pid, signal.SIGTERM)

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/service/test_process.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/service/process.py tests/unit/service/test_process.py
git commit -m "feat(service): add PID file management and process lifecycle"
```

---

### Task 3: Tray Icon Rendering and Menu (`jacked/service/tray.py`)

**Files:**
- Create: `jacked/service/tray.py`
- Create: `tests/unit/service/test_tray.py`

- [ ] **Step 1: Write failing tests for icon generation and menu**

```python
# tests/unit/service/test_tray.py
"""Tests for jacked.service.tray module."""

import pytest
from unittest.mock import patch, MagicMock


def _skip_if_no_tray():
    """Skip test if pystray/Pillow not installed."""
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        pytest.skip("pystray/Pillow not installed")


class TestCreateIcon:
    """Tests for create_icon_image()."""

    def test_running_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image

        img = create_icon_image("running")
        assert img.size == (64, 64)

    def test_stopped_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image

        img = create_icon_image("stopped")
        assert img.size == (64, 64)

    def test_starting_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image

        img = create_icon_image("starting")
        assert img.size == (64, 64)

    def test_different_states_produce_different_images(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image

        running = create_icon_image("running")
        stopped = create_icon_image("stopped")
        assert running.tobytes() != stopped.tobytes()

    def test_unknown_state_defaults_to_stopped(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image

        unknown = create_icon_image("bogus")
        stopped = create_icon_image("stopped")
        assert unknown.tobytes() == stopped.tobytes()


class TestBuildMenu:
    """Tests for build_menu()."""

    def test_menu_has_expected_items(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu

        noop = lambda: None
        menu = build_menu(
            port=8321,
            version="0.39.0",
            autostart_check=lambda: True,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
        )
        # pystray.Menu is iterable
        items = list(menu)
        texts = [str(item) for item in items]
        # Check key items exist (pystray MenuItem.__str__ returns the text)
        assert any("Dashboard" in t for t in texts)
        assert any("Restart" in t for t in texts)
        assert any("Stop" in t for t in texts)
        assert any("Login" in t for t in texts)
        assert any("0.39.0" in t for t in texts)


class TestServiceRunner:
    """Tests for ServiceRunner lifecycle."""

    def test_init_stores_config(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=9999)
        assert runner.host == "127.0.0.1"
        assert runner.port == 9999

    @patch("jacked.service.tray.pystray")
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_thread_is_daemon(self, mock_uvicorn, mock_pystray):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        thread = runner._start_uvicorn()
        assert thread.daemon is True
        thread.join(timeout=0.1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.service.tray'`

- [ ] **Step 3: Implement `jacked/service/tray.py`**

```python
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
        self._icon: "pystray.Icon | None" = None
        self._autostart_enabled = False

    def _start_uvicorn(self) -> threading.Thread:
        """Start uvicorn in a daemon thread."""
        os.environ["JACKED_HOST"] = self.host
        os.environ["JACKED_PORT"] = str(self.port)

        def _run():
            config = uvicorn.Config(
                "jacked.api.main:app",
                host=self.host,
                port=self.port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            self._uvicorn_server = server
            server.run()

        thread = threading.Thread(target=_run, name="jacked-uvicorn", daemon=True)
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
        # Stop uvicorn
        if hasattr(self, "_uvicorn_server"):
            self._uvicorn_server.should_exit = True
        if self._uvicorn_thread:
            self._uvicorn_thread.join(timeout=5)
        # Restart
        self._uvicorn_thread = self._start_uvicorn()
        self._wait_for_ready()
        if self._icon:
            self._icon.icon = create_icon_image("running")

    def _on_stop(self):
        if hasattr(self, "_uvicorn_server"):
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
            icon.notify("Jacked failed to start", "Jacked Service")

    def run(self) -> None:
        """Start the service: tray icon on main thread, uvicorn in background."""
        check_tray_deps()

        if not is_port_available(self.host, self.port):
            raise SystemExit(
                f"Port {self.port} is already in use.\n"
                "Is another jacked instance running? Check with: jacked service status\n"
                "Use --port to run on a different port."
            )

        write_pid(PID_FILE, self.port)

        # Register signal handler for clean shutdown
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "feat(service): add system tray icon rendering and menu"
```

---

### Task 4: Platform Auto-Start (`jacked/service/platform.py`)

**Files:**
- Create: `jacked/service/platform.py`
- Create: `tests/unit/service/test_platform.py`

- [ ] **Step 1: Write failing tests for platform auto-start**

```python
# tests/unit/service/test_platform.py
"""Tests for jacked.service.platform module."""

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestGenerateLaunchdPlist:
    """Tests for _generate_launchd_plist()."""

    def test_contains_label(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/usr/local/bin/jacked",
            host="127.0.0.1",
            port=8321,
        )
        assert "ai.hank.jacked" in plist

    def test_contains_binary_path(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/opt/bin/jacked",
            host="127.0.0.1",
            port=8321,
        )
        assert "/opt/bin/jacked" in plist

    def test_contains_run_at_load(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/usr/local/bin/jacked",
            host="127.0.0.1",
            port=8321,
        )
        assert "<key>RunAtLoad</key>" in plist
        assert "<true/>" in plist

    def test_contains_keep_alive(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/usr/local/bin/jacked",
            host="127.0.0.1",
            port=8321,
        )
        assert "<key>KeepAlive</key>" in plist

    def test_custom_port_in_args(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/usr/local/bin/jacked",
            host="127.0.0.1",
            port=9000,
        )
        assert "9000" in plist

    def test_log_path_in_plist(self):
        from jacked.service.platform import _generate_launchd_plist

        plist = _generate_launchd_plist(
            jacked_bin="/usr/local/bin/jacked",
            host="127.0.0.1",
            port=8321,
        )
        assert "jacked-service.log" in plist


class TestGenerateWindowsVbs:
    """Tests for _generate_windows_vbs()."""

    def test_contains_jacked_path(self):
        from jacked.service.platform import _generate_windows_vbs

        vbs = _generate_windows_vbs(
            jacked_bin=r"C:\Users\test\.local\bin\jacked.exe",
            host="127.0.0.1",
            port=8321,
        )
        assert r"C:\Users\test\.local\bin\jacked.exe" in vbs

    def test_hidden_window(self):
        from jacked.service.platform import _generate_windows_vbs

        vbs = _generate_windows_vbs(
            jacked_bin=r"C:\bin\jacked.exe",
            host="127.0.0.1",
            port=8321,
        )
        # The second argument to WshShell.Run is 0 (hidden window)
        assert ", 0," in vbs

    def test_custom_port_in_vbs(self):
        from jacked.service.platform import _generate_windows_vbs

        vbs = _generate_windows_vbs(
            jacked_bin=r"C:\bin\jacked.exe",
            host="127.0.0.1",
            port=9000,
        )
        assert "--port 9000" in vbs


class TestDetectAutostart:
    """Tests for detect_autostart()."""

    @patch("sys.platform", "darwin")
    def test_darwin_detects_plist(self, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        plist.write_text("<plist>test</plist>")
        from jacked.service.platform import detect_autostart

        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            assert detect_autostart() is True

    @patch("sys.platform", "darwin")
    def test_darwin_no_plist(self, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import detect_autostart

        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            assert detect_autostart() is False

    @patch("sys.platform", "win32")
    def test_win32_detects_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        vbs.write_text("test")
        from jacked.service.platform import detect_autostart

        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            assert detect_autostart() is True

    @patch("sys.platform", "win32")
    def test_win32_no_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        from jacked.service.platform import detect_autostart

        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            assert detect_autostart() is False


class TestInstallAutostart:
    """Tests for install_autostart()."""

    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_writes_plist(self, mock_run, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart

        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("shutil.which", return_value="/usr/local/bin/jacked"):
                install_autostart("127.0.0.1", 8321)

        assert plist.exists()
        content = plist.read_text()
        assert "ai.hank.jacked" in content
        # launchctl load should have been called
        mock_run.assert_called_once()

    @patch("sys.platform", "win32")
    def test_win32_writes_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        from jacked.service.platform import install_autostart

        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            with patch("shutil.which", return_value=r"C:\bin\jacked.exe"):
                install_autostart("127.0.0.1", 8321)

        assert vbs.exists()
        content = vbs.read_text()
        assert "jacked.exe" in content


class TestUninstallAutostart:
    """Tests for uninstall_autostart()."""

    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_removes_plist(self, mock_run, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        plist.write_text("<plist>test</plist>")
        from jacked.service.platform import uninstall_autostart

        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            uninstall_autostart()

        assert not plist.exists()
        mock_run.assert_called_once()

    @patch("sys.platform", "win32")
    def test_win32_removes_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        vbs.write_text("test")
        from jacked.service.platform import uninstall_autostart

        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            uninstall_autostart()

        assert not vbs.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_platform.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.service.platform'`

- [ ] **Step 3: Implement `jacked/service/platform.py`**

```python
"""Platform-specific auto-start install/uninstall for macOS and Windows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from jacked.service import CLAUDE_DIR, DEFAULT_HOST, DEFAULT_PORT, LAUNCHD_LABEL


def _get_launchd_plist_path() -> Path:
    """Return path to the launchd plist file."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _get_windows_startup_path() -> Path:
    """Return path to the Windows startup VBS script."""
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "jacked.vbs"
    )


def _generate_launchd_plist(
    jacked_bin: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Generate launchd plist XML for macOS auto-start."""
    log_path = str(CLAUDE_DIR / "jacked-service.log")
    current_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{jacked_bin}</string>
        <string>service</string>
        <string>start</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{current_path}</string>
    </dict>
</dict>
</plist>
"""


def _generate_windows_vbs(
    jacked_bin: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Generate VBScript for Windows startup folder."""
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{jacked_bin}"" service start'
        f" --host {host} --port {port}\", 0, False\n"
    )


def detect_autostart() -> bool:
    """Check if auto-start is currently configured."""
    if sys.platform == "darwin":
        return _get_launchd_plist_path().exists()
    elif sys.platform == "win32":
        return _get_windows_startup_path().exists()
    return False


def install_autostart(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Install platform auto-start configuration.

    Returns a human-readable status message.
    """
    jacked_bin = shutil.which("jacked")
    if not jacked_bin:
        return "Could not find 'jacked' binary on PATH. Is it installed?"

    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_content = _generate_launchd_plist(jacked_bin, host, port)
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
        )
        return f"Installed launchd agent: {plist_path}"

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        vbs_path.parent.mkdir(parents=True, exist_ok=True)
        vbs_content = _generate_windows_vbs(jacked_bin, host, port)
        vbs_path.write_text(vbs_content, encoding="utf-8")
        return f"Installed startup script: {vbs_path}"

    else:
        return (
            "Auto-start not supported on this platform. "
            "Run `jacked service start` manually."
        )


def uninstall_autostart() -> str:
    """Remove platform auto-start configuration.

    Returns a human-readable status message.
    """
    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        if plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
            )
            plist_path.unlink()
            return f"Removed launchd agent: {plist_path}"
        return "No launchd agent found — nothing to remove."

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        if vbs_path.exists():
            vbs_path.unlink()
            return f"Removed startup script: {vbs_path}"
        return "No startup script found — nothing to remove."

    else:
        return "Auto-start not supported on this platform."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/service/test_platform.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/service/platform.py tests/unit/service/test_platform.py
git commit -m "feat(service): add macOS launchd and Windows startup auto-start"
```

---

### Task 5: CLI Integration (`jacked service` group)

**Files:**
- Modify: `jacked/cli.py` (add `service` group after existing groups, near line ~2607)

- [ ] **Step 1: Write failing test for CLI commands**

Add to `tests/unit/service/test_process.py` (or create a new file — using existing since it's the entry point):

```python
# tests/unit/service/test_cli.py
"""Tests for jacked service CLI commands."""

from unittest.mock import patch, MagicMock
from click.testing import CliRunner

import pytest


class TestServiceStatus:
    """Tests for `jacked service status`."""

    def test_status_when_not_running(self, tmp_path):
        from jacked.cli import main

        runner = CliRunner()
        pid_file = tmp_path / "nope.pid"
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "status"])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    def test_status_when_running(self, tmp_path):
        import os

        from jacked.cli import main

        runner = CliRunner()
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(f"{os.getpid()}\n8321")
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "status"])

        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "8321" in result.output


class TestServiceStop:
    """Tests for `jacked service stop`."""

    def test_stop_when_not_running(self, tmp_path):
        from jacked.cli import main

        runner = CliRunner()
        pid_file = tmp_path / "nope.pid"
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "stop"])

        assert result.exit_code == 0
        assert "not running" in result.output.lower()


class TestServiceInstall:
    """Tests for `jacked service install`."""

    @patch("jacked.service.platform.install_autostart")
    def test_install_calls_platform(self, mock_install):
        from jacked.cli import main

        mock_install.return_value = "Installed launchd agent: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])

        assert result.exit_code == 0
        mock_install.assert_called_once()


class TestServiceUninstall:
    """Tests for `jacked service uninstall`."""

    @patch("jacked.service.platform.uninstall_autostart")
    def test_uninstall_calls_platform(self, mock_uninstall):
        from jacked.cli import main

        mock_uninstall.return_value = "Removed launchd agent: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "uninstall"])

        assert result.exit_code == 0
        mock_uninstall.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_cli.py -v`
Expected: FAIL — `No such command 'service'`

- [ ] **Step 3: Add `service` group and subcommands to `cli.py`**

Add the following after the `profiles` group (around line 2610 in `cli.py`). All service imports are lazy to avoid loading pystray/Pillow on every `jacked` invocation:

```python
@main.group()
def service():
    """Manage the jacked background service (tray icon + auto-start)."""
    pass


@service.command(name="start")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
def service_start(host: str, port: int):
    """Start jacked as a background service with system tray icon."""
    from jacked.service.tray import ServiceRunner

    runner = ServiceRunner(host=host, port=port)
    runner.run()


@service.command(name="stop")
def service_stop():
    """Stop the running jacked service."""
    from jacked.service import PID_FILE
    from jacked.service.process import stop_process

    if stop_process(PID_FILE):
        console.print("[green][OK][/green] Sent stop signal to jacked service")
    else:
        console.print("[yellow]Service is not running[/yellow]")


@service.command(name="restart")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
@click.pass_context
def service_restart(ctx, host: str, port: int):
    """Restart the jacked service."""
    from jacked.service import PID_FILE
    from jacked.service.process import stop_process

    if stop_process(PID_FILE):
        console.print("[dim]Stopped existing service[/dim]")
        import time
        time.sleep(1)

    ctx.invoke(service_start, host=host, port=port)


@service.command(name="status")
def service_status():
    """Show whether the jacked service is running."""
    from jacked.service import PID_FILE
    from jacked.service.process import read_pid, is_process_alive
    from jacked.service.platform import detect_autostart

    info = read_pid(PID_FILE)
    autostart = detect_autostart()
    autostart_label = "[green]enabled[/green]" if autostart else "[dim]disabled[/dim]"

    if info and is_process_alive(info["pid"]):
        import time
        pid_mtime = PID_FILE.stat().st_mtime
        uptime_secs = time.time() - pid_mtime
        hours, remainder = divmod(int(uptime_secs), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m" if hours else f"{minutes}m"

        console.print("[bold green]Jacked Service: running[/bold green]")
        console.print(f"  PID:       {info['pid']}")
        console.print(f"  Port:      {info['port']}")
        console.print(f"  Uptime:    {uptime}")
        console.print(f"  Autostart: {autostart_label}")
        console.print(f"  Dashboard: http://127.0.0.1:{info['port']}")
    else:
        console.print("[bold yellow]Jacked Service: stopped[/bold yellow]")
        console.print(f"  Autostart: {autostart_label}")
        if info:
            # Stale PID file
            from jacked.service.process import remove_pid
            remove_pid(PID_FILE)


@service.command(name="install")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
def service_install(host: str, port: int):
    """Configure jacked to start automatically on login."""
    from jacked.service.platform import install_autostart

    result = install_autostart(host, port)
    console.print(f"[green][OK][/green] {result}")


@service.command(name="uninstall")
def service_uninstall():
    """Remove jacked auto-start configuration."""
    from jacked.service.platform import uninstall_autostart

    result = uninstall_autostart()
    console.print(f"[green][OK][/green] {result}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/service/test_cli.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `uv run python -m pytest tests/ -v --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_cli.py
git commit -m "feat(service): add jacked service CLI group with start/stop/status/install"
```

---

### Task 6: Manual Smoke Test

**Files:** None (verification only)

- [ ] **Step 1: Test `jacked service status` when not running**

Run: `uv run python -m jacked service status`
Expected: Shows "Jacked Service: stopped" with autostart status

- [ ] **Step 2: Test `jacked service start`**

Run: `uv run python -m jacked service start`
Expected: Tray icon appears in menu bar (macOS) or system tray (Windows). Icon transitions from amber to purple. Dashboard accessible at http://localhost:8321.

- [ ] **Step 3: Test tray menu**

Right-click the tray icon:
- "Open Dashboard" opens browser to localhost:8321
- "Restart" flashes amber then back to purple
- "Stop" removes icon and exits

- [ ] **Step 4: Test `jacked service install`**

Run: `uv run python -m jacked service install`
Expected on macOS: Prints path to plist, verify file at `~/Library/LaunchAgents/ai.hank.jacked.plist`

- [ ] **Step 5: Test `jacked service uninstall`**

Run: `uv run python -m jacked service uninstall`
Expected: Plist removed, confirm with `ls ~/Library/LaunchAgents/ai.hank.jacked.plist`

- [ ] **Step 6: Test `jacked service stop` from another terminal**

Start service in terminal A, run `jacked service stop` in terminal B.
Expected: Service in terminal A exits cleanly.

- [ ] **Step 7: Commit any fixes from smoke testing**

```bash
git add -u
git commit -m "fix(service): smoke test fixes"
```

---
