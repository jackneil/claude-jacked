"""Tests for `jacked upgrade` one-shot upgrade command."""

from unittest.mock import patch, MagicMock
from click.testing import CliRunner


def _ok(stdout: str = "") -> MagicMock:
    """A successful subprocess result with real (not MagicMock) text streams.

    The preflight step reads `.stdout`/`.stderr` and concatenates them, so a
    bare MagicMock leaks a repr into the console output.
    """
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(returncode: int = 1, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestUpgradeRefusal:
    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "This is an editable (dev-clone) install — auto-update disabled. Upgrade manually from the repo: `cd <repo> && git pull && uv sync`."),
    )
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_refuses_editable(self, mock_run, mock_popen, mock_gate):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 2
        assert "editable" in result.output.lower()
        assert "git pull" in result.output
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "pip install detected — auto-update disabled. Migrate with: `uv tool install \"claude-jacked[tray]\"`."),
    )
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_refuses_pip(self, mock_run, mock_popen, mock_gate):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 2
        assert "pip" in result.output.lower()
        mock_run.assert_not_called()
        mock_popen.assert_not_called()


class TestUpgradeCommand:
    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_delegates_restart_to_new_cli_when_not_running(
        self, mock_run, mock_popen, mock_read_pid, mock_find, mock_method,
    ):
        """The newly installed CLI owns all service restart decisions."""
        from jacked.cli import main

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # Four blocking calls: package install, preflight (the transaction
        # gate), settings migration, v2 restart.
        assert mock_run.call_count == 4
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "claude-jacked[tray]" in uv_args
        preflight_args = mock_run.call_args_list[1][0][0]
        assert preflight_args == ["/fake/jacked", "service", "preflight"]
        install_args = mock_run.call_args_list[2][0][0]
        assert "/fake/jacked" in install_args
        assert "install" in install_args

        restart_args = mock_run.call_args_list[3][0][0]
        assert restart_args == ["/fake/jacked", "service", "restart"]
        mock_popen.assert_not_called()

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_preflight_runs_before_the_settings_migration(
        self, mock_run, mock_popen, mock_read_pid, mock_find, mock_method,
    ):
        """The gate must close before anything replaces the old install.

        `jacked install --force` rewrites settings.json and the native service
        definition. If preflight ran after it, a refused build would already
        have taken the old service down.
        """
        from jacked.cli import main

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()

        CliRunner().invoke(main, ["upgrade"])

        verbs = [call[0][0][1:3] for call in mock_run.call_args_list[1:]]
        assert verbs.index(["service", "preflight"]) < verbs.index(
            ["install", "--force"]
        )

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.run")
    def test_upgrade_aborts_if_uv_not_found_when_method_is_uv(
        self, mock_run, mock_find, mock_method,
    ):
        """uv-install → we must fail fast if uv itself is missing."""
        from jacked.cli import main
        mock_find.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code != 0
        assert "uv" in result.output.lower()
        mock_run.assert_not_called()

    # NOTE: pre-0.41.19 had a test that `jacked upgrade` used pip when
    # detect_install_method returned 'pip'. That behavior is gone — the
    # gate now refuses pip and editable installs. See TestUpgradeRefusal
    # below for the current pip-path contract.

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.run")
    def test_upgrade_aborts_if_uv_install_fails(self, mock_run, mock_find, mock_method):
        """Inline (POSIX) path: a failed `uv install` aborts with exit 1.

        Pinned to darwin because on Windows `jacked upgrade` delegates to a
        detached cmd.exe helper and returns 0 — the returncode-1 abort lives
        only in the inline path.
        """
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": "/fake/uv"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 1
        # Only one subprocess call — aborts after package upgrade fails
        assert mock_run.call_count == 1

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.read_pid", return_value={"pid": 99999, "port": 8321})
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_v2_service_uses_new_cli_restart(
        self, mock_run, mock_popen, mock_read_pid, mock_find, mock_method, tmp_path,
    ):
        """A v2 manifest delegates the authenticated handoff to the new CLI."""
        from jacked.cli import main

        manifest = tmp_path / "instance.json"
        manifest.write_text("{}", encoding="utf-8")
        paths = MagicMock(manifest=manifest)
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()

        with (
            patch(
                "jacked.service.lifecycle.default_service_paths",
                return_value=paths,
            ),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
        ):
            result = CliRunner().invoke(main, ["upgrade"])

        assert result.exit_code == 0, result.output
        assert mock_run.call_args_list[3][0][0] == [
            "/fake/jacked", "service", "restart"
        ]
        stop.assert_not_called()
        remove.assert_not_called()
        mock_popen.assert_not_called()

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_live_legacy_pid_reports_manual_handoff_without_signal(
        self, mock_run, mock_popen, mock_find, mock_method, tmp_path,
    ):
        """Legacy evidence never authorizes a signal or a competing tray."""
        from jacked.cli import main

        paths = MagicMock(manifest=tmp_path / "missing-instance.json")
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()

        with (
            patch(
                "jacked.service.lifecycle.default_service_paths",
                return_value=paths,
            ),
            patch(
                "jacked.service.legacy.resolve_active_legacy_service",
                return_value=MagicMock(pid=99999, port=8321),
            ) as resolve_legacy,
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
        ):
            result = CliRunner().invoke(main, ["upgrade"])

        assert result.exit_code == 0, result.output
        assert "upgrade complete" in result.output.lower()
        assert "quit the old tray" in result.output.lower()
        assert "service start" in result.output.lower()
        # Package install, preflight, settings migration. No restart: the
        # legacy tray cannot be authenticated, so nothing is signalled.
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[1][0][0] == [
            "/fake/jacked", "service", "preflight"
        ]
        resolve_legacy.assert_called_once()
        stop.assert_not_called()
        remove.assert_not_called()
        mock_popen.assert_not_called()

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=True)
    @patch("jacked.service.process.read_pid", return_value={"pid": 99999, "port": 8321})
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_skip_service_flag_honored(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find, mock_method,
    ):
        from jacked.cli import main
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = _ok()

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade", "--skip-service"])

        assert result.exit_code == 0
        # Package install, preflight, settings migration. The service is
        # untouched, and no detached Popen is spawned either. Preflight still
        # runs: it changes no process, and it is the whole point of the gate.
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[1][0][0] == [
            "/fake/jacked", "service", "preflight"
        ]
        mock_popen.assert_not_called()

    @patch("sys.platform", "darwin")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=False)
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_continues_if_jacked_install_fails(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find, mock_method,
    ):
        """jacked install failure is non-fatal — package is still upgraded."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        # uv install succeeds, preflight passes, jacked install fails.
        mock_run.side_effect = [
            _ok(),
            _ok("[OK] Service contract OK"),
            _fail(1),
            _ok(),
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # Migration remains non-fatal; the new CLI still owns the v2 restart.
        # A failed migration is NOT a rollback trigger: the new build already
        # proved it can provision its service contract.
        assert mock_run.call_count == 4
        assert mock_run.call_args_list[3][0][0] == [
            "/fake/jacked", "service", "restart"
        ]
        mock_popen.assert_not_called()


class TestUpgradeIsATransaction:
    """`jacked upgrade` must never leave the machine without a service.

    Every test here patches subprocess.run and subprocess.Popen, so no real
    package install, preflight or service restart can escape the test process.
    """

    RB_ARGV = [
        "/fake/uv", "tool", "install",
        "claude-jacked[tray]==0.95.0", "--force", "--refresh",
    ]

    @staticmethod
    def _patches():
        """Common patch stack: uv install method, resolvable binaries."""
        return (
            patch("sys.platform", "darwin"),
            patch(
                "jacked.install_method.detect_install_method", return_value="uv"
            ),
            patch(
                "jacked.findbin.find_bin",
                side_effect=lambda name: {
                    "uv": "/fake/uv",
                    "jacked": "/fake/jacked",
                }.get(name),
            ),
            patch("jacked.service.process.read_pid", return_value=None),
        )

    def _invoke(self, run_results, args=(), version="0.95.0", tmp_path=None):
        """Run `jacked upgrade` with a scripted subprocess.run sequence."""
        from jacked.cli import main

        recovery = (tmp_path / "jacked-update-failed.txt") if tmp_path else None
        stack = self._patches()
        with (
            stack[0], stack[1], stack[2], stack[3],
            patch("subprocess.Popen") as popen,
            patch("subprocess.run") as run,
            patch("jacked.__version__", version),
            patch("jacked.cli._installed_package_version", return_value="0.100.0"),
        ):
            run.side_effect = list(run_results)
            if recovery is not None:
                with patch("jacked.service.updater.RECOVERY_FILE", recovery):
                    result = CliRunner().invoke(main, ["upgrade", *args])
            else:
                result = CliRunner().invoke(main, ["upgrade", *args])
        return result, run, popen

    def test_refused_preflight_rolls_back_to_the_previous_version(self, tmp_path):
        """The whole point: a build that cannot boot never keeps the machine."""
        result, run, popen = self._invoke(
            [
                _ok(),                                   # uv install
                _fail(1, stderr="[FAIL] ValueError: runtime_path untrusted"),
                _ok(),                                   # rollback: uv install ==0.95.0
                _ok(),                                   # jacked install --force
                _ok(),                                   # jacked service restart
            ],
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1, result.output
        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[1] == ["/fake/jacked", "service", "preflight"]
        assert argvs[2] == self.RB_ARGV
        assert argvs[3] == ["/fake/jacked", "install", "--force"]
        assert argvs[4] == ["/fake/jacked", "service", "restart"]
        # The migration for the NEW build never ran: the gate closed first.
        assert argvs.count(["/fake/jacked", "install", "--force"]) == 1
        assert "rolling back" in result.output.lower()
        assert "0.95.0" in result.output
        popen.assert_not_called()

    def test_refused_preflight_writes_the_recovery_file(self, tmp_path):
        recovery = tmp_path / "jacked-update-failed.txt"
        result, _run, _popen = self._invoke(
            [_ok(), _fail(1, stderr="[FAIL] ValueError: nope"), _ok(), _ok(), _ok()],
            tmp_path=tmp_path,
        )
        assert result.exit_code == 1
        body = recovery.read_text(encoding="utf-8")
        assert "0.100.0" in body       # the build that refused
        assert "0.95.0" in body        # the build that was restored
        assert "jacked install --force" in body

    def test_failed_restart_also_rolls_back(self, tmp_path):
        """A build can provision its contract and still fail to come up."""
        result, run, _popen = self._invoke(
            [
                _ok(),          # uv install
                _ok("[OK] Service contract OK"),
                _ok(),          # jacked install --force
                _fail(3),       # jacked service restart
                _ok(),          # rollback: uv install ==0.95.0
                _ok(),          # jacked install --force
                _ok(),          # jacked service restart
            ],
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1, result.output
        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[4] == self.RB_ARGV
        assert argvs[6] == ["/fake/jacked", "service", "restart"]
        assert "did not restart" in result.output.lower()

    def test_no_rollback_flag_keeps_the_new_version(self, tmp_path):
        recovery = tmp_path / "jacked-update-failed.txt"
        result, run, _popen = self._invoke(
            [_ok(), _fail(1, stderr="[FAIL] ValueError: nope")],
            args=("--no-rollback",),
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1, result.output
        argvs = [call[0][0] for call in run.call_args_list]
        assert self.RB_ARGV not in argvs
        assert len(argvs) == 2  # install + preflight, then it stops
        assert "--no-rollback" in result.output
        assert "no-rollback" in recovery.read_text(encoding="utf-8")

    def test_skip_service_still_rolls_back_but_never_restarts(self, tmp_path):
        """--skip-service suppresses the restart, not the transaction."""
        result, run, _popen = self._invoke(
            [
                _ok(),                                   # uv install
                _fail(1, stderr="[FAIL] ValueError: nope"),
                _ok(),                                   # rollback install
                _ok(),                                   # jacked install --force
            ],
            args=("--skip-service",),
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1, result.output
        argvs = [call[0][0] for call in run.call_args_list]
        assert argvs[2] == self.RB_ARGV
        assert ["/fake/jacked", "service", "restart"] not in argvs

    def test_a_failed_rollback_command_still_reports_and_exits_nonzero(
        self, tmp_path
    ):
        recovery = tmp_path / "jacked-update-failed.txt"
        result, run, _popen = self._invoke(
            [
                _ok(),
                _fail(1, stderr="[FAIL] ValueError: nope"),
                _fail(2),   # the rollback install itself fails
            ],
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1, result.output
        argvs = [call[0][0] for call in run.call_args_list]
        # It stops after the failed rollback rather than reinstalling blindly.
        assert argvs[-1] == self.RB_ARGV
        body = recovery.read_text(encoding="utf-8")
        assert "rollback" in body.lower()
        assert "claude-jacked[tray]==0.95.0" in body


class TestUpgradeWindows:
    @patch("sys.platform", "win32")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_spawns_detached_helper_and_exits(
        self, mock_popen, mock_find, mock_method,
    ):
        """Windows must spawn a detached cmd.exe helper, never try inline."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "cmd.exe" in args[0]
        assert args[1] == "/c"
        assert args[2].endswith(".bat")
        kwargs = mock_popen.call_args[1]
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x08000000  # CREATE_NO_WINDOW (hidden console, no flashing windows)
        assert not (flags & 0x00000008)  # never DETACHED_PROCESS — that's what popped the windows

    @patch("sys.platform", "win32")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_batch_contains_uv_and_jacked_commands(
        self, mock_popen, mock_find, mock_method, tmp_path, monkeypatch,
    ):
        """Batch file must embed the full upgrade sequence."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        runner = CliRunner()
        runner.invoke(main, ["upgrade", "--extras", "all"])

        assert len(created) == 1
        batch = open(created[0]).read()
        assert "tasklist" in batch  # waits for parent exit
        assert "uv.exe" in batch
        assert "claude-jacked[all]" in batch
        assert "--force" in batch
        assert "jacked install --force" in batch
        assert "service restart" in batch

    # test_windows_batch_uses_pip_user_when_method_is_pip: removed in 0.41.19
    # — pip installs are refused by the pre-flight gate, not auto-upgraded.

    @patch("sys.platform", "win32")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_skip_service_flag(
        self, mock_popen, mock_find, mock_method, tmp_path, monkeypatch,
    ):
        """--skip-service should result in SKIP_SERVICE=1 in the batch."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        runner = CliRunner()
        runner.invoke(main, ["upgrade", "--skip-service"])

        batch = open(created[0]).read()
        assert "set SKIP_SERVICE=1" in batch

    @patch("sys.platform", "win32")
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_batch_wait_loop_is_bounded(
        self, mock_popen, mock_find, mock_method, tmp_path, monkeypatch,
    ):
        """Parent-wait loop must be bounded — an unbounded `find <pid>` poll
        spins forever once the dead PID is reused (the original bug)."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        CliRunner().invoke(main, ["upgrade"])

        batch = open(created[0]).read()
        # bounded loop markers present
        assert "JACKED_WAITED" in batch
        assert ":waitdone" in batch
        assert "GEQ 120" in batch
        assert "goto wait" in batch  # the loop itself still exists
        # the old unbounded form is gone
        assert "if not errorlevel 1" not in batch
