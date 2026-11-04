"""POST /api/upgrade follows the remote-access policy and the switch lease (#143i).

The upgrade installs a new build on the host and replaces the service
process, so it is gated exactly like a credential switch: loopback always
may; a remote client may only while remote access is on and its address is
inside the enabled scope. A refused remote call must never take a lock, so it
cannot wedge a later local upgrade behind a 409. An upgrade also refuses
while a credential switch holds the switch lease, because an exec in the
middle of a switch leaves its journal row pending and its stores half
written.

Harness: a bare FastAPI app with only the system router, a file-backed
Database on app.state (TestClient serves handlers on a worker thread, and a
``:memory:`` DB is per connection) and a forged client address. The real
``_do_upgrade`` is replaced by a recorder, so nothing installs or restarts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import jacked.findbin
from jacked.api.remote_access import REASON_OUTSIDE_SCOPE, REASON_REMOTE_ACCESS_OFF
from jacked.api.routes import system
from jacked.credentials.lease import FileSwitchLease
from jacked.credentials.runtime import SWITCH_LEASE_RELATIVE_PATH, switch_lease_for
from jacked.web.database import Database

LOOPBACK = "127.0.0.1"
TAILNET_IP = "100.64.12.7"
LAN_IP = "192.168.1.20"
GATE_LOGGER = "jacked.api.mutation_gate"
SWITCH_IN_PROGRESS = "Account switch in progress. Try again when it finishes."
LEASE_UNAVAILABLE = (
    "The credential switch lease is not available. Check the service log on "
    "the host machine."
)


class _UpgradeRecorder:
    """Stands in for ``system._do_upgrade``.

    ``calls`` records each time the route schedules an upgrade (inside the
    request, before the response). ``ran`` is set when the scheduled task
    actually starts on the client's event loop.
    """

    def __init__(self) -> None:
        self.calls: list = []
        self.hosts: list = []
        self.ran = threading.Event()

    def __call__(self, ws_registry, client_host=None):
        self.calls.append(ws_registry)
        self.hosts.append(client_host)
        return self._run()

    async def _run(self) -> None:
        self.ran.set()


class _HeldLock:
    """An upgrade lock that an earlier request holds."""

    def locked(self) -> bool:
        return True


class _Registry:
    """Records what ``_do_upgrade`` broadcasts to the dashboard."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


@pytest.fixture(autouse=True)
def _fresh_upgrade_lock():
    """Each test gets an upgrade lock that no earlier event loop touched."""
    system.reset_locks()
    yield
    system.reset_locks()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "jacked.db"))


@pytest.fixture
def app(db):
    application = FastAPI()
    application.include_router(system.router, prefix="/api")
    application.state.db = db
    application.state.ws_registry = None
    return application


@pytest.fixture
def upgrade(monkeypatch):
    recorder = _UpgradeRecorder()
    monkeypatch.setattr(system, "_do_upgrade", recorder)
    return recorder


def _set_remote_access(db, enabled: bool, scope: str) -> None:
    db.set_setting("remote_access_enabled", "true" if enabled else "false")
    db.set_setting("remote_access_scope", scope)


def _client(app, host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


def _gate_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == GATE_LOGGER]


def _gate_info_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in _gate_records(caplog)
        if record.levelno == logging.INFO
    ]


def _denied_body(reason: str) -> dict:
    return {
        "error": {
            "message": system._UPGRADE_DENIED_MESSAGES[reason],
            "code": reason.upper(),
        }
    }


def _assert_lease_is_free() -> None:
    with switch_lease_for(Path.home()).acquire() as held:
        assert held is True, "the credential switch lease was left held"


# ---------------------------------------------------------------------------
# The remote-access gate
# ---------------------------------------------------------------------------


def test_loopback_upgrade_starts_while_remote_access_is_off(app, db, upgrade, caplog):
    _set_remote_access(db, False, "tailscale")

    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        with _client(app, LOOPBACK) as client:
            resp = client.post("/api/upgrade")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "started"}
            assert upgrade.calls == [None]
            assert upgrade.ran.wait(timeout=5), "the scheduled upgrade never ran"

    # Someone at the machine acts all day: the gate stays silent for loopback.
    assert _gate_records(caplog) == []
    # The route only probes the switch lease; it never keeps it.
    _assert_lease_is_free()


def test_tailnet_upgrade_is_refused_while_remote_access_is_off(app, db, upgrade, caplog):
    _set_remote_access(db, False, "tailscale")

    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        with _client(app, TAILNET_IP) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == _denied_body(REASON_REMOTE_ACCESS_OFF)
    error = resp.json()["error"]
    assert error["code"] == "REMOTE_ACCESS_OFF"
    assert "Settings > Advanced" in error["message"]
    assert "host machine" in error["message"]
    assert "—" not in error["message"]
    assert upgrade.calls == []
    assert _gate_info_messages(caplog) == [
        "Remote credential mutation DENIED (upgrade) for client 100.64.12.7: "
        "remote_access_off (enabled=False, scope=tailscale)"
    ]


def test_tailnet_upgrade_starts_when_remote_access_is_on(app, db, upgrade, caplog):
    _set_remote_access(db, True, "tailscale")

    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        with _client(app, TAILNET_IP) as client:
            resp = client.post("/api/upgrade")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "started"}
            assert upgrade.ran.wait(timeout=5), "the scheduled upgrade never ran"

    assert upgrade.calls == [None]
    assert _gate_info_messages(caplog) == [
        "Remote credential mutation ALLOWED (upgrade) for client 100.64.12.7 "
        "(remote access on, scope=tailscale)"
    ]


def test_lan_upgrade_outside_the_tailscale_scope_is_refused(app, db, upgrade, caplog):
    _set_remote_access(db, True, "tailscale")

    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        with _client(app, LAN_IP) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == _denied_body(REASON_OUTSIDE_SCOPE)
    assert resp.json()["error"]["code"] == "OUTSIDE_SCOPE"
    assert upgrade.calls == []
    assert _gate_info_messages(caplog) == [
        "Remote credential mutation DENIED (upgrade) for client 192.168.1.20: "
        "outside_scope (enabled=True, scope=tailscale)"
    ]


def test_lan_upgrade_starts_when_the_scope_is_all(app, db, upgrade, caplog):
    _set_remote_access(db, True, "all")

    with caplog.at_level(logging.INFO, logger=GATE_LOGGER):
        with _client(app, LAN_IP) as client:
            resp = client.post("/api/upgrade")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "started"}
            assert upgrade.ran.wait(timeout=5), "the scheduled upgrade never ran"

    assert upgrade.calls == [None]
    assert _gate_info_messages(caplog) == [
        "Remote credential mutation ALLOWED (upgrade) for client 192.168.1.20 "
        "(remote access on, scope=all)"
    ]


def test_refused_remote_upgrade_never_wedges_a_later_local_one(app, db, upgrade):
    _set_remote_access(db, False, "tailscale")

    with _client(app, TAILNET_IP) as remote:
        assert remote.post("/api/upgrade").status_code == 403
    # The refusal took neither lock.
    assert system._upgrade_lock.locked() is False
    _assert_lease_is_free()

    with _client(app, LOOPBACK) as local:
        resp = local.post("/api/upgrade")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "started"}
        assert upgrade.ran.wait(timeout=5), "the scheduled upgrade never ran"

    assert upgrade.calls == [None]


def test_the_gate_answers_before_any_lock_is_consulted(app, db, upgrade, monkeypatch):
    """A refused client learns nothing about a running upgrade or switch."""
    _set_remote_access(db, False, "tailscale")
    monkeypatch.setattr(system, "_upgrade_lock", _HeldLock())

    with switch_lease_for(Path.home()).acquire() as held:
        assert held is True
        with _client(app, TAILNET_IP) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 403
    assert resp.json() == _denied_body(REASON_REMOTE_ACCESS_OFF)
    assert upgrade.calls == []


# ---------------------------------------------------------------------------
# The credential switch lease and the upgrade lock
# ---------------------------------------------------------------------------


def test_local_upgrade_is_refused_while_a_switch_holds_the_lease(app, db, upgrade):
    _set_remote_access(db, False, "tailscale")
    lease = switch_lease_for(Path.home())

    with lease.acquire() as held:
        assert held is True
        with _client(app, LOOPBACK) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 409
    assert resp.json() == {"detail": SWITCH_IN_PROGRESS}
    assert resp.headers["cache-control"] == "no-store"
    assert upgrade.calls == []


def test_local_upgrade_is_refused_while_another_process_holds_the_lease(
    app, db, upgrade
):
    """``jacked launch`` switches from its own process: only the file lock is held."""
    other_process = FileSwitchLease(
        Path.home() / ".claude" / Path(*SWITCH_LEASE_RELATIVE_PATH)
    )

    with other_process.acquire() as held:
        assert held is True
        with _client(app, LOOPBACK) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 409
    assert resp.json() == {"detail": SWITCH_IN_PROGRESS}
    assert resp.headers["cache-control"] == "no-store"
    assert upgrade.calls == []


def test_local_upgrade_is_refused_while_another_upgrade_runs(
    app, db, upgrade, monkeypatch
):
    monkeypatch.setattr(system, "_upgrade_lock", _HeldLock())

    with _client(app, LOOPBACK) as client:
        resp = client.post("/api/upgrade")

    assert resp.status_code == 409
    assert resp.json() == {"detail": "Upgrade already in progress"}
    assert resp.headers["cache-control"] == "no-store"
    assert upgrade.calls == []


def test_do_upgrade_refuses_to_run_while_the_switch_lease_is_held(monkeypatch):
    looked_up: list[str] = []
    commands: list[list[str]] = []

    def _forbidden_find_bin(name, *args, **kwargs):
        looked_up.append(name)
        raise AssertionError(f"find_bin({name!r}) ran while a switch held the lease")

    async def _forbidden_command(cmd):
        commands.append(cmd)
        raise AssertionError("an upgrade command ran while a switch held the lease")

    # The module looks find_bin up by its own name; patch both bindings.
    monkeypatch.setattr(jacked.findbin, "find_bin", _forbidden_find_bin)
    monkeypatch.setattr(system, "find_bin", _forbidden_find_bin)
    monkeypatch.setattr(system, "_upgrade_run_cmd", _forbidden_command)
    registry = _Registry()

    with switch_lease_for(Path.home()).acquire() as held:
        assert held is True
        asyncio.run(system._do_upgrade(registry))

    assert registry.events == [("upgrade_failed", {"error": SWITCH_IN_PROGRESS})]
    assert looked_up == []
    assert commands == []
    # The refusal released the upgrade lock, so a later upgrade can start.
    assert system._upgrade_lock.locked() is False


def _make_lease_unusable() -> Path:
    """A directory where the lock FILE belongs: os.open(O_RDWR) fails, so the
    lease cannot be opened at all and refuses fail-closed as 'unusable'."""
    path = Path.home() / ".claude" / Path(*SWITCH_LEASE_RELATIVE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    return path


def test_an_unusable_lease_file_is_a_503_not_a_switch_in_progress(
    app, db, upgrade, caplog
):
    """A lock file that cannot be opened proves nothing about switches, so the
    upgrade refuses fail-closed, but it must say so, not blame a switch."""
    _set_remote_access(db, False, "tailscale")
    _make_lease_unusable()

    with caplog.at_level(logging.WARNING, logger="jacked.api.routes.system"):
        with _client(app, LOOPBACK) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 503
    assert resp.json() == {"detail": LEASE_UNAVAILABLE}
    assert upgrade.calls == []
    assert [r.getMessage() for r in caplog.records if r.name == "jacked.api.routes.system"] == [
        "Upgrade refused for client 127.0.0.1: the credential switch lease is unusable"
    ]


def test_do_upgrade_reports_an_unusable_lease_as_unavailable(monkeypatch):
    monkeypatch.setattr(system, "find_bin", lambda *a, **k: pytest.fail("ran"))
    _make_lease_unusable()
    registry = _Registry()

    asyncio.run(system._do_upgrade(registry, "127.0.0.1"))

    assert registry.events == [("upgrade_failed", {"error": LEASE_UNAVAILABLE})]
    assert system._upgrade_lock.locked() is False


def test_the_route_hands_the_client_host_to_the_upgrade(app, db, upgrade):
    _set_remote_access(db, False, "tailscale")
    with _client(app, LOOPBACK) as client:
        assert client.post("/api/upgrade").status_code == 200
    assert upgrade.hosts == [LOOPBACK]


def test_do_upgrade_holds_the_switch_lease_for_its_whole_run(monkeypatch, caplog):
    """A switch cannot start between the first command and the exec.

    The install commands and the process replacement are stubbed; each stub
    probes the REAL file lease (as ``jacked launch`` in another process
    would) and records whether it was held. The exec stub raises, so the
    failure path releases everything too.
    """
    import jacked.service.restart as restart

    probes: list[tuple[str, bool]] = []
    lock_path = Path.home() / ".claude" / Path(*SWITCH_LEASE_RELATIVE_PATH)
    real_sleep = asyncio.sleep

    def _lease_is_held() -> bool:
        # Probe the FILE lock as another process would. The composite lease
        # would refuse at the in-process lock first and prove nothing about
        # the file lock.
        with FileSwitchLease(lock_path).acquire() as free:
            return not free

    def _fake_find_bin(name, *args, **kwargs):
        return f"/stub/bin/{name}"

    async def _stub_command(cmd):
        probes.append((cmd[0], _lease_is_held()))
        await real_sleep(0)  # a real scheduling yield while holding the lease
        probes.append((cmd[0] + ":after-await", _lease_is_held()))

    async def _no_sleep(_seconds):
        return None

    def _stub_execv(prog, argv):
        probes.append(("execv", _lease_is_held()))
        raise OSError("exec stubbed out in a unit test")

    monkeypatch.setattr(jacked.findbin, "find_bin", _fake_find_bin)
    monkeypatch.setattr(system, "find_bin", _fake_find_bin)
    monkeypatch.setattr(system, "_upgrade_run_cmd", _stub_command)
    monkeypatch.setattr(system.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(restart, "_exec_target", lambda: ("/stub/bin/jacked", ["jacked"]))
    monkeypatch.setattr(system.os, "execv", _stub_execv)
    registry = _Registry()

    with caplog.at_level(logging.INFO, logger="jacked.api.routes.system"):
        asyncio.run(system._do_upgrade(registry, "127.0.0.1"))

    messages = [r.getMessage() for r in caplog.records if r.name == "jacked.api.routes.system"]
    assert messages == [
        "Upgrade started (requested by client 127.0.0.1)",
        "Upgrade failed at stage restart: OSError",
    ]
    assert probes == [
        ("/stub/bin/uv", True),
        ("/stub/bin/uv:after-await", True),
        ("/stub/bin/jacked", True),
        ("/stub/bin/jacked:after-await", True),
        ("execv", True),
    ]
    assert [event for event, _ in registry.events] == [
        "upgrade_started",
        "upgrade_progress",
        "upgrade_progress",
        "upgrade_complete",
        "upgrade_failed",
    ]
    assert "exec stubbed out" in registry.events[-1][1]["error"]
    # Everything is released after the failure, so a later upgrade can start.
    _assert_lease_is_free()
    assert system._upgrade_lock.locked() is False


def test_a_second_request_during_an_upgrade_is_told_about_the_upgrade(
    app, db, upgrade, monkeypatch
):
    """The running upgrade holds the switch lease itself, so the upgrade lock
    is checked first: the dashboard keeps the running upgrade's modal instead
    of showing an account-switch failure."""
    _set_remote_access(db, False, "tailscale")
    monkeypatch.setattr(system, "_upgrade_lock", _HeldLock())

    with switch_lease_for(Path.home()).acquire() as held:
        assert held is True
        with _client(app, LOOPBACK) as client:
            resp = client.post("/api/upgrade")

    assert resp.status_code == 409
    assert resp.json() == {"detail": "Upgrade already in progress"}
    assert resp.headers["cache-control"] == "no-store"
    assert upgrade.calls == []


class _NoisyStdout:
    """A stdout pipe that a stopped child still has to be drained of."""

    def __init__(self) -> None:
        self.drained = asyncio.Event()
        self._chunks = [b"x" * 1024, b"y" * 1024]

    async def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        self.drained.set()
        return b""


class _FakeChild:
    """A child whose communicate() never returns until it is stopped, and
    which asyncio reports as finished only once its stdout pipe was drained
    (a noisy child that ignores SIGTERM keeps the pipe open until killed)."""

    def __init__(self, ignores_sigterm: bool = False) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.ignores_sigterm = ignores_sigterm
        self.stdout = _NoisyStdout()
        self._done = asyncio.Event()

    async def communicate(self):
        await self._done.wait()
        return b"", None

    def terminate(self):
        self.terminated = True
        if self.ignores_sigterm:
            return
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        await self.stdout.drained.wait()
        return self.returncode


def test_cancellation_stops_the_installer_before_the_lease_is_released(monkeypatch):
    """Shutdown cancels _do_upgrade while uv runs: the child is terminated and
    awaited inside the lease, so no switch can start beside a live installer."""
    child = _FakeChild()
    lease_held_when_child_stopped: list[bool] = []

    async def _fake_exec(*cmd, **kwargs):
        return child

    original_terminate = child.terminate

    def _probing_terminate():
        with switch_lease_for(Path.home()).acquire() as free:
            lease_held_when_child_stopped.append(not free)
        original_terminate()

    child.terminate = _probing_terminate
    monkeypatch.setattr(system.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(jacked.findbin, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(system, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    registry = _Registry()

    async def _run():
        task = asyncio.create_task(system._do_upgrade(registry, "127.0.0.1"))
        for _ in range(20):  # let the upgrade reach the child
            await asyncio.sleep(0)
            if registry.events and registry.events[-1][0] == "upgrade_progress":
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(_run(), timeout=10))

    assert child.terminated is True
    assert child.stdout.drained.is_set()
    assert lease_held_when_child_stopped == [True]
    _assert_lease_is_free()
    assert system._upgrade_lock.locked() is False


def test_a_noisy_child_that_ignores_sigterm_is_killed_and_drained(monkeypatch):
    """SIGTERM is ignored, the pipe stays busy: the child is killed after the
    grace period, its stdout drained to EOF, and the cancellation still
    propagates with the lease released afterwards."""
    child = _FakeChild(ignores_sigterm=True)

    async def _fake_exec(*cmd, **kwargs):
        return child

    real_wait_for = asyncio.wait_for

    async def _short_grace(awaitable, timeout):
        # Shorten only _stop_child's 5 s grace; delegate to the real wait_for
        # (captured before the patch, so this cannot recurse into itself).
        return await real_wait_for(awaitable, timeout=0.05 if timeout == 5 else timeout)

    monkeypatch.setattr(system.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(system, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(jacked.findbin, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    registry = _Registry()

    async def _run():
        monkeypatch.setattr(system.asyncio, "wait_for", _short_grace)
        try:
            task = asyncio.create_task(system._do_upgrade(registry, "127.0.0.1"))
            for _ in range(20):
                await real_sleep(0)
                if registry.events and registry.events[-1][0] == "upgrade_progress":
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            monkeypatch.setattr(system.asyncio, "wait_for", real_wait_for)

    real_sleep = asyncio.sleep
    asyncio.run(asyncio.wait_for(_run(), timeout=10))

    assert child.terminated is True and child.killed is True
    assert child.stdout.drained.is_set()
    _assert_lease_is_free()
    assert system._upgrade_lock.locked() is False


def test_the_module_keeps_a_strong_reference_to_every_scheduled_task(
    app, db, upgrade
):
    """asyncio holds tasks weakly; an unreferenced upgrade task can be
    collected. Two requests scheduled on one tick must both stay rooted
    until they finish, and the set must empty itself afterwards."""
    _set_remote_access(db, False, "tailscale")
    with _client(app, LOOPBACK) as client:
        assert client.post("/api/upgrade").status_code == 200
        assert upgrade.ran.wait(timeout=5)
    assert system._upgrade_tasks == set()


def test_repeated_denials_from_one_host_are_folded_into_one_line(
    app, db, upgrade, caplog, monkeypatch
):
    """A refused peer looping on POST must not evict the log ring buffer."""
    from jacked.api import mutation_gate

    _set_remote_access(db, False, "tailscale")
    clock = {"now": 1000.0}
    monkeypatch.setattr(mutation_gate.time, "monotonic", lambda: clock["now"])

    with caplog.at_level(logging.INFO, logger="jacked.api.mutation_gate"):
        with _client(app, TAILNET_IP) as client:
            for _ in range(3):
                assert client.post("/api/upgrade").status_code == 403
            clock["now"] += mutation_gate.DENIAL_LOG_INTERVAL_SECONDS + 1
            assert client.post("/api/upgrade").status_code == 403

    lines = [r.getMessage() for r in caplog.records if r.name == "jacked.api.mutation_gate"]
    assert len(lines) == 2
    assert lines[0].endswith("(enabled=False, scope=tailscale)")
    assert lines[1].endswith("(enabled=False, scope=tailscale) (2 more in the last 60 s)")
    assert upgrade.calls == []


def _denied_decision(db, audit: str, host: str = TAILNET_IP) -> None:
    from jacked.api import mutation_gate

    request = SimpleNamespace(
        client=SimpleNamespace(host=host),
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
    )
    allowed, _reason, _host = mutation_gate.mutation_decision(request, audit=audit)
    assert allowed is False


def _gate_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "jacked.api.mutation_gate"]


def test_a_denied_upgrade_is_logged_inside_the_window_of_a_denied_switch(db, caplog):
    """/cso M2 (Repudiation): the fold is per action. A denied switch from a
    peer must not hide that peer's denied upgrade 60 s later or sooner."""
    _set_remote_access(db, False, "tailscale")

    with caplog.at_level(logging.INFO, logger="jacked.api.mutation_gate"):
        _denied_decision(db, "switch")
        _denied_decision(db, "upgrade")

    lines = _gate_lines(caplog)
    assert len(lines) == 2
    assert lines[0].startswith("Remote credential mutation DENIED (switch)")
    assert lines[1].startswith("Remote credential mutation DENIED (upgrade)")
    assert TAILNET_IP in lines[1]


def test_an_ended_denial_window_is_pruned_and_its_folded_count_is_written(
    db, caplog, monkeypatch
):
    """A peer that stops being refused leaves no entry behind, and the count
    it folded is written by the next line instead of being lost."""
    from jacked.api import mutation_gate

    _set_remote_access(db, False, "tailscale")
    clock = {"now": 1000.0}
    monkeypatch.setattr(mutation_gate.time, "monotonic", lambda: clock["now"])

    with caplog.at_level(logging.INFO, logger="jacked.api.mutation_gate"):
        for _ in range(3):
            _denied_decision(db, "switch")
        _denied_decision(db, "upgrade", host="100.64.12.8")
        assert len(mutation_gate._denial_log_state) == 2
        clock["now"] += mutation_gate.DENIAL_LOG_INTERVAL_SECONDS + 1
        _denied_decision(db, "swap-pause", host="100.64.12.9")

    assert [key[0] for key in mutation_gate._denial_log_state] == ["swap-pause"]
    lines = _gate_lines(caplog)
    assert len(lines) == 4
    assert lines[2].startswith("Remote credential mutation DENIED (switch)")
    assert lines[2].endswith("(2 more after the last line)")
    assert lines[3].startswith("Remote credential mutation DENIED (swap-pause)")


def test_a_window_that_has_not_ended_keeps_its_entry_and_its_count(db, caplog, monkeypatch):
    from jacked.api import mutation_gate

    _set_remote_access(db, False, "tailscale")
    clock = {"now": 1000.0}
    monkeypatch.setattr(mutation_gate.time, "monotonic", lambda: clock["now"])

    with caplog.at_level(logging.INFO, logger="jacked.api.mutation_gate"):
        _denied_decision(db, "switch")
        _denied_decision(db, "switch")
        clock["now"] += 1
        _denied_decision(db, "upgrade")

    assert len(_gate_lines(caplog)) == 2
    assert len(mutation_gate._denial_log_state) == 2
    assert mutation_gate._denial_log_state[next(
        key for key in mutation_gate._denial_log_state if key[0] == "switch"
    )][1] == 1


def test_cancel_running_upgrade_stops_the_child_inside_the_lease(monkeypatch):
    """The lifespan shutdown cancels a running upgrade first: the child is
    terminated (and killed when it ignores SIGTERM), drained, and only then is
    the lease released."""
    child = _FakeChild(ignores_sigterm=True)

    async def _fake_exec(*cmd, **kwargs):
        return child

    real_wait_for = asyncio.wait_for

    async def _short_grace(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.05 if timeout == 5 else timeout)

    monkeypatch.setattr(system.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(system, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(jacked.findbin, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    registry = _Registry()
    real_sleep = asyncio.sleep

    async def _run():
        monkeypatch.setattr(system.asyncio, "wait_for", _short_grace)
        try:
            task = asyncio.create_task(system._do_upgrade(registry, "127.0.0.1"))
            system._upgrade_tasks.add(task)
            task.add_done_callback(system._upgrade_tasks.discard)
            for _ in range(20):
                await real_sleep(0)
                if registry.events and registry.events[-1][0] == "upgrade_progress":
                    break
            await system.cancel_running_upgrade()
        finally:
            monkeypatch.setattr(system.asyncio, "wait_for", real_wait_for)

    asyncio.run(real_wait_for(_run(), timeout=10))

    assert child.terminated is True and child.killed is True
    assert child.stdout.drained.is_set()
    assert system._upgrade_tasks == set()
    _assert_lease_is_free()
    assert system._upgrade_lock.locked() is False


def test_service_shutdown_cancels_a_running_upgrade_first(quiet_lifespan, monkeypatch):
    """The lifespan cancels the upgrade before it tears anything else down."""
    from fastapi.testclient import TestClient

    from jacked.api import main as api_main

    order: list[str] = []

    async def _recording_cancel():
        order.append("cancel_running_upgrade")

    monkeypatch.setattr(system, "cancel_running_upgrade", _recording_cancel)
    real_close = api_main._close_loop_db_connection

    def _recording_close(*args, **kwargs):
        order.append("close_db")
        return real_close(*args, **kwargs)

    monkeypatch.setattr(api_main, "_close_loop_db_connection", _recording_close)

    with TestClient(api_main.app):
        pass

    assert order[0] == "cancel_running_upgrade"
    assert "close_db" in order


def test_do_upgrade_drops_a_second_request_that_slipped_past_the_route(
    monkeypatch, caplog
):
    """Two requests can pass the route's lock check on one loop tick; the
    second task must not queue behind the first and run an unrequested
    upgrade once the first finishes."""
    monkeypatch.setattr(system, "_upgrade_lock", _HeldLock())
    monkeypatch.setattr(system, "find_bin", lambda *a, **k: pytest.fail("ran"))
    registry = _Registry()

    with caplog.at_level(logging.INFO, logger="jacked.api.routes.system"):
        asyncio.run(system._do_upgrade(registry, "127.0.0.1"))

    assert registry.events == []
    assert "Upgrade already in progress; a second request was dropped" in caplog.text


# ---------------------------------------------------------------------------
# User-facing copy
# ---------------------------------------------------------------------------


def test_upgrade_denial_copy_is_user_facing_clean():
    """Shown to a user: name where to fix it, and no em-dash or dash stand-in."""
    messages = system._UPGRADE_DENIED_MESSAGES

    assert set(messages) == {REASON_REMOTE_ACCESS_OFF, REASON_OUTSIDE_SCOPE}
    # The two denials have different fixes, so they read differently.
    assert len(set(messages.values())) == 2
    for message in messages.values():
        assert "—" not in message
        assert "–" not in message
        assert "--" not in message
        assert "Settings > Advanced" in message
        assert "host machine" in message


def test_pin_295i_the_dashboard_upgrade_never_installs_an_older_version(monkeypatch):
    """#295i: the Upgrade button asks the shared guard before it runs uv.
    When the index serves an older version than the one on disk, no command
    runs, the service is not replaced, and the page shows why."""
    from jacked import upgrade_guard

    refusal = (
        "Nothing to upgrade. The package index serves v0.111.1. "
        "The installed version is v0.111.2."
    )
    commands: list[list[str]] = []

    async def _forbidden_command(cmd):
        commands.append(cmd)
        raise AssertionError("an install command ran for a downgrade")

    def _forbidden_execv(prog, argv):
        raise AssertionError("the service was replaced for a downgrade")

    monkeypatch.setattr(upgrade_guard, "installed_version", lambda: "0.111.2")
    monkeypatch.setattr(
        upgrade_guard,
        "probe_index_versions",
        lambda timeout=5.0: upgrade_guard.IndexVersions("0.111.2", "0.111.1"),
    )
    monkeypatch.setattr(jacked.findbin, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(system, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(system, "_upgrade_run_cmd", _forbidden_command)
    monkeypatch.setattr(system.os, "execv", _forbidden_execv)
    registry = _Registry()

    asyncio.run(system._do_upgrade(registry, LOOPBACK))

    assert commands == []
    assert registry.events == [
        ("upgrade_started", {}),
        ("upgrade_failed", {"error": refusal}),
    ]
    _assert_lease_is_free()
    assert system._upgrade_lock.locked() is False
