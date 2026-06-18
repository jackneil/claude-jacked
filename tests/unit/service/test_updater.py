"""Tests for the auto-updater."""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock


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
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_order_wait_install_migrate_restart(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_method, mock_gate, mock_ensure,
    ):
        """Verify: wait_for_exit -> uv install -> jacked install -> jacked service start.

        Forces ensure_native_lifecycle to 'unavailable' so the updater falls
        through to the manual Popen(jacked service start) path exercised here."""
        from jacked.service import updater

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", return_value=True) as mock_wait:
            updater.run_update(parent_pid=12345, extras="tray")

        assert mock_wait.called
        assert mock_run.call_count == 2
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "tool" in uv_args and "install" in uv_args
        assert "claude-jacked[tray]" in uv_args
        assert "--force" in uv_args

        jacked_install_args = mock_run.call_args_list[1][0][0]
        assert "/fake/jacked" in jacked_install_args
        assert "install" in jacked_install_args
        assert "--force" in jacked_install_args

        assert mock_popen.call_count == 1
        restart_args = mock_popen.call_args_list[0][0][0]
        assert "/fake/jacked" in restart_args
        assert "service" in restart_args and "start" in restart_args

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
        """When the upgrade phase exits early (e.g. install --force fails),
        the finally-guard MUST attempt native_restart so the tray comes back.
        Regression test for the SameFileError-leaves-tray-dead bug (v0.45.0)."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        # First subprocess.run (uv tool install) succeeds; second (jacked install) fails.
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=1)]

        # Track whether native_restart was called from the finally-guard.
        with patch("jacked.service.platform.native_restart", return_value=(True, "test-kickstart")) as mock_restart, \
             patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        # Two subprocess.run calls happened (uv + jacked install), and neither
        # success-path restart nor Popen fallback ran (install bailed first).
        # The finally-guard SHOULD have called native_restart exactly once.
        assert mock_restart.called, \
            "Finally-guard must call native_restart when upgrade bails early"
        # Log should record the guard firing.
        assert "Final guard" in (tmp_path / "update.log").read_text()

    # Removed in 0.41.19: pip installs are refused by the gate, not auto-upgraded.


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
        mock_run.return_value = MagicMock(returncode=0)

        # Return False 2 times (port busy), then always True.
        call_counter = {"n": 0}
        def port_result(*_args, **_kw):
            call_counter["n"] += 1
            return call_counter["n"] > 2
        mock_port_avail.side_effect = port_result

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_called_once()
        # is_port_available polled more than once (proves we looped)
        assert mock_port_avail.call_count >= 3


class TestParentKillEscalation:
    """Updater must force-kill the parent tray if pystray ignores icon.stop()."""

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater._force_kill_pid")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_sigkill_parent_when_it_wont_exit(
        self, mock_popen, mock_run, mock_find, mock_force_kill, mock_port_avail, mock_gate,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", side_effect=[False, True]):
            updater.run_update(parent_pid=99999, extras="tray")

        mock_force_kill.assert_called_once_with(99999)


class TestPortStuckRecovery:
    """If port is still bound after the wait loop, force-kill the squatter."""

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
    def test_force_kills_port_squatter(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_force_kill, mock_port_pids, mock_gate,
        mock_method, mock_ensure,
    ):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        # Port-wait loop sees "stuck" (False). After the force-kill grace
        # loop, port becomes free (True), and verification also sees True.
        call_state = {"kill_done": False}
        def port_result(*_args, **_kw):
            if mock_force_kill.called:
                call_state["kill_done"] = True
            return call_state["kill_done"]
        mock_port_avail.side_effect = port_result

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_force_kill.assert_called_with(54321)
        mock_popen.assert_called_once()

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
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_recovery_file_written_when_new_service_never_binds(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, mock_ensure,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        # Port-wait: True (free). Then verification phase: always True (never bound).
        mock_port_avail.return_value = True

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_called_once()
        assert (tmp_path / "recovery.txt").exists()
        assert "never came up" in (tmp_path / "recovery.txt").read_text()


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
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_skips_restart_if_jacked_install_fails(
        self, mock_popen, mock_run, mock_find, mock_method, mock_gate, tmp_path, monkeypatch,
    ):
        """Partial migration must NOT silently restart with broken settings."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        # uv install succeeds, jacked install fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_not_called()
        assert (tmp_path / "recovery.txt").exists()
        content = (tmp_path / "recovery.txt").read_text()
        assert "jacked install --force" in content


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
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_succeeded_status_with_all_phases(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_gate, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        from jacked.service.update_phases import PHASE_NAMES
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)

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
        for expected in PHASE_NAMES:
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

            assert 'start "" "http://' in body
            assert "/update.html" in body
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
            assert "127.0.0.1:9000/update.html" in body
            assert "127.0.0.1:9000/api/version" in body
            assert "bind :9000" in body
        finally:
            import os as _os
            try:
                _os.unlink(mock_popen.call_args[0][0][2])
            except OSError:
                pass


class TestRunUpdateReusesTrayPreInit:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_reuses_tray_pre_init_and_completes(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="0.41.19",
            to_version="0.41.20",
            method="uv",
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


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_mark_succeeded_exception_degrades_to_failed(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
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
