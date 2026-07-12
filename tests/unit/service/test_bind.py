"""Tests for jacked.service.bind — bind resolution + socket construction.

Covers the precedence matrix (explicit CLI > DB settings > loopback default),
the Tailscale detection branches (UDP route trick + `tailscale ip` fallback),
the mutually-exclusive socket sets, and create_sockets' pre-bind failure
cleanup. Detection and DB are mocked so nothing here needs a real tailnet or
the user's real settings DB; the autouse fixture in conftest.py isolates
~/.claude regardless.
"""

import logging
import socket
import sqlite3
import subprocess
from unittest import mock

import pytest

from jacked.service.bind import (
    BindPlan,
    create_sockets,
    detect_tailscale_ip,
    resolve_bind,
)
from jacked.web.database import Database


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_db():
    """A throwaway in-memory settings DB."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def socket_cleanup():
    """Collect sockets created during a test and close them at teardown."""
    created = []
    yield created
    for s in created:
        try:
            s.close()
        except OSError:
            pass


def _mock_udp_socket(local_ip="192.168.1.50", raise_oserror=False):
    """Build a mock stdlib socket for the UDP route-trick detection path."""
    m = mock.MagicMock()
    if raise_oserror:
        m.connect.side_effect = OSError("network is unreachable")
    m.getsockname.return_value = (local_ip, 41234)
    return m


class _BrokenDB:
    """A settings DB whose reads always blow up (corrupt/locked file)."""

    def get_setting(self, key):
        raise sqlite3.Error("database disk image is malformed")


# ---------------------------------------------------------------------------
# Precedence matrix
# ---------------------------------------------------------------------------


def test_cli_host_beats_db_enabled_all():
    """Explicit --host wins over a DB that says enabled+all, with no DB read."""
    db = mock.Mock()
    plan = resolve_bind("192.168.1.5", 8321, db)
    assert plan.mode == "cli"
    assert plan.addresses == ("192.168.1.5",)
    assert plan.primary_host == "192.168.1.5"
    # One-shot override never touches the DB or detection.
    db.get_setting.assert_not_called()


def test_cli_host_all_interfaces_single_address():
    """cli_host '0.0.0.0' preserves today's behavior: a single-socket plan."""
    plan = resolve_bind("0.0.0.0", 8321, db=None)
    assert plan.mode == "cli"
    assert plan.addresses == ("0.0.0.0",)
    assert plan.primary_host == "0.0.0.0"
    assert plan.tailscale_ip is None
    assert plan.fallback_reason is None


def test_db_used_when_no_cli_host(mem_db):
    """With no --host, the DB settings drive the plan."""
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "all")
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "all"


def test_loopback_when_neither(mem_db):
    """No --host and an empty DB → loopback default."""
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "loopback"
    assert plan.addresses == ("127.0.0.1",)
    assert plan.primary_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# DB states
# ---------------------------------------------------------------------------


def test_absent_keys_loopback(mem_db):
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "loopback"
    assert plan.addresses == ("127.0.0.1",)


def test_enabled_false_loopback(mem_db):
    mem_db.set_setting("remote_access_enabled", "false")
    mem_db.set_setting("remote_access_scope", "all")
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "loopback"
    assert plan.addresses == ("127.0.0.1",)


def test_enabled_garbage_loopback(mem_db):
    """A non-'true' enabled value (typo, garbage) fails safe to loopback."""
    mem_db.set_setting("remote_access_enabled", "yes")
    mem_db.set_setting("remote_access_scope", "all")
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "loopback"


def test_scope_all(mem_db):
    """enabled+all → 0.0.0.0 alone (never a second socket on the same port)."""
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "all")
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "all"
    assert plan.addresses == ("0.0.0.0",)
    assert plan.primary_host == "0.0.0.0"
    assert plan.tailscale_ip is None


def test_scope_tailscale_found(mem_db):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "tailscale")
    with mock.patch(
        "jacked.service.bind.detect_tailscale_ip", return_value="100.64.12.7"
    ):
        plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "tailscale"
    assert plan.addresses == ("127.0.0.1", "100.64.12.7")
    assert plan.primary_host == "100.64.12.7"
    assert plan.tailscale_ip == "100.64.12.7"
    assert plan.fallback_reason is None


def test_scope_tailscale_not_found_fallback(mem_db, caplog):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "tailscale")
    with mock.patch("jacked.service.bind.detect_tailscale_ip", return_value=None):
        with caplog.at_level(logging.WARNING, logger="jacked.service.bind"):
            plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "tailscale"
    # Fallback binds loopback ONLY — never widen to 0.0.0.0.
    assert plan.addresses == ("127.0.0.1",)
    assert plan.primary_host == "127.0.0.1"
    assert plan.tailscale_ip is None
    assert plan.fallback_reason  # human-readable, non-empty
    assert "0.0.0.0" not in plan.addresses
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_missing_scope_defaults_to_tailscale(mem_db, caplog):
    """enabled+no scope → tailscale path, with a warning about the missing value."""
    mem_db.set_setting("remote_access_enabled", "true")
    with mock.patch(
        "jacked.service.bind.detect_tailscale_ip", return_value="100.64.1.1"
    ):
        with caplog.at_level(logging.WARNING, logger="jacked.service.bind"):
            plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "tailscale"
    assert plan.addresses == ("127.0.0.1", "100.64.1.1")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_garbage_scope_defaults_to_tailscale_and_warns(mem_db, caplog):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "banana")
    with mock.patch(
        "jacked.service.bind.detect_tailscale_ip", return_value="100.64.2.2"
    ):
        with caplog.at_level(logging.WARNING, logger="jacked.service.bind"):
            plan = resolve_bind(None, 8321, mem_db)
    assert plan.mode == "tailscale"
    assert plan.addresses == ("127.0.0.1", "100.64.2.2")
    # The warning should name the offending value so a log reader can see it.
    assert any("banana" in r.getMessage() for r in caplog.records)


def test_db_read_error_falls_back_to_loopback(caplog):
    """A sqlite error while reading settings must not crash the boot."""
    with caplog.at_level(logging.WARNING, logger="jacked.service.bind"):
        plan = resolve_bind(None, 8321, _BrokenDB())
    assert plan.mode == "loopback"
    assert plan.addresses == ("127.0.0.1",)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_default_db_constructed_lazily_when_none(mem_db):
    """db=None constructs a Database lazily; patch it so we don't hit ~/.claude."""
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "all")
    with mock.patch("jacked.web.database.Database", return_value=mem_db) as ctor:
        plan = resolve_bind(None, 8321, db=None)
    ctor.assert_called_once()
    assert plan.mode == "all"


# ---------------------------------------------------------------------------
# Socket-set exclusivity
# ---------------------------------------------------------------------------


def test_socket_set_all_is_exclusive(mem_db):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "all")
    plan = resolve_bind(None, 8321, mem_db)
    assert plan.addresses == ("0.0.0.0",)
    assert len(plan.addresses) == 1


def test_socket_set_tailscale_found_pairs_loopback_and_ip(mem_db):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "tailscale")
    with mock.patch(
        "jacked.service.bind.detect_tailscale_ip", return_value="100.64.99.9"
    ):
        plan = resolve_bind(None, 8321, mem_db)
    assert plan.addresses == ("127.0.0.1", "100.64.99.9")


def test_socket_set_fallback_is_loopback_only(mem_db):
    mem_db.set_setting("remote_access_enabled", "true")
    mem_db.set_setting("remote_access_scope", "tailscale")
    with mock.patch("jacked.service.bind.detect_tailscale_ip", return_value=None):
        plan = resolve_bind(None, 8321, mem_db)
    assert plan.addresses == ("127.0.0.1",)


# ---------------------------------------------------------------------------
# BindPlan.as_effective
# ---------------------------------------------------------------------------


def test_as_effective_shape():
    plan = BindPlan(
        mode="tailscale",
        addresses=("127.0.0.1", "100.64.5.5"),
        port=8321,
        primary_host="100.64.5.5",
        tailscale_ip="100.64.5.5",
        fallback_reason=None,
    )
    eff = plan.as_effective()
    assert eff == {
        "mode": "tailscale",
        "addresses": ["127.0.0.1", "100.64.5.5"],
        "tailscale_ip": "100.64.5.5",
        "fallback_reason": None,
    }
    # addresses must be a plain list (JSON-serializable), not a tuple.
    assert isinstance(eff["addresses"], list)


def test_bindplan_is_frozen():
    plan = BindPlan(
        mode="loopback", addresses=("127.0.0.1",), port=8321, primary_host="127.0.0.1"
    )
    with pytest.raises(Exception):
        plan.mode = "all"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BindPlan.probe_host
# ---------------------------------------------------------------------------


def test_probe_host_loopback_is_loopback():
    plan = BindPlan(
        mode="loopback", addresses=("127.0.0.1",), port=8321, primary_host="127.0.0.1"
    )
    assert plan.probe_host == "127.0.0.1"


def test_probe_host_tailscale_is_loopback():
    """tailscale primary_host is the 100.x IP, but loopback is always bound too
    and is the reliable local probe target."""
    plan = BindPlan(
        mode="tailscale",
        addresses=("127.0.0.1", "100.64.5.5"),
        port=8321,
        primary_host="100.64.5.5",
        tailscale_ip="100.64.5.5",
    )
    assert plan.probe_host == "127.0.0.1"


def test_probe_host_all_is_loopback():
    """all binds 0.0.0.0 (not a connect target); loopback covers it."""
    plan = BindPlan(
        mode="all", addresses=("0.0.0.0",), port=8321, primary_host="0.0.0.0"
    )
    assert plan.probe_host == "127.0.0.1"


def test_probe_host_cli_is_primary_host():
    """A cli plan binds ONLY the caller's address, so it must be probed there
    (this preserves today's --host verbatim behavior)."""
    plan = BindPlan(
        mode="cli", addresses=("192.168.1.5",), port=8321, primary_host="192.168.1.5"
    )
    assert plan.probe_host == "192.168.1.5"


# ---------------------------------------------------------------------------
# Tailscale detection
# ---------------------------------------------------------------------------


def test_detect_udp_in_range_short_circuits_cli():
    sock = _mock_udp_socket(local_ip="100.64.12.7")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run") as run:
            assert detect_tailscale_ip() == "100.64.12.7"
    run.assert_not_called()
    sock.close.assert_called_once()


def test_detect_udp_out_of_range_falls_to_cli():
    """A LAN IP from the default route is a miss; fall through to the CLI."""
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    cli = mock.Mock(returncode=0, stdout="100.64.9.9\n")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() == "100.64.9.9"


def test_detect_udp_oserror_falls_to_cli():
    sock = _mock_udp_socket(raise_oserror=True)
    cli = mock.Mock(returncode=0, stdout="100.64.5.5\n")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() == "100.64.5.5"
    # Socket still closed even when connect() raised.
    sock.close.assert_called_once()


def test_detect_cli_success():
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    cli = mock.Mock(returncode=0, stdout="100.64.7.7\n")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() == "100.64.7.7"


def test_detect_cli_file_not_found():
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch(
            "jacked.service.bind.subprocess.run", side_effect=FileNotFoundError
        ):
            assert detect_tailscale_ip() is None


def test_detect_cli_timeout():
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    err = subprocess.TimeoutExpired(cmd="tailscale", timeout=3)
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", side_effect=err):
            assert detect_tailscale_ip() is None


def test_detect_cli_nonzero_exit():
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    cli = mock.Mock(returncode=1, stdout="")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() is None


def test_detect_cli_garbage_stdout():
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    cli = mock.Mock(returncode=0, stdout="not-an-ip-address\n")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() is None


def test_detect_cli_out_of_range_stdout():
    """CLI returns a real IP, but outside CGNAT — reject it."""
    sock = _mock_udp_socket(local_ip="192.168.1.50")
    cli = mock.Mock(returncode=0, stdout="10.0.0.5\n")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch("jacked.service.bind.subprocess.run", return_value=cli):
            assert detect_tailscale_ip() is None


def test_detect_rejects_cgnat_boundary():
    """100.63.x is just below the 100.64.0.0/10 range and must be rejected."""
    sock = _mock_udp_socket(local_ip="100.63.255.255")
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch(
            "jacked.service.bind.subprocess.run", side_effect=FileNotFoundError
        ):
            assert detect_tailscale_ip() is None


def test_detect_both_fail_returns_none():
    sock = _mock_udp_socket(raise_oserror=True)
    with mock.patch("jacked.service.bind.socket.socket", return_value=sock):
        with mock.patch(
            "jacked.service.bind.subprocess.run", side_effect=FileNotFoundError
        ):
            assert detect_tailscale_ip() is None


# ---------------------------------------------------------------------------
# create_sockets
# ---------------------------------------------------------------------------


def test_create_sockets_two_loopback_ephemeral(socket_cleanup):
    """Two loopback sockets on port 0 (ephemeral) bind cleanly."""
    plan = BindPlan(
        mode="loopback",
        addresses=("127.0.0.1", "127.0.0.1"),
        port=0,
        primary_host="127.0.0.1",
    )
    socks = create_sockets(plan)
    socket_cleanup.extend(socks)
    assert len(socks) == 2
    for s in socks:
        assert s.fileno() != -1
        assert s.family == socket.AF_INET
        assert s.type == socket.SOCK_STREAM


def test_create_sockets_single_loopback(socket_cleanup):
    plan = BindPlan(
        mode="loopback", addresses=("127.0.0.1",), port=0, primary_host="127.0.0.1"
    )
    socks = create_sockets(plan)
    socket_cleanup.extend(socks)
    assert len(socks) == 1
    assert socks[0].fileno() != -1


def test_create_sockets_conflict_names_address_and_closes_earlier(socket_cleanup):
    """A partway bind failure names the failing address:port and closes the
    sockets already created (they can't be leaked on a restart)."""
    # A free port for the first (loopback) socket to grab.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    # Second address is TEST-NET-1 (RFC 5737), never assigned to any local
    # interface, so binding it always fails with EADDRNOTAVAIL — deterministic
    # and cross-platform, unlike relying on a port-conflict race.
    plan = BindPlan(
        mode="tailscale",
        addresses=("127.0.0.1", "192.0.2.1"),
        port=port,
        primary_host="127.0.0.1",
    )

    created = []
    real_socket = socket.socket

    def _spy(*args, **kwargs):
        s = real_socket(*args, **kwargs)
        created.append(s)
        return s

    with mock.patch("jacked.service.bind.socket.socket", side_effect=_spy):
        with pytest.raises(OSError) as exc_info:
            create_sockets(plan)
    socket_cleanup.extend(created)

    msg = str(exc_info.value)
    assert "192.0.2.1" in msg
    assert str(port) in msg
    # Every socket create_sockets opened must be closed after the failure.
    assert created
    for s in created:
        assert s.fileno() == -1


def test_create_sockets_conflict_on_occupied_port(socket_cleanup):
    """Bind a port first, then create_sockets on the SAME port → OSError that
    names the conflicting address."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    socket_cleanup.append(blocker)
    port = blocker.getsockname()[1]

    plan = BindPlan(
        mode="loopback", addresses=("127.0.0.1",), port=port, primary_host="127.0.0.1"
    )
    with pytest.raises(OSError) as exc_info:
        create_sockets(plan)
    msg = str(exc_info.value)
    assert "127.0.0.1" in msg
    assert str(port) in msg
