"""Integration test for the multi-socket serving model (milestone 2).

Proves the actual serving contract the tray + `jacked webux` now rely on:
uvicorn ``Server.run(sockets=[...])`` binds the REAL app to a set of pre-bound
sockets, and every socket in the set answers. We use two loopback sockets on
two distinct ephemeral ports (instead of loopback + a 100.x tailnet IP) so the
test needs no tailnet, while exercising the exact ``sockets=[...]`` code path.

Isolation: the app lifespan constructs ``Database()`` and opens
``~/.claude/jacked-server.log`` via ``Path.home()``. We redirect HOME /
USERPROFILE to a tmp dir so booting the lifespan never touches the user's real
~/.claude. (The service-dir conftest already isolates ``jacked.service.*``
paths, but NOT ``jacked.web.database`` / the lifespan's home-derived paths.)
"""

import socket
import threading
import time

import pytest

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

try:
    import uvicorn

    _UVICORN_AVAILABLE = True
except ImportError:
    _UVICORN_AVAILABLE = False


@pytest.fixture
def _isolate_home(tmp_path, monkeypatch):
    """Point Path.home() at a throwaway dir so the real app lifespan (DB +
    server log) writes only under tmp."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows Path.home()
    # Keep CORS/CSRF host resolution on loopback for the boot.
    monkeypatch.setenv("JACKED_HOST", "127.0.0.1")
    monkeypatch.setenv("JACKED_PORT", "8321")
    yield home


def _bind_ephemeral_loopback() -> tuple[socket.socket, int]:
    """A loopback socket bound (not listened) on an OS-assigned ephemeral port,
    matching what jacked.service.bind.create_sockets hands uvicorn."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


@pytest.mark.skipif(
    not (_UVICORN_AVAILABLE and _HTTPX_AVAILABLE),
    reason="requires uvicorn + httpx",
)
def test_two_prebound_sockets_both_answer_health(_isolate_home):
    """Serve the real app on two pre-bound loopback sockets; both /api/health
    endpoints return 200, then shut down cleanly (should_exit → force_exit)."""
    s1, p1 = _bind_ephemeral_loopback()
    s2, p2 = _bind_ephemeral_loopback()
    assert p1 != p2

    config = uvicorn.Config("jacked.api.main:app", log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=lambda: server.run(sockets=[s1, s2]),
        name="test-multi-socket-uvicorn",
        daemon=True,
    )
    thread.start()

    try:
        # Wait for the server (and the app lifespan) to finish startup.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not getattr(server, "started", False):
            time.sleep(0.1)
        assert getattr(server, "started", False), (
            "uvicorn did not finish startup within timeout"
        )

        r1 = httpx.get(f"http://127.0.0.1:{p1}/api/health", timeout=5)
        r2 = httpx.get(f"http://127.0.0.1:{p2}/api/health", timeout=5)
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json().get("status") == "ok"
        assert r2.json().get("status") == "ok"
    finally:
        # Mirror the tray's graceful-then-force shutdown (tray.py:398-411).
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        for sock in (s1, s2):
            try:
                sock.close()
            except OSError:
                pass
