"""Real-process regression for old-service/new-starter coexistence."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


_LEGACY_SERVER = r"""
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

pathlib.Path(sys.argv[1]).write_text("legacy-icon\n", encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"status": "ok", "db": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
"""


def test_old_listener_and_stale_pid_yield_before_second_icon(tmp_path, monkeypatch):
    """A mixed-generation bind collision keeps exactly the old tray visible."""
    import jacked.service.tray as tray_module
    from jacked.service.tray import ServiceRunner

    legacy_icon = tmp_path / "legacy-icon.txt"
    old = subprocess.Popen(
        [sys.executable, "-u", "-c", _LEGACY_SERVER, str(legacy_icon)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert old.stdout is not None
        port = int(old.stdout.readline().strip())
        pid_file = tmp_path / "jacked-service.pid"
        pid_file.write_text(f"999999999\n{port}", encoding="utf-8")
        monkeypatch.setattr(tray_module, "PID_FILE", pid_file)

        fake_pystray = MagicMock()
        for _attempt in range(3):
            runner = ServiceRunner(host="127.0.0.1", port=port)
            runner._ownership = MagicMock()
            with (
                patch.object(tray_module, "pystray", fake_pystray, create=True),
                patch.object(tray_module, "_TRAY_AVAILABLE", True),
                patch.object(tray_module, "_UVICORN_AVAILABLE", True),
                patch.object(
                    tray_module, "_mac_menubar_available", return_value=False
                ),
                patch("jacked.service.platform.detect_autostart", return_value=False),
                patch.object(tray_module.signal, "signal"),
                pytest.raises(SystemExit) as exc_info,
            ):
                runner._run()
            assert exc_info.value.code == 0
            runner._ownership.publish.assert_not_called()

        fake_pystray.Icon.assert_not_called()
        assert old.poll() is None
        assert legacy_icon.read_text(encoding="utf-8") == "legacy-icon\n"
        assert pid_file.read_text(encoding="utf-8") == f"999999999\n{port}"
    finally:
        old.terminate()
        old.wait(timeout=5)
