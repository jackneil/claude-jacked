"""Tests for the auto-updater."""

import itertools
import os
import subprocess
import sys
from unittest.mock import patch, MagicMock


def _ok(stdout: str = "") -> MagicMock:
    """Successful subprocess result with real text streams.

    The preflight step concatenates `.stdout` and `.stderr`, so a bare
    MagicMock would put a repr into the update log.
    """
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_order_wait_install_migrate_restart(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
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
        # Port-wait: free. Verification: False = the port is bound, so the new
        # service came up and no rollback is owed.
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True) as mock_wait:
            updater.run_update(parent_pid=12345, extras="tray")

        assert mock_wait.called
        # uv install -> service preflight -> jacked install --force.
        assert mock_run.call_count == 3
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "tool" in uv_args and "install" in uv_args
        assert "claude-jacked[tray]" in uv_args
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
        assert mock_restart.called, \
            "Finally-guard must call native_restart when upgrade bails early"
        # Log should record the guard firing.
        assert "Final guard" in (tmp_path / "update.log").read_text()

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
    @patch("jacked.service.updater.is_port_available")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_polls_port_before_spawning_service(
        self, mock_popen, mock_run, mock_port_avail, mock_find,
        mock_method, mock_gate, mock_ensure,
    ):
        """After uv+jacked install, must wait for port 8321 before `service start`."""
        from jacked.service import updater

        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()

        # Port-wait: busy twice, then free (loop check + post-loop confirm).
        # Verification: False = the port is bound, so the new service came up
        # and no rollback is owed.
        mock_port_avail.side_effect = [False, False, True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_called_once()
        # is_port_available polled more than once (proves we looped)
        assert mock_port_avail.call_count >= 3


class TestParentKillEscalation:
    """A reusable integer PID is never enough evidence to kill a process."""

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater._force_kill_pid")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_refuses_pid_only_parent_kill_when_it_wont_exit(
        self, mock_popen, mock_run, mock_find, mock_force_kill, mock_port_avail, mock_gate,
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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_refuses_to_kill_port_squatter(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_force_kill, mock_port_pids, mock_gate,
        mock_method, mock_ensure,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.return_value = False

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_port_pids.assert_not_called()
        mock_force_kill.assert_not_called()
        mock_popen.assert_not_called()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.time.sleep", lambda _s: None)
    @patch("jacked.service.updater._pids_bound_to_port", return_value=[])
    @patch("jacked.service.updater.is_port_available", return_value=False)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_aborts_when_port_cannot_be_freed(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_port_pids,
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

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_not_called()
        assert (tmp_path / "recovery.txt").exists()
        assert "port 8321" in (tmp_path / "recovery.txt").read_text().lower()


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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_recovery_file_written_when_new_service_never_binds(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_clock,
        mock_gate, mock_method, mock_ensure,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        # Port-wait: True (free). Then verification phase: always True (never bound).
        mock_port_avail.return_value = True

        with patch.object(updater, "wait_for_exit", return_value=True):
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
    """Port oracle: nothing binds until the rollback ran, then the old build does.

    `is_port_available` is True when the port is FREE. The restored build's
    start-and-verify tail first waits for the port to be free (two reads), then
    polls until it is taken - so the oracle answers free, free, then bound.
    """
    state = {"after": 0}

    def oracle(run):
        if run.call_count < after_run_calls:
            return True
        state["after"] += 1
        return state["after"] <= 2

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
    def _run_update(monkeypatch, tmp_path, run_results, port_results):
        """Drive run_update with scripted subprocess and port outcomes."""
        from jacked.service import updater, update_status as us_mod

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")

        with (
            patch("jacked.service.platform.ensure_native_lifecycle",
                  return_value=(False, "unavailable", "test: manual spawn")),
            patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")),
            patch("jacked.install_method.detect_install_method", return_value="uv"),
            patch("jacked.service.updater.time.sleep", lambda _s: None),
            # A fake clock: sleep is a no-op, so a real 20s verify deadline
            # would busy-spin for 20 real seconds per attempt.
            patch("jacked.service.updater.time.monotonic",
                  side_effect=itertools.count(0.0, 5.0)),
            patch("jacked.service.updater.is_port_available") as port,
            patch("jacked.service.updater.find_bin") as find,
            patch("subprocess.run") as run,
            patch("subprocess.Popen") as popen,
            patch("jacked.__version__", "0.95.0"),
            patch.object(updater, "wait_for_exit", return_value=True),
        ):
            find.side_effect = lambda name: {
                "uv": "/fake/uv", "jacked": "/fake/jacked",
            }.get(name)
            run.side_effect = list(run_results)
            if isinstance(port_results, bool):
                # The verify loop spins with sleep patched out, so a finite
                # side_effect list would run dry mid-poll.
                port.return_value = port_results
            elif callable(port_results):
                # An oracle reading the subprocess mock, for scripts where the
                # port answer must change once the rollback has run.
                port.side_effect = lambda *_a, **_k: port_results(run)
            else:
                port.side_effect = list(port_results)
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
            # Port free for the wait, then bound so the restored build verifies.
            port_results=[True, True] + [False] * 100,
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
            # The new build never binds. After the rollback the port reads
            # free for the wait phase, then bound - the restored build is up.
            port_results=_restored_build_binds(after_run_calls=4),
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
            port_results=True,
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
            port_results=[True, True] + [False] * 100,
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
            ],
            port_results=[True, True] + [False] * 100,
        )

        assert status["overall"] == "succeeded"
        assert "rolling_back" not in {p["name"] for p in status["phases"]}
        assert not recovery.exists()
        argvs = [call[0][0] for call in run.call_args_list]
        assert not [a for a in argvs if a[1:] == self.RB_ARGV_TAIL]
        popen.assert_called_once()


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
            port_results=[True, True] + [False] * 100,
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
            # The port stays free forever: nothing ever comes up.
            port_results=True,
        )

        assert status["overall"] == "failed"
        body = recovery.read_text(encoding="utf-8")
        assert "stopped at the service restart step" in body
        assert "rolled back to v0.95.0" not in body


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
            patch("jacked.service.updater.is_port_available",
                  side_effect=[True, True] + [False] * 100),
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
            port_results=[True, True] + [False] * 100,
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
        assert args[0] == "cmd.exe"
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
        assert "-m" in args
        assert "jacked.service.updater" in args


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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_succeeded_status_with_all_phases(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_gate, mock_method,
        mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        from jacked.service.update_phases import PHASE_NAMES
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()

        # Port-wait phase: True (port is free, break loop) — may be called
        # twice (loop check + post-loop confirmation). Verify phase: False
        # (port is bound = service came up).
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
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
        assert args[0] == "cmd.exe"
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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_reuses_tray_pre_init_and_completes(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        mock_port_avail.side_effect = [True, True] + [False] * 100

        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="0.41.19",
            to_version="0.41.20",
            method="uv",
            preinit=True,
        )

        with patch.object(updater, "wait_for_exit", return_value=True):
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
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_jacked_missing_after_install_writes_failed(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_mark_succeeded_exception_degrades_to_failed(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = _ok()
        mock_port_avail.side_effect = [True, True] + [False] * 100

        def boom(*_args, **_kw):
            raise OSError("disk full")
        monkeypatch.setattr(us_mod, "mark_succeeded", boom)

        with patch.object(updater, "wait_for_exit", return_value=True):
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
                assert candidate.startswith("if errorlevel"), (
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
            monkeypatch, tmp_path, run_results, port_results=True
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
            monkeypatch, tmp_path, [_ok(), _ok(), _ok()], port_results=False
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

