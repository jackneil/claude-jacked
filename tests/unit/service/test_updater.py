"""Tests for the auto-updater."""

import contextlib
import http.client
import itertools
import json
import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest
from jacked.winproc import system_program



def _ok(stdout: str = "") -> MagicMock:
    """Successful subprocess result with real text streams.

    The preflight step concatenates `.stdout` and `.stderr`, so a bare
    MagicMock would put a repr into the update log.
    """
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# What a jacked service answers on /api/health: exactly this shape, and
# nothing else counts (service/legacy.py pins the same fingerprint).
JACKED_HEALTH = b'{"status": "ok", "db": true}'


class _FakeHttpResponse:
    """Stand-in for an HTTP response: a status, and a bounded read()."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, amount: "int | None" = None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _probe_opener(version=None, health=JACKED_HEALTH, status: int = 200):
    """build_opener() stand-in: one fake listener answering both local routes.

    `version` is None (the route never responds), a version string, or a
    callable returning either per call - the build on the port changes across
    a run that rolls back. `health` None means /api/health never responds.
    A MagicMock, so a test can assert the listener was actually asked.
    """
    def answer(url, *_a, **_k):
        if "/api/health" in str(url):
            if health is None:
                raise OSError("connection refused")
            return _FakeHttpResponse(health, status)
        current = version() if callable(version) else version
        if isinstance(current, BaseException):
            raise current
        if current is None:
            raise OSError("connection refused")
        return _FakeHttpResponse(json.dumps({"current": current}).encode("utf-8"))

    opener = MagicMock()
    opener.open.side_effect = answer
    return opener


@contextlib.contextmanager
def _local_probes(version=None, health=JACKED_HEALTH, status: int = 200):
    """Patch the opener both local probes build, and hand it to the test."""
    opener = _probe_opener(version, health, status)
    with patch("urllib.request.build_opener", return_value=opener):
        yield opener


def _free_at_each_tail_start():
    """Listening oracle: free for the first probe of each restart tail.

    The subprocess count only changes between tails, so the first probe after
    it moves is that tail's waiting_port_free (free), and every probe until it
    moves again is that tail's verify (a service answering).
    """
    state = {"seen": None}

    def oracle(run):
        if state["seen"] != run.call_count:
            state["seen"] = run.call_count
            return False
        return True

    return oracle


def _comes_up_after(free_polls: int = 1):
    """Listening oracle: free for the first `free_polls` probes, up after.

    That is the ordinary shape of a run: waiting_port_free finds the port
    free, then the service the restart started answers for verifying_service.
    """
    state = {"n": 0}

    def oracle(*_a, **_k):
        state["n"] += 1
        return state["n"] > free_polls

    return oracle


def _script_port_mock(mock, results, run_mock) -> None:
    """Point a port-probe mock at a bool, a finite list, or a run-count oracle."""
    if isinstance(results, bool):
        # The verify loop spins with sleep patched out, so a finite
        # side_effect list would run dry mid-poll.
        mock.return_value = results
    elif callable(results):
        # An oracle reading the subprocess mock, for scripts where the port
        # answer must change once the rollback has run.
        mock.side_effect = lambda *_a, **_k: results(run_mock)
    else:
        mock.side_effect = list(results)


@pytest.fixture(autouse=True)
def _stub_native_restart(monkeypatch):
    """Keep the real native lifecycle adapter out of every test in this file.

    The final guard of run_update calls native_restart when a run ends
    early. Without this stub, that ran the real adapter
    (provision_service_contract, then restart_native_owned) against the
    temporary HOME. Only the missing supervisor artifact and the conftest
    launchctl tripwire stopped it. A test that asserts on native_restart
    patches its own mock, and that patch replaces this stub for the test.

    The stub goes through the test's own ``monkeypatch``. A ``patch()``
    context here was undone BEFORE ``monkeypatch`` (conftest fixtures set
    ``monkeypatch`` up first), so a test that set native_restart with
    ``monkeypatch.setattr`` put this stub back for every later test file
    (test_native_restart.py then read "stubbed native_restart").
    """
    stub = MagicMock(return_value=(True, "stubbed native_restart"))
    monkeypatch.setattr("jacked.service.platform.native_restart", stub)
    return stub


def test_no_test_in_this_file_runs_the_real_native_restart():
    """The autouse stub is in force for every test in this file."""
    from jacked.service import platform

    assert platform.native_restart() == (True, "stubbed native_restart")


class TestWaitForExit:
    def test_returns_true_when_process_exits(self):
        from jacked.service.updater import wait_for_exit
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert wait_for_exit(p.pid, timeout=2.0) is True

    def test_returns_false_on_timeout(self):
        from jacked.service.updater import wait_for_exit
        assert wait_for_exit(os.getpid(), timeout=0.3) is False


class TestRunUpdate:
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test: manual spawn"))
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_order_wait_install_migrate_restart(
        self, mock_popen, mock_run, mock_find, mock_listening,
        mock_method, mock_gate, mock_ensure,
    ):
        """Verify: wait_for_exit -> uv install -> preflight -> jacked install
        -> jacked service start.

        Forces ensure_native_lifecycle to 'unavailable' so the updater falls
        through to the manual Popen(jacked service start) path exercised here."""
        from jacked.service import updater

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()
        # One connect probe drives both phases: free while waiting for the
        # port, answering once the new service is up.
        mock_listening.side_effect = _comes_up_after(1)

        with (
            patch.object(updater, "wait_for_exit", return_value=True) as mock_wait,
            _local_probes(version="0.100.0"),
        ):
            updater.run_update(parent_pid=12345, extras="tray")

        assert mock_wait.called
        # uv install -> service preflight -> jacked install --force --no-packs
        # -> (service up) -> jacked packs update.
        assert mock_run.call_count == 4
        assert mock_run.call_args_list[3][0][0] == ["/fake/jacked", "packs", "update"]
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "tool" in uv_args and "install" in uv_args
        # #295i: the requirement carries the installed version as a floor.
        assert any(arg.startswith("claude-jacked[tray]>=") for arg in uv_args)
        assert "--force" in uv_args

        assert mock_run.call_args_list[1][0][0] == [
            "/fake/jacked", "service", "preflight"
        ]

        jacked_install_args = mock_run.call_args_list[2][0][0]
        assert "/fake/jacked" in jacked_install_args
        assert "install" in jacked_install_args
        assert "--force" in jacked_install_args

        assert mock_popen.call_count == 1
        restart_args = mock_popen.call_args_list[0][0][0]
        assert "/fake/jacked" in restart_args
        assert "service" in restart_args and "start" in restart_args
        # Regression pin: the updater's detached spawn must stay host-free so
        # the restarted service resolves its bind from the settings DB (the
        # GUI Remote access toggle survives upgrades).
        assert "--host" not in restart_args

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="pipx")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_pipx_install_runs_the_resolved_pipx_path(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate,
    ):
        """The service PATH is system-only, so a bare "pipx" argv fails with
        FileNotFoundError when pipx lives in ~/.local/bin. The updater must
        run the binary find_bin resolved, exactly as it does for uv."""
        from jacked.service import updater
        mock_find.side_effect = lambda name: {
            "pipx": "/fake/home/.local/bin/pipx", "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=1)  # stop after install

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        install_argv = mock_run.call_args_list[0][0][0]
        assert install_argv[0] == "/fake/home/.local/bin/pipx"
        assert "install" in install_argv
        assert any(arg.startswith("claude-jacked[tray]>=") for arg in install_argv)

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="pipx")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_pipx_missing_fails_loud_without_running_install(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        for call in mock_run.call_args_list:
            assert "pipx" not in call[0][0][0]
        content = (tmp_path / "recovery.txt").read_text()
        assert "Could not find `pipx`" in content

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_skips_restart_if_install_fails(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_not_called()

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_recovery_file_on_install_failure(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate, tmp_path, monkeypatch,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        assert (tmp_path / "recovery.txt").exists()
        content = (tmp_path / "recovery.txt").read_text()
        assert "uv tool install" in content

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_finally_guard_kicks_service_when_upgrade_phase_bails_early(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate, tmp_path, monkeypatch,
    ):
        """When the upgrade phase exits early, the finally-guard MUST attempt
        native_restart so the tray comes back.
        Regression test for the SameFileError-leaves-tray-dead bug (v0.45.0).

        The early bail used here is a `jacked` binary that vanished after the
        install: that path returns before any restart is attempted. (A failed
        settings migration no longer bails - it rolls back and restarts the
        previous build, which is covered by TestJackedInstallFailure.)
        """
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv"}.get(name)
        # uv tool install succeeds; the jacked binary is then unfindable.
        mock_run.side_effect = [_ok()]

        # Track whether native_restart was called from the finally-guard.
        with patch("jacked.service.platform.native_restart", return_value=(True, "test-kickstart")) as mock_restart, \
             patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        # Neither the success-path restart nor the Popen fallback ran: the run
        # bailed first. The finally-guard SHOULD have called native_restart.
        mock_restart.assert_called_once_with()
        mock_popen.assert_not_called()
        # The log records the guard firing, and its result (#140i keeps this
        # early-exit kickstart exactly as it was).
        log = (tmp_path / "update.log").read_text()
        assert "Final guard: no restart attempted by upgrade flow. Kickstart." in log
        assert "Final guard: native_restart OK (test-kickstart)" in log
        assert "Final guard: skipped" not in log

    # Removed in 0.41.19: pip installs are refused by the gate, not auto-upgraded.


class TestWindowsTrayUpdaterBatch:
    @patch("sys.platform", "win32")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_tray_update_wait_loop_is_bounded(
        self, mock_popen, mock_find, mock_method, mock_gate, tmp_path, monkeypatch,
    ):
        """Tray-update helper must bound its parent-wait loop too — same
        PID-reuse infinite-spin bug as the `jacked upgrade` helper."""
        from jacked.service.updater import _spawn_windows_tray_updater

        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        _spawn_windows_tray_updater(parent_pid=4242, extras="tray", port=8321)

        assert len(created) == 1
        batch = open(created[0]).read()
        assert "JACKED_WAITED" in batch
        assert ":waitdone" in batch
        assert "GEQ 120" in batch
        assert "4242" in batch  # waits on the correct parent PID
        # Old unbounded form gone. Match the exact jump the spin loop used —
        # a bare "if not errorlevel 1" also matches the verify-retry branch,
        # which is a different (and deliberately bounded) construct. No \r\n in
        # the needle: this file was opened in universal-newline mode.
        assert "if not errorlevel 1 goto wait" not in batch
        mock_popen.assert_called_once()


class TestPortWaitBeforeServiceStart:
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test: force manual spawn"))
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.time.sleep", lambda _s: None)
    @patch("jacked.service.updater.find_bin")
    @patch("jacked.service.updater.is_port_listening")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_polls_port_before_spawning_service(
        self, mock_popen, mock_run, mock_listening, mock_find,
        mock_method, mock_gate, mock_ensure,
    ):
        """After uv+jacked install, must wait for port 8321 before `service start`."""
        from jacked.service import updater

        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()

        # Port-wait: a stranger holds the port for two polls (it never
        # answers /api/health), then the port frees; afterwards our own
        # service answers for the verify phase.
        mock_listening.side_effect = [True, True, False] + [True] * 100

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            _local_probes(version="0.100.0", health=None),
        ):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_called_once()
        # the port was polled more than once (proves we looped)
        assert mock_listening.call_count >= 3


class TestParentKillEscalation:
    """A reusable integer PID is never enough evidence to kill a process."""

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_listening", return_value=False)
    @patch("jacked.service.updater._force_kill_pid")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_refuses_pid_only_parent_kill_when_it_wont_exit(
        self, mock_popen, mock_run, mock_find, mock_force_kill, mock_listening, mock_gate,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", side_effect=[False, True]):
            updater.run_update(parent_pid=99999, extras="tray")

        mock_force_kill.assert_not_called()
        mock_popen.assert_not_called()


class TestPortStuckRecovery:
    """An occupied port is never process-ownership evidence."""

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.time.sleep", lambda _s: None)
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater._pids_bound_to_port", return_value=[54321])
    @patch("jacked.service.updater._force_kill_pid")
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_refuses_to_kill_port_squatter(
        self, mock_popen, mock_run, mock_find, mock_listening,
        mock_force_kill, mock_port_pids, mock_gate,
        mock_method, mock_ensure,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_listening.return_value = True

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            # A clock step small enough that the identification probe has a
            # real budget: it must actually ask, and be refused.
            patch("jacked.service.updater.time.monotonic",
                  side_effect=itertools.count(0.0, 0.1)),
            # The squatter never identifies itself as a jacked service, so the
            # port stays ambiguous and nothing may be signalled.
            _local_probes(health=None) as opener,
        ):
            updater.run_update(parent_pid=12345, extras="tray")

        opener.open.assert_called()
        mock_port_pids.assert_not_called()
        mock_force_kill.assert_not_called()
        mock_popen.assert_not_called()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.time.sleep", lambda _s: None)
    @patch("jacked.service.updater._pids_bound_to_port", return_value=[])
    @patch("jacked.service.updater.is_port_listening", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_aborts_when_port_cannot_be_freed(
        self, mock_popen, mock_run, mock_find, mock_listening, mock_port_pids,
        mock_gate, mock_method, mock_ensure,
        tmp_path, monkeypatch,
    ):
        """If we can't find who holds the port or can't kill them, don't spawn a start
        that will silently die — write recovery instructions instead."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            # Small clock step: the identification probe gets a real budget.
            patch("jacked.service.updater.time.monotonic",
                  side_effect=itertools.count(0.0, 0.1)),
            # The listener never answers /api/health, so it is NOT a jacked
            # service the restart would replace - it stays a squatter.
            _local_probes(health=None) as opener,
        ):
            updater.run_update(parent_pid=12345, extras="tray")

        assert opener.open.called, "the listener was never asked to identify itself"
        mock_popen.assert_not_called()
        assert (tmp_path / "recovery.txt").exists()
        assert "port 8321" in (tmp_path / "recovery.txt").read_text().lower()
        assert "unverified listener" in (tmp_path / "recovery.txt").read_text()


class TestNewServiceVerification:
    """After spawning, confirm the new tray actually bound the port."""

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.time.sleep", lambda _s: None)
    # Fake clock: sleep is a no-op, so the real 20s verify deadline would
    # busy-spin for 20 real seconds on each of the two start attempts.
    @patch("jacked.service.updater.time.monotonic",
           side_effect=itertools.count(0.0, 5.0))
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_recovery_file_written_when_new_service_never_binds(
        self, mock_popen, mock_run, mock_find, mock_listening, mock_clock,
        mock_gate, mock_method, mock_ensure,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        # Nothing ever answers on the port: it is free for every wait phase,
        # and neither the new build nor the restored one comes up.
        mock_listening.return_value = False

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            _local_probes(),
        ):
            updater.run_update(parent_pid=12345, extras="tray")

        # A new build that never binds is rolled back, and the restored build
        # is started through the same start-and-verify tail: two spawns.
        assert mock_popen.call_count == 2
        assert (tmp_path / "recovery.txt").exists()
        body = (tmp_path / "recovery.txt").read_text()
        assert "never became ready" in body
        # The port never binds for the RESTORED build either, so this run must
        # NOT claim a restoration. It names the step that failed instead.
        assert "rolled back to" not in body
        assert "stopped at the service restart step" in body


def _restored_build_binds(after_run_calls: int):
    """Listening oracle: nothing answers until the rollback reinstalled the old build.

    `is_port_listening` is the updater's only port probe: False means the port
    is free for waiting_port_free, True means a service answers for
    verifying_service. The new build never comes up, so its verify polls to
    the deadline; once the rollback has run (`run.call_count >=
    after_run_calls`) the restored build is serving, which its own tail then
    identifies and verifies.
    """

    def oracle(run):
        return run.call_count >= after_run_calls

    return oracle


class TestUpdaterRollback:
    """The tray update is a transaction: a build that cannot run is undone.

    Every test patches subprocess.run, subprocess.Popen and
    ensure_native_lifecycle, so no real install, service or supervisor is
    touched.
    """

    RB_ARGV_TAIL = ["tool", "install", "claude-jacked[tray]==0.95.0",
                    "--force", "--refresh"]

    @staticmethod
    def _run_update(
        monkeypatch, tmp_path, run_results, listen_results,
        version_answer=None, health_answer=JACKED_HEALTH, clock_step=5.0,
        lifecycle=(False, "unavailable", "test: manual spawn"),
        parent_exits=True,
    ):
        """Drive run_update with scripted subprocess, port and probe outcomes.

        `listen_results` scripts `is_port_listening` (True = something answers
        on the port) and accepts a bool, a finite list, or a run-count oracle.
        `version_answer` scripts /api/version and `health_answer` /api/health,
        the two ways a listener can identify itself. `lifecycle` is the answer
        of ensure_native_lifecycle; the default forces the manual spawn.
        `parent_exits` False keeps the old service alive past its wait.
        """
        from jacked.service import updater, update_status as us_mod

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")

        with (
            patch("jacked.service.platform.ensure_native_lifecycle",
                  return_value=lifecycle),
            patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")),
            patch("jacked.install_method.detect_install_method", return_value="uv"),
            patch("jacked.service.updater.time.sleep", lambda _s: None),
            # A fake clock: sleep is a no-op, so a real 20s verify deadline
            # would busy-spin for 20 real seconds per attempt.
            patch("jacked.service.updater.time.monotonic",
                  side_effect=itertools.count(0.0, clock_step)),
            patch("jacked.service.updater.is_port_listening") as listening,
            _local_probes(version_answer, health_answer),
            patch("jacked.service.updater.find_bin") as find,
            patch("subprocess.run") as run,
            patch("subprocess.Popen") as popen,
            patch("jacked.__version__", "0.95.0"),
            patch.object(updater, "wait_for_exit", return_value=parent_exits),
        ):
            find.side_effect = lambda name: {
                "uv": "/fake/uv", "jacked": "/fake/jacked",
            }.get(name)
            run.side_effect = list(run_results)
            _script_port_mock(listening, listen_results, run)
            updater.run_update(
                parent_pid=12345, extras="tray", target_version="0.100.0"
            )
        status = us_mod.read_status(tmp_path / "status.json")
        return run, popen, status, (tmp_path / "recovery.txt")

    def test_refused_preflight_rolls_back_and_restarts_the_old_build(
        self, tmp_path, monkeypatch
    ):
        run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                        # uv install
                _fail(1, stderr="[FAIL] ValueError: runtime_path untrusted"),
                _ok(),                                        # rollback install
                _ok(),                                        # jacked install --force
            ],
            # The port is free when the tail waits, then the restored build
            # answers on it as v0.95.0 - the build that tail expects.
            listen_results=_comes_up_after(1),
            version_answer="0.95.0",
        )

        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[1] == ["/fake/jacked", "service", "preflight"]
        assert argvs[2][1:] == self.RB_ARGV_TAIL
        assert argvs[3] == ["/fake/jacked", "install", "--force"]
        # The NEW build's settings migration never ran: the gate closed first.
        assert argvs.count(["/fake/jacked", "install", "--force"]) == 1
        # The restored build is started through the normal start+verify tail.
        popen.assert_called_once()

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["preflight"] == "failed"
        assert phases["rolling_back"] == "ok"
        assert status["overall"] == "failed"
        body = recovery.read_text(encoding="utf-8")
        assert "0.100.0" in body and "0.95.0" in body

    def test_a_service_that_never_binds_rolls_back(self, tmp_path, monkeypatch):
        run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                             # uv install
                _ok("[OK] Service contract OK"),   # preflight
                _ok(),                             # jacked install --force
                _ok(),                             # rollback install
                _ok(),                             # jacked install --force
            ],
            # The new build never answers. After the rollback the restored
            # build is serving as v0.95.0, so only the second verify passes.
            listen_results=_restored_build_binds(after_run_calls=4),
            version_answer="0.95.0",
            clock_step=0.1,
        )

        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[3][1:] == self.RB_ARGV_TAIL
        # Start attempted for the new build, then again for the restored one.
        assert popen.call_count == 2
        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["rolling_back"] == "ok"
        body = recovery.read_text(encoding="utf-8")
        assert "never became ready" in body
        assert "rolled back to v0.95.0" in body

    def test_rollback_runs_at_most_once_per_update(self, tmp_path, monkeypatch):
        """The restored build's own verify must not trigger a second rollback.

        Both attempts fail to bind here. Without the once-per-run guard the
        updater would reinstall 0.95.0 twice and loop.
        """
        run, popen, status, _recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                             # uv install
                _ok("[OK] Service contract OK"),   # preflight
                _ok(),                             # jacked install --force
                _ok(),                             # rollback install
                _ok(),                             # jacked install --force
            ],
            listen_results=False,
        )

        rollback_calls = [
            call[0][0] for call in run.call_args_list
            if call[0][0][1:] == self.RB_ARGV_TAIL
        ]
        assert len(rollback_calls) == 1
        assert popen.call_count == 2

    def test_a_failed_rollback_command_is_reported_not_hidden(
        self, tmp_path, monkeypatch
    ):
        run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                  # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),
                _fail(2),                               # rollback install fails
            ],
            listen_results=_comes_up_after(1),
            version_answer="0.95.0",
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["rolling_back"] == "failed"
        assert status["overall"] == "failed"
        body = recovery.read_text(encoding="utf-8")
        assert "stopped at the package rollback step" in body
        # The manual recovery line names the exact version to reinstall.
        assert "claude-jacked[tray]==0.95.0" in body

    def test_a_healthy_update_never_records_a_rollback(self, tmp_path, monkeypatch):
        run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),
                _ok("[OK] Service contract OK"),
                _ok(),
                _ok(),  # jacked packs update, after the service is up
            ],
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
        )

        assert status["overall"] == "succeeded"
        assert "rolling_back" not in {p["name"] for p in status["phases"]}
        assert not recovery.exists()
        argvs = [call[0][0] for call in run.call_args_list]
        assert not [a for a in argvs if a[1:] == self.RB_ARGV_TAIL]
        popen.assert_called_once()

    @pytest.mark.parametrize("earlier_failure", [True, False])
    def test_a_healthy_update_removes_the_recovery_file_of_an_earlier_failure(
        self, earlier_failure, tmp_path, monkeypatch
    ):
        """A good update removes the recovery file an earlier failure left.
        With no such file there is nothing to remove, and nothing to warn
        about either."""
        if earlier_failure:
            (tmp_path / "recovery.txt").write_text(
                "Jacked auto-update failed: an earlier run\n", encoding="utf-8"
            )

        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
        )

        assert status["overall"] == "succeeded"
        assert not recovery.exists()
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "Could not remove the update recovery file" not in log

    def test_a_recovery_file_that_cannot_be_removed_is_logged_not_fatal(
        self, tmp_path, monkeypatch, caplog
    ):
        """An earlier failure left the recovery file, and this good update
        cannot remove it (a locked file, a read-only dir). The update stays a
        success. The refusal is logged at WARNING with the exception class
        only, never its text, and the update log carries the same line: the
        detached updater's stderr goes to DEVNULL, so the update log is the
        record an operator reads."""
        import logging
        import pathlib

        stale = tmp_path / "recovery.txt"
        stale.write_text("Jacked auto-update failed: an earlier run\n", encoding="utf-8")
        real_unlink = pathlib.Path.unlink

        def refuse(self, *args, **kwargs):
            if self == stale:
                raise PermissionError(13, "Permission denied", str(self))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", refuse)
        with caplog.at_level(logging.WARNING, logger="jacked.service.updater"):
            _run, _popen, status, recovery = self._run_update(
                monkeypatch, tmp_path,
                run_results=_successful_runs(),
                listen_results=_comes_up_after(1),
                version_answer="0.100.0",
            )

        assert status["overall"] == "succeeded"
        assert recovery.exists()
        warned = [
            r.getMessage() for r in caplog.records
            if r.name == "jacked.service.updater" and r.levelno == logging.WARNING
        ]
        assert warned.count(
            "Could not remove the update recovery file (PermissionError)"
        ) == 1, warned
        assert not any("Permission denied" in w for w in warned), warned
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "WARNING: Could not remove the update recovery file (PermissionError)" in log
        assert "Permission denied" not in log


class TestUpdaterPartialRollback:
    """The tray must never say "restored" about a half-finished rollback."""

    RB_ARGV_TAIL = TestUpdaterRollback.RB_ARGV_TAIL
    _run_update = staticmethod(TestUpdaterRollback._run_update)

    def test_a_restored_build_that_cannot_migrate_is_not_a_rollback(
        self, tmp_path, monkeypatch
    ):
        run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                  # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),
                _ok(),                                  # rollback install: ok
                _fail(5),                               # its jacked install: fails
            ],
            listen_results=_comes_up_after(1),
            version_answer="0.95.0",
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["rolling_back"] == "failed"
        assert status["overall"] == "failed"
        assert "settings migration" in status["error"]
        body = recovery.read_text(encoding="utf-8")
        assert "stopped at the settings migration step" in body
        assert "rolled back to v0.95.0" not in body

    def test_a_restored_service_that_never_binds_is_not_a_rollback(
        self, tmp_path, monkeypatch
    ):
        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                  # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),
                _ok(),                                  # rollback install
                _ok(),                                  # jacked install --force
            ],
            # Nothing ever answers on the port: no service comes up.
            listen_results=False,
        )

        assert status["overall"] == "failed"
        body = recovery.read_text(encoding="utf-8")
        assert "stopped at the service restart step" in body
        assert "rolled back to v0.95.0" not in body


class TestVerifyRejectsTheWrongBuild:
    """A listener is not proof: verification must see the EXPECTED build.

    The previous build under launchd KeepAlive, a hand-run `jacked webux`, or
    the failed new build can all be answering here. Accepting any of them let
    the rollback recovery claim "v0.95.0 is restored" while another build
    served the port.
    """

    _run_update = staticmethod(TestUpdaterRollback._run_update)

    def test_a_stranger_build_on_the_port_never_verifies(
        self, tmp_path, monkeypatch
    ):
        _run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + the rollback pair
            listen_results=True,
            # Neither tail's expected build: not the 0.100.0 being installed,
            # not the 0.95.0 the rollback restores.
            version_answer="0.90.0",
            clock_step=0.1,
        )

        verify = [p for p in status["phases"] if p["name"] == "verifying_service"]
        assert verify and all(p["status"] == "failed" for p in verify)
        assert status["overall"] == "failed"
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "answers as build 0.90.0, expected 0.100.0" in log
        assert "answers as build 0.90.0, expected 0.95.0" in log
        # Nothing was restored: a stranger holding the port is not a rollback.
        body = recovery.read_text(encoding="utf-8")
        assert "rolled back to v0.95.0" not in body
        assert "stopped at the service restart step" in body


def _healthy_runs():
    """uv install, a passing preflight, jacked install --force --no-packs."""
    return [_ok(), _ok("[OK] Service contract OK"), _ok()]


def _successful_runs():
    """A whole successful update: the healthy steps, then the skill-pack
    refresh that runs once the new service is up (#178i)."""
    return _healthy_runs() + [_ok()]


class TestWaitingPortFreeIdentifiesTheListener:
    """A port held by a jacked service is not a squatter.

    `jacked install --force` (the migrating_settings phase) activates the tray,
    so the new service is often ALREADY listening when waiting_port_free runs -
    exactly what the 2026-09-11 log shows. The old bind probe was blind to that
    wildcard listener and sailed through; an honest probe must identify the
    owner instead of aborting the upgrade.
    """

    _run_update = staticmethod(TestUpdaterRollback._run_update)

    def test_a_jacked_listener_after_migrate_is_not_a_squatter(
        self, tmp_path, monkeypatch
    ):
        from jacked.service import update_status as us_mod

        succeeded = []
        real_mark = us_mod.mark_succeeded

        def spy(*args, **kwargs):
            succeeded.append(args)
            return real_mark(*args, **kwargs)

        monkeypatch.setattr(us_mod, "mark_succeeded", spy)

        with (
            patch("jacked.service.updater._force_kill_pid") as kill,
            patch("jacked.service.updater._pids_bound_to_port") as port_pids,
        ):
            _run, popen, status, recovery = self._run_update(
                monkeypatch, tmp_path,
                run_results=_successful_runs(),
                # The port never reads free: `jacked install` already activated
                # a service on it, and /api/health fingerprints it as ours.
                listen_results=True,
                version_answer="0.100.0",
                clock_step=0.1,
            )

        # The permissive path is still a no-signal path: an occupied port is
        # never ownership evidence, so nothing is looked up or killed.
        kill.assert_not_called()
        port_pids.assert_not_called()

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["waiting_port_free"] == "ok"
        assert phases["starting_service"] == "ok"
        assert phases["verifying_service"] == "ok"
        assert status["overall"] == "succeeded"
        assert succeeded, "mark_succeeded never ran for a healthy upgrade"
        assert not recovery.exists()
        popen.assert_called_once()
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "is held by a jacked service; a native restart" in log

    @pytest.mark.parametrize(
        ("label", "health"),
        [
            ("html", b"<html><body>hello</body></html>"),
            ("json-list", b'["status", "ok"]'),
            ("missing-db", b'{"status": "ok"}'),
        ],
    )
    def test_a_listener_that_is_not_jacked_is_a_squatter(
        self, label, health, tmp_path, monkeypatch
    ):
        """Answering /api/health is not enough: the shape has to be ours."""
        _run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=health,
            clock_step=0.1,
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["waiting_port_free"] == "failed", label
        assert "starting_service" not in phases
        popen.assert_not_called()
        assert "occupied by an unverified listener" in recovery.read_text(
            encoding="utf-8"
        )

    def test_a_listener_that_will_not_identify_is_still_a_squatter(
        self, tmp_path, monkeypatch
    ):
        _run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=None,   # it never answers /api/health
            clock_step=0.1,
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["waiting_port_free"] == "failed"
        # No start is attempted against a port whose owner is unknown.
        assert "starting_service" not in phases
        popen.assert_not_called()
        assert recovery.exists()
        body = recovery.read_text(encoding="utf-8")
        assert "occupied by an unverified listener" in body


class TestVerifyingServiceUsesAConnectProbe:
    """Verification asks whether the service ANSWERS, not whether a bind fits.

    With the dashboard bound to 0.0.0.0 (remote access on) a 127.0.0.1 bind
    probe still succeeds next to the wildcard listener, so the old check
    rolled healthy upgrades back. Verification asks the listener which build
    it reports and only accepts the one this update installed.
    """

    _run_update = staticmethod(TestUpdaterRollback._run_update)

    def test_a_listening_service_passes_verification(self, tmp_path, monkeypatch):
        from jacked.service import update_status as us_mod

        succeeded = []
        real_mark = us_mod.mark_succeeded

        def spy(*args, **kwargs):
            succeeded.append(args)
            return real_mark(*args, **kwargs)

        monkeypatch.setattr(us_mod, "mark_succeeded", spy)

        _run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["verifying_service"] == "ok"
        assert status["overall"] == "succeeded"
        assert succeeded, "mark_succeeded never ran for a service that answers"
        assert not recovery.exists()
        popen.assert_called_once()

    def test_a_service_that_never_answers_fails_verification(
        self, tmp_path, monkeypatch
    ):
        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + rollback install
            listen_results=False,
        )

        verify = [p for p in status["phases"] if p["name"] == "verifying_service"]
        assert verify, "the verify phase never ran"
        assert all(p["status"] == "failed" for p in verify)
        assert status["overall"] == "failed"
        assert recovery.exists()
        assert "never became ready" in recovery.read_text(encoding="utf-8")

    def test_the_reported_build_is_logged_when_version_answers(
        self, tmp_path, monkeypatch
    ):
        _run, _popen, status, _recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
        )

        assert status["overall"] == "succeeded"
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "Service reports build 0.100.0" in log

    def test_a_hanging_version_route_leaves_the_health_probe_a_budget(
        self, tmp_path, monkeypatch
    ):
        """A wedged /api/version must not eat the whole verify window.

        Given the full window the version read starves the health fallback,
        and a port that is demonstrably bound gets reported as one that never
        bound at all.
        """
        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer=TimeoutError("read timed out"),
            health_answer=JACKED_HEALTH,
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["verifying_service"] == "ok"
        assert status["overall"] == "succeeded"
        assert not recovery.exists()
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "as a jacked service, but its build could not be read" in log

    def test_the_version_read_is_capped_so_health_still_fits(
        self, tmp_path, monkeypatch
    ):
        """Every version read leaves at least the identify budget behind."""
        from jacked.service import updater

        version_budgets, health_budgets = [], []
        real_read = updater._read_reported_build
        real_identify = updater._identify_jacked_listener

        def read_spy(port, budget):
            version_budgets.append(budget)
            return real_read(port, budget)

        def identify_spy(port, budget):
            health_budgets.append(budget)
            return real_identify(port, budget)

        monkeypatch.setattr(updater, "_read_reported_build", read_spy)
        monkeypatch.setattr(updater, "_identify_jacked_listener", identify_spy)

        self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer=TimeoutError("read timed out"),
            health_answer=JACKED_HEALTH,
        )

        assert version_budgets, "the version route was never read"
        assert max(version_budgets) <= updater.VERSION_READ_TIMEOUT_SECONDS
        assert max(version_budgets) <= (
            updater.VERIFY_TIMEOUT_SECONDS - updater.IDENTIFY_TIMEOUT_SECONDS
        )
        assert health_budgets, "the health fallback never ran"
        assert max(health_budgets) > 0, "the fallback was left no budget at all"

    def test_a_bound_port_that_never_identifies_is_not_called_unbound(
        self, tmp_path, monkeypatch
    ):
        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + the rollback pair
            # Free when each tail waits, then bound but mute: it accepts the
            # connection and will not say what it is on either route.
            listen_results=_free_at_each_tail_start(),
            version_answer=TimeoutError("read timed out"),
            health_answer=b"<html><body>not jacked</body></html>",
            clock_step=0.1,
        )

        verify = [p for p in status["phases"] if p["name"] == "verifying_service"]
        assert verify and all(p["status"] == "failed" for p in verify)
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        # A port that accepted the connection is never reported as one that
        # never bound, on either tail.
        assert log.count(
            "is bound but the listener never identified itself as jacked"
        ) == len(verify)
        assert "did not bind" not in log
        # LOW 1: both restart tails reject for the SAME reason, and each must
        # still say so once - a diagnosis set shared across tails silenced the
        # second one.
        assert log.count(
            "is not identified as jacked: /api/health did not answer JSON"
        ) == len(verify)
        assert "stopped at the service restart step" in recovery.read_text(
            encoding="utf-8"
        )

    def test_a_silent_version_endpoint_does_not_fail_the_phase(
        self, tmp_path, monkeypatch
    ):
        _run, _popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer=None,      # /api/version refuses the connection
            health_answer=JACKED_HEALTH,   # but /api/health still identifies it
        )

        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["verifying_service"] == "ok"
        assert status["overall"] == "succeeded"
        assert not recovery.exists()
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert "as a jacked service, but its build could not be read" in log


class TestPreflightSubprocessGuards:
    """The preflight gate is bounded and never crashes the detached helper."""

    @staticmethod
    def _run_with_preflight_raising(monkeypatch, tmp_path, exc):
        from jacked.service import updater, update_status as us_mod

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        calls = []

        def fake_run(args, *a, **kw):
            calls.append((list(args), kw))
            if list(args)[1:] == ["service", "preflight"]:
                raise exc
            return _ok()

        with (
            patch("jacked.service.platform.ensure_native_lifecycle",
                  return_value=(False, "unavailable", "test: manual spawn")),
            patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")),
            patch("jacked.install_method.detect_install_method", return_value="uv"),
            patch("jacked.service.updater.time.sleep", lambda _s: None),
            patch("jacked.service.updater.time.monotonic",
                  side_effect=itertools.count(0.0, 5.0)),
            patch("jacked.service.updater.is_port_listening",
                  side_effect=_comes_up_after(1)),
            _local_probes(version="0.95.0"),
            patch("jacked.service.updater.find_bin") as find,
            patch("subprocess.run", side_effect=fake_run),
            patch("subprocess.Popen"),
            patch("jacked.__version__", "0.95.0"),
            patch.object(updater, "wait_for_exit", return_value=True),
        ):
            find.side_effect = lambda name: {
                "uv": "/fake/uv", "jacked": "/fake/jacked",
            }.get(name)
            updater.run_update(
                parent_pid=12345, extras="tray", target_version="0.100.0"
            )
        return calls, us_mod.read_status(tmp_path / "status.json")

    def test_preflight_carries_a_timeout(self, tmp_path, monkeypatch):
        from jacked.service import updater

        calls, _status = self._run_with_preflight_raising(
            monkeypatch, tmp_path, OSError("boom")
        )
        preflight = [
            kw for argv, kw in calls if argv[1:] == ["service", "preflight"]
        ]
        assert preflight, "the preflight step never ran"
        assert preflight[0]["timeout"] == updater.PREFLIGHT_TIMEOUT_SECONDS

    def test_a_preflight_that_cannot_run_is_a_refusal(self, tmp_path, monkeypatch):
        calls, status = self._run_with_preflight_raising(
            monkeypatch, tmp_path, OSError("no such binary")
        )
        assert status["overall"] == "failed"
        assert "no such binary" in status["error"]
        # It rolled back rather than crashing the detached helper.
        assert any(
            argv[1:] == ["tool", "install", "claude-jacked[tray]==0.95.0",
                         "--force", "--refresh"]
            for argv, _kw in calls
        )

    def test_a_preflight_that_hangs_is_a_refusal(self, tmp_path, monkeypatch):
        import subprocess as _sp

        _calls, status = self._run_with_preflight_raising(
            monkeypatch, tmp_path, _sp.TimeoutExpired(cmd="preflight", timeout=120),
        )
        assert status["overall"] == "failed"
        assert "did not answer within" in status["error"]


class TestSpawnDetached:
    @patch("subprocess.Popen")
    def test_posix_sets_start_new_session(self, mock_popen):
        from jacked.service.updater import _spawn_detached
        with patch.object(sys, "platform", "darwin"):
            _spawn_detached(["/bin/true"])
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL

    @patch("subprocess.Popen")
    def test_windows_uses_no_window_flag(self, mock_popen):
        from jacked.service.updater import _spawn_detached
        with patch.object(sys, "platform", "win32"):
            with patch.object(subprocess, "CREATE_NO_WINDOW", 0x8, create=True):
                _spawn_detached(["cmd", "/c", "exit"])
        kwargs = mock_popen.call_args[1]
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x8  # CREATE_NO_WINDOW (hidden console — no popped window)


class TestFindUpdaterPython:
    def test_uses_current_interpreter(self):
        """Helper must run in a Python that can import jacked.service.updater —
        that means the tool venv Python (sys.executable), not a system Python
        that wouldn't have jacked on its path."""
        from jacked.service.updater import _find_updater_python
        assert _find_updater_python() == sys.executable

    def test_chosen_interpreter_can_import_updater_module(self):
        """Integration check: chosen Python must actually import the module.

        Catches the class of bug where we picked a Python that doesn't have
        jacked on sys.path. This is what the detached helper depends on."""
        from jacked.service.updater import _find_updater_python
        py = _find_updater_python()
        result = subprocess.run(
            [py, "-c", "import jacked.service.updater"],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Chosen Python {py} cannot import jacked.service.updater: "
            f"{result.stderr.decode(errors='replace')}"
        )


class TestJackedInstallFailure:
    """A settings migration that fails is a transaction failure.

    It used to `return` with the OLD tray already gone: no rollback, no
    restart, no service at all until the user found the log.
    """

    RB_ARGV_TAIL = TestUpdaterRollback.RB_ARGV_TAIL
    _run_update = staticmethod(TestUpdaterRollback._run_update)

    def test_a_failed_settings_migration_rolls_back_and_restarts(
        self, tmp_path, monkeypatch
    ):
        run, popen, status, recovery = self._run_update(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                             # uv install
                _ok("[OK] Service contract OK"),   # preflight
                _fail(1),                          # jacked install --force
                _ok(),                             # rollback install
                _ok(),                             # jacked install --force
            ],
            listen_results=_comes_up_after(1),
            version_answer="0.95.0",
        )

        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[3][1:] == self.RB_ARGV_TAIL
        # The previous build is brought back up: the machine keeps a service.
        popen.assert_called_once()
        phases = {p["name"]: p["status"] for p in status["phases"]}
        assert phases["migrating_settings"] == "failed"
        assert phases["rolling_back"] == "ok"
        assert status["overall"] == "failed"
        body = recovery.read_text(encoding="utf-8")
        assert "rolled back to v0.95.0" in body
        assert "settings.json.bak-" in body


class TestSpawnFromTrayWindows:
    """Windows tray-update path uses cmd.exe batch, not a Python subprocess.

    These tests call _spawn_windows_tray_updater directly rather than going
    through the sys.platform dispatch — mocking sys.platform is unreliable
    because stdlib modules (subprocess, shutil) cached their platform-check
    at import time.
    """

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value="C:\\Users\\x\\.local\\bin\\uv.exe")
    @patch("subprocess.Popen")
    def test_windows_spawns_cmd_batch(
        self, mock_popen, mock_find, mock_method, mock_gate, monkeypatch, tmp_path,
    ):
        """The helper spawns a detached cmd.exe batch file."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False,
        )

        updater._spawn_windows_tray_updater(parent_pid=12345, extras="tray")

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == system_program("cmd")
        assert args[1] == "/c"
        assert args[2].endswith(".bat")
        kwargs = mock_popen.call_args[1]
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x08000000  # CREATE_NO_WINDOW — no flashing find/timeout windows
        assert not (flags & 0x00000008)  # never DETACHED_PROCESS

        import os as _os
        try:
            _os.unlink(args[2])
        except OSError:
            pass

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value="C:\\Users\\x\\.local\\bin\\uv.exe")
    @patch("subprocess.Popen")
    def test_windows_batch_contains_uv_install_and_service_start(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        """The batch must run uv tool install --force AND jacked service start."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(
            subprocess, "DETACHED_PROCESS", 0x8, raising=False,
        )

        updater._spawn_windows_tray_updater(parent_pid=99999, extras="tray")

        batch_path = mock_popen.call_args[0][0][2]
        with open(batch_path) as f:
            body = f.read()
        try:
            assert 'tool' in body and 'install' in body
            assert 'claude-jacked[tray]' in body
            assert '--force' in body
            assert "jacked install --force" in body
            assert "jacked service start" in body
            assert "PID eq 99999" in body
            assert 'start "" /B' in body
        finally:
            import os as _os
            try:
                _os.unlink(batch_path)
            except OSError:
                pass

    @patch("jacked.install_method.detect_install_method", return_value="pip")
    @patch("subprocess.Popen")
    def test_windows_updater_refuses_pip_method(
        self, mock_popen, mock_method, monkeypatch, tmp_path,
    ):
        """0.41.24: Windows tray updater refuses pip method via
        can_auto_upgrade gate, writes recovery file, does NOT spawn
        the batch helper.  Previously pip method produced a
        `python -m pip install` batch that crashed in uv-managed
        venvs with 'No module named pip' (0.41.17 bug)."""
        from jacked.service import updater
        recovery_path = tmp_path / "recovery.txt"
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "_write_recovery",
                            lambda msg: recovery_path.write_text(msg))
        monkeypatch.setattr(
            subprocess, "DETACHED_PROCESS", 0x8, raising=False,
        )

        updater._spawn_windows_tray_updater(parent_pid=12345, extras="tray")

        assert recovery_path.exists()
        body = recovery_path.read_text()
        assert "refused" in body.lower()
        assert "uv tool install" in body
        mock_popen.assert_not_called()

    @patch("jacked.service.updater.find_bin", return_value="C:\\fake\\uv.exe")
    @patch("subprocess.Popen")
    def test_posix_still_uses_python_subprocess(self, mock_popen, mock_find, monkeypatch, tmp_path):
        """POSIX path unchanged — still spawns python -m jacked.service.updater."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")

        with patch.object(sys, "platform", "darwin"):
            updater.spawn_updater_from_tray(parent_pid=12345, extras="tray")

        args = mock_popen.call_args[0][0]
        assert args[1:4] == ["-I", "-m", "jacked.service.updater"]


class TestMainEntrypoint:
    def test_missing_pid_exits_2(self):
        from jacked.service import updater
        import sys as real_sys
        argv_backup = real_sys.argv
        real_sys.argv = ["updater"]
        try:
            try:
                updater._cli()
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 2
        finally:
            real_sys.argv = argv_backup

    def test_bad_pid_exits_2(self):
        from jacked.service import updater
        import sys as real_sys
        argv_backup = real_sys.argv
        real_sys.argv = ["updater", "not-a-number"]
        try:
            try:
                updater._cli()
                assert False, "expected SystemExit"
            except SystemExit as e:
                assert e.code == 2
        finally:
            real_sys.argv = argv_backup


class TestUpdaterWritesStatus:
    # Hermetic pin: without this the start phase drives a REAL launchctl.
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test: manual spawn"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_succeeded_status_with_all_phases(
        self, mock_popen, mock_run, mock_find, mock_listening, mock_gate, mock_method,
        mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        from jacked.service.update_phases import PHASE_NAMES
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()

        # Port free for the wait phase, then the installed build answers as
        # 0.41.19 - exactly the version this update targeted.
        mock_listening.side_effect = _comes_up_after(1)

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            _local_probes(version="0.41.19"),
        ):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.19")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "succeeded"
        assert data["to_version"] == "0.41.19"
        phase_names = [p["name"] for p in data["phases"]]
        # `rolling_back` is an exceptional phase: a healthy update must never
        # record it. Every other phase must be present and ok.
        assert "rolling_back" not in phase_names
        for expected in PHASE_NAMES:
            if expected == "rolling_back":
                continue
            assert expected in phase_names, f"missing phase {expected}"
        for p in data["phases"]:
            assert p["status"] == "ok", f"phase {p['name']} ended with {p['status']}"


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_install_failure_writes_failed_phase_and_overall(
        self, mock_popen, mock_run, mock_find, mock_gate, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.19")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data["overall"] == "failed"
        install_phase = next(
            (p for p in data["phases"] if p["name"] == "installing_package"),
            None,
        )
        assert install_phase is not None
        assert install_phase["status"] == "failed"


    @patch("jacked.install_method.can_auto_upgrade", return_value=(False, "editable — run git pull"))
    def test_run_update_refuses_non_upgradable(
        self, mock_gate, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        updater.run_update(parent_pid=12345, extras="tray")
        # No status file written (run was refused before init_status)
        assert not (tmp_path / "status.json").exists()
        # Recovery file written with the reason
        assert (tmp_path / "recovery.txt").exists()
        assert "editable" in (tmp_path / "recovery.txt").read_text().lower()


class TestPosixSpawnThreadsTargetVersion:
    @patch("subprocess.Popen")
    @patch("jacked.service.updater._find_updater_python", return_value="/fake/python")
    def test_posix_spawn_threads_target_version_and_port(
        self, mock_py, mock_popen,
    ):
        import sys as _sys
        from jacked.service import updater
        with patch.object(_sys, "platform", "darwin"):
            updater.spawn_updater_from_tray(
                parent_pid=12345, extras="tray",
                target_version="0.41.19", port=9000,
            )
        argv = mock_popen.call_args[0][0]
        assert "--target-version" in argv
        i = argv.index("--target-version")
        assert argv[i + 1] == "0.41.19"
        assert "--port" in argv
        j = argv.index("--port")
        assert argv[j + 1] == "9000"


class TestCliForwards:
    def test_cli_forwards_target_version_and_port(self, monkeypatch):
        from jacked.service import updater
        captured = {}

        def fake_run(parent_pid, extras="tray", target_version=None, port=8321):
            captured["target_version"] = target_version
            captured["port"] = port

        monkeypatch.setattr(updater, "run_update", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["updater", "12345", "tray", "--target-version", "0.41.19", "--port", "9000"],
        )
        updater._cli()
        assert captured["target_version"] == "0.41.19"
        assert captured["port"] == 9000


    def test_cli_empty_target_version_becomes_none(self, monkeypatch):
        from jacked.service import updater
        captured = {}
        def fake_run(*a, target_version=None, port=8321, **kw):
            captured["target_version"] = target_version
        monkeypatch.setattr(updater, "run_update", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["updater", "12345", "tray", "--target-version", ""],
        )
        updater._cli()
        assert captured["target_version"] is None


class TestWindowsBatchPhases:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_has_all_phases_in_order_plus_success_terminal(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.19",
        )
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == system_program("cmd")
        batch_path = args[2]
        body = open(batch_path).read()
        try:
            # Target version threaded correctly
            assert "0.41.19" in body
            assert '"next"' not in body, "target_version placeholder leaked"

            required_in_order = [
                "_update_status_init",
                "waiting_for_parent in_progress",
                "waiting_for_parent ok",
                "installing_package in_progress",
                "installing_package ok",
                "preflight in_progress",
                "preflight ok",
                "migrating_settings in_progress",
                "migrating_settings ok",
                "waiting_port_free in_progress",
                "waiting_port_free ok",
                "starting_service in_progress",
                "starting_service ok",
                "verifying_service in_progress",
                "verifying_service ok",
                "_update_status_succeed",
            ]
            last_idx = -1
            for frag in required_in_order:
                idx = body.find(frag)
                assert idx >= 0, f"batch missing fragment: {frag}"
                assert idx > last_idx, f"fragment out of order: {frag}"
                last_idx = idx

            # The tray opens the file:// bootstrap now; the batch must not
            # open any browser tab (it would race a guaranteed-down server).
            assert 'start "" "http' not in body
            assert "/update.html" not in body
        finally:
            import os as _os
            try:
                _os.unlink(batch_path)
            except OSError:
                pass

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_uses_next_placeholder_when_target_version_missing(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version=None,
        )
        batch_path = mock_popen.call_args[0][0][2]
        body = open(batch_path).read()
        try:
            assert '"next"' in body
            assert "waiting_for_parent in_progress" in body
            assert "_update_status_succeed" in body
        finally:
            import os as _os
            try:
                _os.unlink(batch_path)
            except OSError:
                pass

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_threads_custom_port(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.19", port=9000,
        )
        body = open(mock_popen.call_args[0][0][2]).read()
        try:
            # Port still threaded through the verify step; the batch no longer
            # opens /update.html itself.
            assert "/update.html" not in body
            assert "127.0.0.1:9000/api/version" in body
            assert "bind :9000" in body
        finally:
            import os as _os
            try:
                _os.unlink(mock_popen.call_args[0][0][2])
            except OSError:
                pass

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_never_opens_a_browser(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        """The tray already opened the file:// bootstrap. The detached batch
        runs while the service is guaranteed down, so a `start ""` here would
        only spawn a dead error tab."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.19", port=9000,
        )
        body = open(mock_popen.call_args[0][0][2]).read()
        try:
            assert 'start "" "http' not in body
        finally:
            import os as _os
            try:
                _os.unlink(mock_popen.call_args[0][0][2])
            except OSError:
                pass


class TestRunUpdateReusesTrayPreInit:
    # Hermetic pin: without this the start phase drives a REAL launchctl on
    # macOS, which the conftest tripwire refuses.
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test: manual spawn"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_reuses_tray_pre_init_and_completes(
        self, mock_popen, mock_run, mock_find, mock_listening,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        mock_listening.side_effect = _comes_up_after(1)

        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="0.41.19",
            to_version="0.41.20",
            method="uv",
            preinit=True,
        )

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            _local_probes(version="0.41.20"),
        ):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(us_mod.UPDATE_STATUS_FILE)
        assert data["overall"] == "succeeded"
        assert data["to_version"] == "0.41.20"


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    def test_truly_busy_lock_still_refused(
        self, mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")

        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="a", to_version="b", method="uv",
        )
        us_mod.begin_phase(us_mod.UPDATE_STATUS_FILE, "installing_package")

        updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(us_mod.UPDATE_STATUS_FILE)
        assert data["current_phase"] == "installing_package"


class TestRunUpdateTerminalStatus:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.find_bin", return_value=None)
    def test_uv_missing_branch_writes_failed(
        self, mock_find, mock_gate, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "uv" in (data.get("error") or "").lower()


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_listening", return_value=False)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_jacked_missing_after_install_writes_failed(
        self, mock_popen, mock_run, mock_find, mock_listening,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        # uv is found (first call), jacked is not (second call)
        find_calls = iter(["/fake/uv", None])
        mock_find.side_effect = lambda name: next(find_calls)
        mock_run.return_value = MagicMock(returncode=0)
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "jacked" in (data.get("error") or "").lower()


    # Hermetic pin: without this the start phase drives a REAL launchctl.
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test: manual spawn"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_listening")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_mark_succeeded_exception_degrades_to_failed(
        self, mock_popen, mock_run, mock_find, mock_listening,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        mock_listening.side_effect = _comes_up_after(1)

        def boom(*_args, **_kw):
            raise OSError("disk full")
        monkeypatch.setattr(us_mod, "mark_succeeded", boom)

        with (
            patch.object(updater, "wait_for_exit", return_value=True),
            _local_probes(version="0.41.20"),
        ):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "mark_succeeded" in (data.get("error") or "")


def test_run_update_handles_upgrade_command_valueerror(tmp_path, monkeypatch):
    """0.41.24: if upgrade_command raises ValueError (defensive raise
    for pip method), run_update writes a recovery file instead of
    crashing the detached helper."""
    from jacked.service import updater, update_status as us_mod
    from jacked import install_method
    recovery_path = tmp_path / "recovery.txt"
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(updater, "_write_recovery",
                        lambda msg: recovery_path.write_text(msg))
    monkeypatch.setattr(install_method, "can_auto_upgrade", lambda: (True, ""))
    # updater imports upgrade_command inside run_update via
    # `from jacked.install_method import upgrade_command`, so patching the
    # source module intercepts the call.

    def _raise(extras):
        raise ValueError("pip auto-upgrade is not supported (test)")

    monkeypatch.setattr(install_method, "upgrade_command", _raise)
    monkeypatch.setattr(install_method, "upgrade_command_label", _raise)

    with patch.object(updater, "wait_for_exit", return_value=True):
        updater.run_update(parent_pid=0, extras="tray", target_version="0.41.24")

    assert recovery_path.exists()
    body = recovery_path.read_text()
    assert "pip auto-upgrade" in body
    assert "uv tool install" in body


def test_windows_batch_checks_errorlevel_after_status_writes(
    tmp_path, monkeypatch,
):
    """After each `_update_status <phase> in_progress` line, the IMMEDIATELY
    next non-empty line must be an errorlevel guard."""
    from unittest.mock import patch as _patch
    from jacked.service import updater
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    with _patch("jacked.install_method.detect_install_method", return_value="uv"), \
         _patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe"), \
         _patch("subprocess.Popen") as mock_popen:
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.20",
        )
    body_path = mock_popen.call_args[0][0][2]
    body = open(body_path).read()
    try:
        lines = body.splitlines()
        for phase in ["waiting_for_parent", "installing_package",
                      "migrating_settings", "waiting_port_free",
                      "starting_service", "verifying_service"]:
            in_prog_idx = None
            for i, ln in enumerate(lines):
                if f"_update_status {phase} in_progress" in ln:
                    in_prog_idx = i
                    break
            assert in_prog_idx is not None, f"missing begin for {phase}"
            for j in range(in_prog_idx + 1, len(lines)):
                candidate = lines[j].strip()
                if not candidate:
                    continue
                # The exact compare: a crashed shim exits with a negative
                # code, which "if errorlevel 1" does not see.
                assert candidate == 'if not "%ERRORLEVEL%"=="0" (', (
                    f"first line after `_update_status {phase} in_progress` "
                    f"is not an errorlevel guard — got: {candidate!r}"
                )
                break
    finally:
        import os as _os
        try:
            _os.unlink(body_path)
        except OSError:
            pass


class TestUpdaterRollbackStepsThatCannotSpawn:
    """A rollback step whose binary is gone must be recorded, not raised.

    `subprocess.run` raises OSError when `uv` or `jacked` is missing or not
    executable. That escaped `_rollback` and killed the detached helper before
    the failed step reached the status file or the recovery file, so the user
    was left with a dead service and no breadcrumb at all.
    """

    @staticmethod
    def _run_update(monkeypatch, tmp_path, run_results):
        return TestUpdaterRollback._run_update(
            monkeypatch, tmp_path, run_results, listen_results=False,
        )

    def test_a_package_rollback_that_cannot_spawn_is_recorded(
        self, tmp_path, monkeypatch
    ):
        from jacked.service import update_status as us_mod

        _run, _popen, _status, recovery = self._run_update(
            monkeypatch,
            tmp_path,
            [
                _ok(),                                  # uv install
                _fail(1, stderr="[FAIL] refused"),      # preflight
                OSError("No such file or directory: '/fake/uv'"),
            ],
        )
        raw = us_mod._read_raw(tmp_path / "status.json")
        assert raw["overall"] == "failed"
        assert "package rollback" in raw["error"]
        assert recovery.exists()
        assert "rolled back to v0.95.0" not in recovery.read_text()

    def test_a_settings_rollback_that_cannot_spawn_is_recorded(
        self, tmp_path, monkeypatch
    ):
        from jacked.service import update_status as us_mod

        _run, _popen, _status, recovery = self._run_update(
            monkeypatch,
            tmp_path,
            [
                _ok(),                                  # uv install
                _fail(1, stderr="[FAIL] refused"),      # preflight
                _ok(),                                  # rollback package
                PermissionError("jacked is not executable"),
            ],
        )
        raw = us_mod._read_raw(tmp_path / "status.json")
        assert raw["overall"] == "failed"
        assert "settings migration" in raw["error"]
        assert recovery.exists()
        assert "rolled back to v0.95.0" not in recovery.read_text()


class TestUpdaterHoldsTheExclusiveLock:
    """`run_update` serializes against every other updater on the machine."""

    def test_run_update_refuses_while_another_process_holds_the_lock(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from jacked.service import updater, update_status as us_mod

        status = tmp_path / "status.json"
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", status)

        holder_script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from jacked.service.update_status import acquire_update_lock\n"
            "handle = acquire_update_lock(Path(%r))\n"
            "print('HELD' if handle is not None else 'REFUSED', flush=True)\n"
            "sys.stdin.readline()\n"
        ) % (str(Path(__file__).resolve().parents[3]), str(status))

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"
            with (
                patch("jacked.install_method.can_auto_upgrade",
                      return_value=(True, "")),
                patch("jacked.install_method.detect_install_method",
                      return_value="uv"),
                patch("subprocess.run") as run,
                patch("subprocess.Popen") as popen,
                patch.object(updater, "wait_for_exit", return_value=True),
            ):
                updater.run_update(
                    parent_pid=12345, extras="tray", target_version="0.100.0"
                )
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)

        run.assert_not_called()
        popen.assert_not_called()
        assert not status.exists(), "a refused updater clobbered the status file"
        assert "already running" in (tmp_path / "recovery.txt").read_text()

    def test_a_finished_update_releases_the_lock(self, tmp_path, monkeypatch):
        from jacked.service.update_status import acquire_update_lock

        TestUpdaterRollback._run_update(
            monkeypatch, tmp_path, [_ok(), _ok(), _ok()],
            listen_results=True, health_answer=None,
        )
        handle = acquire_update_lock(tmp_path / "status.json")
        assert handle is not None, "run_update never released the update lock"
        handle.close()


class TestUpdaterFunctionLength:
    """Guardrail on the update transaction's own helpers: 50 lines each.

    `_rollback` grew past that, and the length is what let its package step
    and its settings step drift into one unreadable block. `_start_and_verify`
    went the same way. Both are now composed of named steps.
    """

    STEP_FUNCTIONS = {
        "_rollback",
        "_rollback_package",
        "_rollback_settings",
        "_run_rollback_step",
        "_fail_and_recover",
        "_start_and_verify",
        "_wait_port_free",
        "_spawn_service",
        "_verify_service",
        "_verified_build",
        "_identify_jacked_listener",
        "_read_reported_build",
    }

    def test_every_transaction_step_is_under_50_lines(self):
        import ast
        import inspect

        from jacked.service import updater

        tree = ast.parse(inspect.getsource(updater))
        seen, too_long = set(), []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in self.STEP_FUNCTIONS:
                continue
            seen.add(node.name)
            length = node.end_lineno - node.lineno + 1
            if length > 50:
                too_long.append((node.name, length))
        assert too_long == [], f"functions over 50 lines: {too_long}"
        missing = self.STEP_FUNCTIONS - seen
        assert not missing, f"the guardrail names functions that are gone: {missing}"


class TestUpdaterLockFailsClosed:
    def test_a_lock_error_refuses_the_update_before_any_install(self, monkeypatch):
        from jacked.service import update_status as us
        from jacked.service import updater

        def _boom(path):
            raise PermissionError("no lock for you")

        monkeypatch.setattr(us, "acquire_update_lock", _boom)
        monkeypatch.setattr(
            "jacked.install_method.can_auto_upgrade", lambda: (True, "")
        )
        monkeypatch.setattr(
            "jacked.install_method.detect_install_method", lambda: "uv"
        )
        ran = []
        monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: ran.append(a))
        updater.run_update(4242, "tray", target_version="9.9.9", port=8321)
        assert ran == []
        assert "update lock could not be taken" in updater.RECOVERY_FILE.read_text()

    def test_tray_batch_never_carries_an_unvalidated_target_version(self):
        from jacked.service import updater

        captured = {}

        class _Popen:
            def __init__(self, args, **kwargs):
                captured["args"] = args

        with (
            patch.object(updater.subprocess, "Popen", _Popen),
            patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")),
            patch("jacked.install_method.detect_install_method", return_value="uv"),
            patch("jacked.findbin.find_bin", side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n)),
            patch("sys.platform", "win32"),
        ):
            updater._spawn_windows_tray_updater(
                4242, "tray", target_version='9.9" & del C:\\ &', port=8321
            )
        from pathlib import Path as _P

        body = _P(captured["args"][-1]).read_text(encoding="utf-8")
        assert "del C:" not in body
        assert '"next"' in body



class TestClassifyJackedListener:
    """The three-way /api/health verdict that the install release uses."""

    @pytest.mark.parametrize(
        "error, verdict",
        [
            (TimeoutError("SECRET"), "unknown"),
            (OSError("SECRET"), "unknown"),
            (ConnectionRefusedError("SECRET"), "foreign"),
            (ConnectionResetError("SECRET"), "foreign"),
            (http.client.RemoteDisconnected("SECRET"), "foreign"),
            (http.client.BadStatusLine("SECRET"), "foreign"),
        ],
        ids=["timeout", "other", "refused", "reset", "remote-closed", "not-http"],
    )
    def test_errors_map_to_a_verdict(self, error, verdict):
        from jacked.service import updater

        opener = MagicMock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            got, reason = updater.classify_jacked_listener(8321, 2.0)
        assert got == verdict
        assert "SECRET" not in reason

    def test_a_jacked_body_is_jacked_and_the_wrapper_agrees(self):
        from jacked.service import updater

        with _local_probes(health=JACKED_HEALTH):
            assert updater.classify_jacked_listener(8321, 2.0)[0] == "jacked"
            assert updater._identify_jacked_listener(8321, 2.0)[0] is True

    def test_an_unknown_verdict_is_not_jacked_for_the_squatter_check(self):
        from jacked.service import updater

        opener = MagicMock()
        opener.open.side_effect = TimeoutError()
        with patch("urllib.request.build_opener", return_value=opener):
            assert updater._identify_jacked_listener(8321, 2.0)[0] is False



class TestSkillPacksRefreshAfterTheService:
    """#178i: the settings migration no longer downloads skill packs while the
    machine has no service, and a pack failure never fails the upgrade."""

    def test_the_migration_skips_packs_and_has_a_timeout(self, monkeypatch):
        import subprocess as sp

        from jacked.service import updater

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"], seen["timeout"] = argv, kwargs.get("timeout")
            return _ok()

        monkeypatch.setattr(updater.subprocess, "run", fake_run)
        assert updater.run_migrate_settings("/fake/jacked") == (0, None)
        assert seen["argv"] == ["/fake/jacked", "install", "--force", "--no-packs"]
        assert seen["timeout"] == updater.MIGRATE_TIMEOUT_SECONDS

        def hang(argv, **kwargs):
            raise sp.TimeoutExpired(argv, kwargs.get("timeout"))

        monkeypatch.setattr(updater.subprocess, "run", hang)
        code, error = updater.run_migrate_settings("/fake/jacked")
        assert code != 0 and "timed out" in error

    def test_a_failed_pack_refresh_is_a_warning_not_a_failure(self, tmp_path, monkeypatch):
        _run, _popen, status, _recovery = TestUpdaterRollback()._run_update(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_fail(1)],
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
        )
        assert status["overall"] == "succeeded"
        packs = [p for p in status["phases"] if p["name"] == "refreshing_packs"]
        assert packs and packs[-1]["status"] == "warning"


class TestFinalGuardFollowsTheOutcome:
    """#140i: the final guard acts on how the run ended.

    The guard used to ask only "did the flow attempt a restart". The
    port_busy exit never gets to the restart, so the guard then ran
    native_restart (launchctl kickstart -k on macOS) right after the
    recovery file said that no port owner was killed. A deliberate
    hands-off exit now stays hands-off. An early exit or a crash still gets
    its kickstart, and a run that restarted the service gets no second one.
    """

    _run_update = staticmethod(TestUpdaterRollback._run_update)

    SKIP_PREFIX = "Final guard: skipped"
    PORT_BUSY_SKIPPED = (
        "Final guard: skipped (port held by an unverified listener; "
        "nothing restarted)"
    )
    PARENT_ALIVE_SKIPPED = (
        "Final guard: skipped (the old service did not exit; nothing restarted)"
    )
    KICKSTART = "Final guard: no restart attempted by upgrade flow. Kickstart."
    PORT_BUSY_RECOVERY = (
        "Jacked auto-update stopped safely because port 8321 is occupied by an "
        "unverified listener. No port owner was killed. Run `jacked service "
        "status` for ownership and quarantine guidance.\n"
    )
    PARENT_ALIVE_RECOVERY = (
        "Jacked auto-update stopped safely because the old service did not "
        "exit.\nNo process was killed from PID-only evidence. Stop jacked "
        "normally, then retry.\n"
    )

    def _drive(self, monkeypatch, tmp_path, kick_result=(True, "test-kickstart"), **kwargs):
        """Run one update with native_restart mocked, and read its log."""
        with patch(
            "jacked.service.platform.native_restart",
            return_value=kick_result,
        ) as kick:
            _run, popen, status, recovery = self._run_update(
                monkeypatch, tmp_path, **kwargs
            )
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        return kick, popen, status, recovery, log

    def test_a_port_held_by_an_unverified_listener_is_left_alone(
        self, tmp_path, monkeypatch
    ):
        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=None,   # the holder never answers /api/health
            clock_step=0.1,
        )

        popen.assert_not_called()
        kick.assert_not_called()
        assert self.PORT_BUSY_SKIPPED in log
        assert self.KICKSTART not in log
        # The hands-off exit is the intended behavior, so its recovery text
        # stays exactly as it was: nothing was killed, and it says so.
        assert recovery.read_text(encoding="utf-8") == self.PORT_BUSY_RECOVERY
        # The status record ends failed, names the held port, and gives the
        # command to run.
        assert status["overall"] == "failed"
        assert status["error"] == "port 8321 is held by an unverified listener"
        assert status["recovery"] == "jacked service status"

    def test_a_rollback_tail_that_finds_the_port_held_is_left_alone(
        self, tmp_path, monkeypatch
    ):
        """The same exit on the restored build's tail is hands-off too."""
        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                        # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),   # preflight refuses
                _ok(),                                        # rollback install
                _ok(),                                        # jacked install --force
            ],
            listen_results=True,
            health_answer=None,
            clock_step=0.1,
        )

        popen.assert_not_called()
        kick.assert_not_called()
        assert self.PORT_BUSY_SKIPPED in log
        assert self.KICKSTART not in log
        assert status["overall"] == "failed"
        assert "stopped at the service restart step" in recovery.read_text(
            encoding="utf-8"
        )

    def test_an_old_service_that_did_not_exit_is_left_alone(
        self, tmp_path, monkeypatch
    ):
        """The parent refusal is hands-off too.

        On macOS the guard's kickstart is `launchctl kickstart -k`, which
        kills the job, and that job can be the old service that is still
        alive. The recovery text says that no process was killed.
        """
        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=[],   # no step may run: the update stops first
            listen_results=False,
            parent_exits=False,
        )

        popen.assert_not_called()
        kick.assert_not_called()
        assert self.PARENT_ALIVE_SKIPPED in log
        assert self.KICKSTART not in log
        # The recovery text stays as it was. It is true now that the guard
        # does not kick the job.
        assert recovery.read_text(encoding="utf-8") == self.PARENT_ALIVE_RECOVERY
        # The waiting_for_parent phase already closed the record as failed.
        assert status["overall"] == "failed"
        assert status["error"] == (
            "parent process did not exit; ownership cannot be revalidated"
        )
        assert status["recovery"] == (
            "stop the owned service normally, then retry the update"
        )

    def test_a_restart_that_ran_is_never_logged_as_nothing_restarted(
        self, tmp_path, monkeypatch
    ):
        """A later hands-off exit does not hide an earlier restart."""
        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + the rollback pair
            # The new build never comes up. Once the rollback has run (four
            # subprocess calls), an unverified listener holds the port for
            # the restored build's tail.
            listen_results=lambda run: run.call_count >= 4,
            health_answer=None,
            clock_step=0.1,
        )

        popen.assert_called_once()   # the new build's start ran
        kick.assert_not_called()
        assert self.SKIP_PREFIX not in log
        assert self.KICKSTART not in log
        assert status["overall"] == "failed"
        assert "stopped at the service restart step" in recovery.read_text(
            encoding="utf-8"
        )

    def test_a_crash_before_any_restart_still_gets_its_kickstart(
        self, tmp_path, monkeypatch
    ):
        """An unplanned exit leaves the guard pending, so it kicks as before."""
        with (
            patch(
                "jacked.service.platform.native_restart",
                return_value=(True, "test-kickstart"),
            ) as kick,
            pytest.raises(RuntimeError, match="boom"),
        ):
            self._run_update(
                monkeypatch, tmp_path,
                run_results=[RuntimeError("boom")],  # the package step crashes
                listen_results=False,
            )

        kick.assert_called_once_with()
        log = (tmp_path / "update.log").read_text(encoding="utf-8")
        assert self.KICKSTART in log
        assert self.SKIP_PREFIX not in log

    @pytest.mark.parametrize(
        ("lifecycle", "spawn_kicks"),
        [
            ((False, "unavailable", "test: manual spawn"), 0),
            ((True, "just_installed", "test: RunAtLoad started it"), 0),
            ((True, "already_installed", "test: kickstart"), 1),
        ],
        ids=["manual-spawn", "just-installed", "native-restart"],
    )
    def test_a_run_that_restarted_the_service_gets_no_guard_kick(
        self, lifecycle, spawn_kicks, tmp_path, monkeypatch
    ):
        """Each restart path records itself, so the guard adds no second kick."""
        kick, _popen, status, _recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_successful_runs(),
            listen_results=_comes_up_after(1),
            version_answer="0.100.0",
            lifecycle=lifecycle,
        )

        assert status["overall"] == "succeeded"
        # Only the start step's own kickstart may run, never one from the guard.
        assert kick.call_count == spawn_kicks
        assert "Final guard" not in log

    # A wedged listener that IS this installation's own service (pre-mortem
    # 5): the instance manifest names a live process bound to this port. The
    # old guard restarted it, and the hands-off exit must not strand it. A
    # manifest that proves nothing keeps the hands-off exit exactly as built.

    OWN_HELD = (
        "Port 8321 is held by this installation's own service (pid live) "
        "that does not answer health; the final guard restarts it"
    )
    OWN_HELD_AFTER_A_RESTART = (
        "Port 8321 is held by this installation's own service (pid live) "
        "that does not answer health; this run restarted the service once "
        "already, so the final guard restarts it again"
    )
    SECOND_KICK = (
        "Final guard: this installation's own service still holds the port "
        "without answering health after this run's restart. Second kickstart."
    )
    OWN_ERROR_RESTARTED = (
        "port 8321 is held by this installation's own service that did not "
        "answer health; the final guard restarted it"
    )
    OWN_ERROR_NOT_RESTARTED = (
        "port 8321 is held by this installation's own service that did not "
        "answer health; the final guard could not restart it"
    )
    OWN_RESTARTED_RECOVERY = (
        "Jacked auto-update stopped safely because port 8321 is occupied by an "
        "unverified listener. That listener was this installation's own "
        "service, which did not answer health, and the final guard restarted "
        "it. Run `jacked service status` for ownership and quarantine "
        "guidance.\n"
    )

    @staticmethod
    def _publish_own_service(monkeypatch, *, port=8321, alive=True):
        """Publish a signed instance manifest where the updater reads it.

        The service conftest points CLAUDE_DIR at this test's tmp dir, so
        `default_service_paths()` names a tmp manifest. The process probe is
        faked: ``alive`` False makes the recorded process read as dead.
        """
        from jacked.service import instance_storage
        from jacked.service.instance import (
            BindIdentity,
            InstanceManifest,
            ProcessIdentity,
            current_user_identity,
            publish_manifest,
        )
        from jacked.service.lifecycle import default_service_paths
        from jacked.service.spec import SupervisorKind
        from tests.unit.service.supervisor_test_support import make_spec

        process = ProcessIdentity(
            pid=4242, creation_id="test-creation", executable="/opt/jacked/python"
        )
        paths = default_service_paths()
        manifest = InstanceManifest.create(
            spec=make_spec(SupervisorKind.MANUAL),
            process=process,
            user_id=current_user_identity(),
            machine_id="machine-test",
            bind=BindIdentity(host="127.0.0.1", port=port),
            control_address=str(paths.control),
        )
        publish_manifest(paths.manifest, manifest)

        def identity(pid):
            if not alive:
                raise ProcessLookupError(pid)
            return process

        monkeypatch.setattr(instance_storage, "process_identity", identity)

    def test_this_installations_wedged_service_gets_the_guard_restart(
        self, tmp_path, monkeypatch
    ):
        """The holder never answers health, but the manifest proves it is
        this installation's own live service on this port. The guard stays
        pending and kickstarts it once, and the recovery text says so instead
        of "No port owner was killed"."""
        self._publish_own_service(monkeypatch)

        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=None,   # the holder never answers /api/health
            clock_step=0.1,
        )

        popen.assert_not_called()
        kick.assert_called_once_with()
        assert self.OWN_HELD in log
        assert self.KICKSTART in log
        assert "Final guard: native_restart OK (test-kickstart)" in log
        assert self.SKIP_PREFIX not in log
        assert recovery.read_text(encoding="utf-8") == self.OWN_RESTARTED_RECOVERY
        # The status record says the same thing as the recovery file.
        assert status["overall"] == "failed"
        assert status["error"] == self.OWN_ERROR_RESTARTED
        assert status["recovery"] == "jacked service status"

    def test_a_guard_restart_that_fails_is_not_reported_as_done(
        self, tmp_path, monkeypatch
    ):
        """The kickstart is refused. The recovery text must not say that the
        guard restarted the service: it says the restart failed and points
        at the update log."""
        self._publish_own_service(monkeypatch)

        kick, _popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            kick_result=(False, "test-refused"),
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=None,
            clock_step=0.1,
        )

        kick.assert_called_once_with()
        assert "Final guard: native_restart FAILED (test-refused)" in log
        assert status["overall"] == "failed"
        assert status["error"] == self.OWN_ERROR_NOT_RESTARTED
        assert status["recovery"] == "jacked service status"
        body = recovery.read_text(encoding="utf-8")
        assert body.startswith(
            "Jacked auto-update stopped safely because port 8321 is occupied by "
            "an unverified listener. That listener is this installation's own "
            "service, which did not answer health, and the final guard could "
            "not restart it. "
        )
        assert "restarted it" not in body
        assert f"See {tmp_path / 'update.log'} for the reason." in body
        assert "Run `jacked service status`" in body

    @pytest.mark.parametrize("manifest", ["stale", "other_port", "unreadable"])
    def test_a_manifest_that_proves_nothing_keeps_the_hands_off_exit(
        self, manifest, tmp_path, monkeypatch
    ):
        """A dead recorded process, a manifest for another port, or a
        manifest that cannot be read is not evidence that the holder is
        ours, so nothing is restarted and the text stays as it was."""
        if manifest == "stale":
            self._publish_own_service(monkeypatch, alive=False)
        elif manifest == "other_port":
            self._publish_own_service(monkeypatch, port=8400)
        else:
            from jacked.service.lifecycle import default_service_paths

            path = default_service_paths().manifest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)

        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_healthy_runs(),
            listen_results=True,
            health_answer=None,
            clock_step=0.1,
        )

        popen.assert_not_called()
        kick.assert_not_called()
        assert self.PORT_BUSY_SKIPPED in log
        assert "this installation's own service" not in log
        assert recovery.read_text(encoding="utf-8") == self.PORT_BUSY_RECOVERY
        assert status["error"] == "port 8321 is held by an unverified listener"

    @pytest.mark.parametrize(
        ("lifecycle", "spawn_popens", "kicks"),
        [
            ((False, "unavailable", "test: manual spawn"), 1, 1),
            ((True, "already_installed", "test: kickstart"), 0, 2),
        ],
        ids=["manual-spawn", "native-restart"],
    )
    def test_an_own_service_after_a_restart_gets_a_second_kick(
        self, lifecycle, spawn_popens, kicks, tmp_path, monkeypatch
    ):
        """This run already started the new build, which never became ready.
        On the rollback tail the wedged holder is this installation's own
        service. The last tail decides: the guard kicks once more, so the
        restored build gets started, and the log says it is the second kick."""
        self._publish_own_service(monkeypatch)

        kick, popen, _status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + the rollback pair
            listen_results=lambda run: run.call_count >= 4,
            health_answer=None,
            clock_step=0.1,
            lifecycle=lifecycle,
        )

        assert popen.call_count == spawn_popens   # the new build's start
        assert kick.call_count == kicks           # the start's kick + the guard's
        assert self.OWN_HELD_AFTER_A_RESTART in log
        assert self.SECOND_KICK in log
        assert "Final guard: native_restart OK (test-kickstart)" in log
        assert self.OWN_HELD not in log
        assert self.KICKSTART not in log
        assert self.SKIP_PREFIX not in log
        assert "stopped at the service restart step" in recovery.read_text(
            encoding="utf-8"
        )

    # Wave 2 W4: only the update tail's port_busy exit recorded what the
    # guard's restart did. A rollback tail that found this installation's
    # own wedged service left the record saying the rollback stopped at the
    # restart step, and the guard's kick after it was never recorded.

    NEVER_READY_ROLLBACK = (
        "v0.100.0 never became ready on :8321; the rollback to v0.95.0 "
        "stopped at the service restart step"
    )
    REFUSED_ROLLBACK = (
        "v0.100.0 refused to start: [FAIL] ValueError: nope; the rollback to "
        "v0.95.0 stopped at the service restart step"
    )
    ROLLBACK_RECOVERY = (
        'uv tool install "claude-jacked[tray]==0.95.0" --force --refresh '
        "&& jacked install --force && jacked service start"
    )
    GUARD_RESTARTED_NOTE = (
        "This installation's own service held port 8321 and did not answer "
        "health. The final guard then restarted it. Nothing checked the "
        "service after that restart: run `jacked service status`."
    )
    GUARD_NOT_RESTARTED_NOTE = (
        "This installation's own service held port 8321 and did not answer "
        "health. The final guard could not restart it."
    )
    GUARD_RESTARTED_CLAUSE = (
        "the final guard then restarted this installation's own service on "
        "port 8321 (not verified)"
    )
    GUARD_NOT_RESTARTED_CLAUSE = (
        "the final guard could not restart this installation's own service "
        "on port 8321"
    )

    def _rollback_record(self, tmp_path, failure, note):
        """The recovery file of a rollback failure, with the guard's note
        after the failure."""
        return (
            f"Jacked auto-update failed: {failure}\n\n"
            f"{note}\n\n"
            f"See {tmp_path / 'update.log'} for details.\n\n"
            "Recovery:\n"
            f"  {self.ROLLBACK_RECOVERY}\n"
        )

    @pytest.mark.parametrize(
        ("kick_result", "note", "clause"),
        [
            ((True, "test-kickstart"), GUARD_RESTARTED_NOTE, GUARD_RESTARTED_CLAUSE),
            ((False, "test-refused"), GUARD_NOT_RESTARTED_NOTE, GUARD_NOT_RESTARTED_CLAUSE),
        ],
        ids=["restarted", "could-not-restart"],
    )
    def test_a_rollback_tail_records_what_the_second_kick_did(
        self, kick_result, note, clause, tmp_path, monkeypatch
    ):
        """The new build never became ready, the rollback ran, and its tail
        found this installation's own service wedged on the port. The guard
        kicks a second time, and the record keeps the update failure first,
        then says what that kick did. A kick that worked is never called a
        verified restore."""
        self._publish_own_service(monkeypatch)

        kick, _popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            kick_result=kick_result,
            run_results=_healthy_runs() + [_ok(), _ok()],  # + the rollback pair
            listen_results=lambda run: run.call_count >= 4,
            health_answer=None,
            clock_step=0.1,
        )

        kick.assert_called_once_with()     # the manual spawn ran, then the guard
        assert self.OWN_HELD_AFTER_A_RESTART in log
        assert self.SECOND_KICK in log
        body = recovery.read_text(encoding="utf-8")
        assert body == self._rollback_record(tmp_path, self.NEVER_READY_ROLLBACK, note)
        assert "rolled back to" not in body
        assert status["overall"] == "failed"
        assert status["error"] == f"{self.NEVER_READY_ROLLBACK}; {clause}"
        assert status["recovery"] == self.ROLLBACK_RECOVERY

    def test_a_refused_preflight_tail_records_the_guard_restart(
        self, tmp_path, monkeypatch
    ):
        """The same record when the guard is still pending: the preflight
        refused, nothing was started, and the rollback tail found this
        installation's own wedged service. The guard's first kick is the
        restart that the record names."""
        self._publish_own_service(monkeypatch)

        kick, popen, status, recovery, log = self._drive(
            monkeypatch, tmp_path,
            run_results=[
                _ok(),                                        # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),   # preflight refuses
                _ok(),                                        # rollback install
                _ok(),                                        # jacked install --force
            ],
            listen_results=True,
            health_answer=None,
            clock_step=0.1,
        )

        popen.assert_not_called()
        kick.assert_called_once_with()
        assert self.OWN_HELD in log
        assert self.KICKSTART in log
        body = recovery.read_text(encoding="utf-8")
        assert body == self._rollback_record(
            tmp_path, self.REFUSED_ROLLBACK, self.GUARD_RESTARTED_NOTE
        )
        assert status["error"] == (
            f"{self.REFUSED_ROLLBACK}; {self.GUARD_RESTARTED_CLAUSE}"
        )
        assert status["recovery"] == self.ROLLBACK_RECOVERY


class TestTrayUpdaterNeverDowngrades:
    """#295i: the tray updater never installs an older version than the one
    on disk. The shared guard (jacked.upgrade_guard) refuses before the
    install, and the install command carries a version floor."""

    REFUSAL = (
        "Nothing to upgrade. The package index serves v0.111.1. "
        "The installed version is v0.111.2."
    )

    @staticmethod
    def _setup(monkeypatch, installable):
        from jacked import upgrade_guard
        from jacked.service import updater

        monkeypatch.setattr("jacked.install_method.can_auto_upgrade", lambda: (True, ""))
        monkeypatch.setattr("jacked.install_method.detect_install_method", lambda: "uv")
        monkeypatch.setattr(upgrade_guard, "installed_version", lambda: "0.111.2")
        monkeypatch.setattr(
            upgrade_guard,
            "probe_index_versions",
            lambda timeout=5.0: upgrade_guard.IndexVersions("0.111.2", installable),
        )
        monkeypatch.setattr(updater, "wait_for_exit", lambda pid, timeout=0: True)

    def test_pin_295i_an_older_index_installs_nothing_and_restores_the_service(
        self, monkeypatch
    ):
        from jacked.service import update_status as us
        from jacked.service import updater

        self._setup(monkeypatch, installable="0.111.1")
        ran = []
        monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: ran.append(a))
        restarts = []
        monkeypatch.setattr(
            "jacked.service.platform.native_restart",
            lambda: restarts.append(1) or (True, "stubbed"),
        )

        # The tray writes the status record before it spawns the updater.
        us.init_status(us.UPDATE_STATUS_FILE, from_version="0.111.2",
                       to_version="0.111.3", method="uv", preinit=True)

        updater.run_update(4242, "tray", target_version="0.111.3", port=8321)

        assert ran == []
        assert f"REFUSED: {self.REFUSAL}" in updater.UPDATE_LOG.read_text()
        # UG-1: nothing failed, so the next tray start shows no
        # "Jacked Update Failed Earlier" toast.
        assert not updater.RECOVERY_FILE.exists()
        data = us.read_status(us.UPDATE_STATUS_FILE)
        assert data["overall"] == "succeeded"
        assert data["to_version"] == "0.111.2"
        assert data["note"] == self.REFUSAL
        assert "preinit" not in data and not data.get("error")
        # The tray stopped the service for the update: the refusal starts it again.
        assert restarts == [1]

    def test_a_no_op_leaves_an_earlier_closed_record_and_recovery_file_alone(
        self, monkeypatch
    ):
        """Only the record of THIS click (the tray pre-init) closes as a no-op.
        A record and a recovery file from an earlier real failure stay."""
        from jacked.service import update_status as us
        from jacked.service import updater

        self._setup(monkeypatch, installable="0.111.1")
        monkeypatch.setattr(
            "jacked.service.platform.native_restart", lambda: (True, "stubbed")
        )
        us.init_status(us.UPDATE_STATUS_FILE, from_version="0.111.0",
                       to_version="0.111.2", method="uv")
        us.mark_failed(us.UPDATE_STATUS_FILE, error="an earlier failure")
        updater.RECOVERY_FILE.write_text("an earlier failure\n")

        updater.run_update(4242, "tray", target_version="0.111.3", port=8321)

        data = us.read_status(us.UPDATE_STATUS_FILE)
        assert data["overall"] == "failed" and data["error"] == "an earlier failure"
        assert updater.RECOVERY_FILE.read_text() == "an earlier failure\n"

    def test_same_version_reinstalls_with_the_floor(self, monkeypatch):
        from jacked.service import updater

        self._setup(monkeypatch, installable="0.111.2")
        ran = []

        def _run(cmd, *a, **k):
            ran.append(cmd)
            return _fail(1)  # stop after the install step

        monkeypatch.setattr(updater.subprocess, "run", _run)
        monkeypatch.setattr(updater, "find_bin", lambda name: f"/fake/{name}")

        updater.run_update(4242, "tray", target_version="0.111.2", port=8321)

        assert ran[0] == [
            "/fake/uv", "tool", "install", "claude-jacked[tray]>=0.111.2", "--force", "--refresh",
        ]

    def test_the_windows_tray_batch_carries_the_floor(self, monkeypatch):
        from jacked.service import updater

        self._setup(monkeypatch, installable="0.111.2")
        captured = {}

        class _Popen:
            def __init__(self, args, **kwargs):
                captured["args"] = args

        with (
            patch.object(updater.subprocess, "Popen", _Popen),
            patch("jacked.findbin.find_bin", side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n)),
            patch("sys.platform", "win32"),
        ):
            updater._spawn_windows_tray_updater(4242, "tray", target_version="0.111.2", port=8321)
        from pathlib import Path as _P

        body = _P(captured["args"][-1]).read_text(encoding="utf-8")
        assert '"claude-jacked[tray]>=0.111.2"' in body
