"""Tests for jacked.service.platform module."""

from unittest.mock import MagicMock, patch


class TestGenerateLaunchdPlist:
    def test_contains_label(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "ai.hank.jacked" in plist

    def test_contains_binary_path(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/opt/bin/jacked", port=8321)
        assert "/opt/bin/jacked" in plist

    def test_contains_run_at_load(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "<key>RunAtLoad</key>" in plist
        assert "<true/>" in plist

    def test_contains_keep_alive(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "<key>KeepAlive</key>" in plist

    def test_keep_alive_scoped_to_crash_only(self):
        """KeepAlive should only restart on crash, not on clean user stop."""
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "<key>SuccessfulExit</key>" in plist
        # SuccessfulExit false means "don't restart after successful exit"

    def test_custom_port_in_args(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=9000)
        assert "9000" in plist

    def test_log_path_in_plist(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "jacked-service.log" in plist

    def test_no_baked_host(self):
        """The plist must be host-free: the bind host lives in the settings DB
        and is resolved at every boot, so the GUI Remote access toggle survives
        reboots and upgrades."""
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", port=8321)
        assert "--host" not in plist
        assert "--port" in plist


class TestGenerateWindowsVbs:
    def test_prefers_pythonw_when_present(self, tmp_path, monkeypatch):
        # pythonw.exe next to the interpreter -> VBS launches it, not jacked.exe
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(
            "jacked.service.platform.sys.executable", str(tmp_path / "python.exe")
        )
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=8321)
        assert "pythonw.exe" in vbs
        assert "-m jacked service start" in vbs
        assert r"C:\bin\jacked.exe" not in vbs

    def test_falls_back_to_jacked_exe_without_pythonw(self, tmp_path, monkeypatch):
        # no pythonw.exe -> fall back to the jacked.exe trampoline
        monkeypatch.setattr(
            "jacked.service.platform.sys.executable", str(tmp_path / "python.exe")
        )
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=8321)
        assert r"C:\bin\jacked.exe" in vbs

    def test_hidden_window(self):
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=8321)
        assert ", 0," in vbs

    def test_custom_port_in_vbs(self):
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=9000)
        assert "--port 9000" in vbs

    def test_no_baked_host_pythonw_branch(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(
            "jacked.service.platform.sys.executable", str(tmp_path / "python.exe")
        )
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=8321)
        assert "--host" not in vbs
        assert "--port 8321" in vbs

    def test_no_baked_host_jacked_exe_branch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jacked.service.platform.sys.executable", str(tmp_path / "python.exe")
        )
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", port=8321)
        assert "--host" not in vbs
        assert "--port 8321" in vbs


class TestDetectAutostart:
    @patch("sys.platform", "darwin")
    def test_darwin_detects_plist(self, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import _generate_launchd_plist, detect_autostart

        plist.write_text(_generate_launchd_plist("/usr/local/bin/jacked"))
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
        with (
            patch("jacked.service.platform._get_windows_startup_path", return_value=vbs),
            patch(
                "jacked.service.autostart.subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ),
        ):
            assert detect_autostart() is True

    @patch("sys.platform", "win32")
    def test_win32_no_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        from jacked.service.platform import detect_autostart
        with (
            patch("jacked.service.platform._get_windows_startup_path", return_value=vbs),
            patch(
                "jacked.service.autostart.subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ),
        ):
            assert detect_autostart() is False

    @patch("sys.platform", "win32")
    def test_win32_staged_task_definition_is_not_reported_as_registered(
        self, tmp_path, monkeypatch
    ):
        import jacked.service.platform as platform_module
        from jacked.service.platform import detect_autostart

        monkeypatch.setattr(platform_module, "CLAUDE_DIR", tmp_path)
        task = (
            tmp_path
            / "jacked-service-v2"
            / "supervisors"
            / "jacked-task.xml"
        )
        task.parent.mkdir(parents=True)
        task.write_text("owned task")
        with patch(
            "jacked.service.platform._get_windows_startup_path",
            return_value=tmp_path / "missing.vbs",
        ):
            with patch(
                "jacked.service.autostart.subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ):
                assert detect_autostart() is False

    @patch("sys.platform", "linux")
    def test_linux_requires_systemd_to_report_unit_enabled(self, tmp_path):
        from jacked.service.platform import detect_autostart

        unit = tmp_path / "jacked.service"
        unit.write_text("owned unit")
        with patch(
            "jacked.service.platform._get_systemd_user_unit_path",
            return_value=unit,
        ):
            with patch(
                "jacked.service.autostart.subprocess.run",
                return_value=MagicMock(returncode=1, stdout="disabled"),
            ):
                assert detect_autostart() is False


# Realistic pre-M5 artifacts with a baked --host, for migration tests.
_LEGACY_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hank.jacked</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/jacked</string>
        <string>service</string>
        <string>start</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8321</string>
    </array>
</dict>
</plist>
"""

_LEGACY_VBS = (
    'Set WshShell = CreateObject("WScript.Shell")\n'
    'WshShell.Run """C:\\bin\\jacked.exe"" service start'
    ' --host 0.0.0.0 --port 8321", 0, False\n'
)


class TestInstallAutostart:
    @patch("jacked.service.platform._service_is_running", return_value=False)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_writes_plist_and_bootstraps(
        self, mock_run, _mock_running, tmp_path, monkeypatch,
    ):
        import os
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                result = install_autostart(8321)
        assert plist.exists()
        content = plist.read_text()
        assert "ai.hank.jacked" in content
        assert "--host" not in content
        # bootout then bootstrap (replaces the old blind `launchctl load`,
        # which silently kept a stale in-memory definition when the label
        # was already loaded).
        assert mock_run.call_count == 2
        bootout_args = mock_run.call_args_list[0][0][0]
        bootstrap_args = mock_run.call_args_list[1][0][0]
        assert bootout_args[:2] == ["launchctl", "bootout"]
        assert "gui/501/ai.hank.jacked" in bootout_args
        assert bootstrap_args[:2] == ["launchctl", "bootstrap"]
        assert "gui/501" in bootstrap_args
        assert str(plist) in bootstrap_args
        assert "running now" in result

    @patch("jacked.service.platform._service_is_running", return_value=False)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_tolerates_bootout_not_loaded(
        self, mock_run, _mock_running, tmp_path, monkeypatch,
    ):
        """`launchctl bootout` on an unloaded label fails with a "Boot-out
        failed: ... not loaded" style error — that is the NORMAL first-install
        case and must not stop the bootstrap."""
        import os
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_run.side_effect = [
            MagicMock(returncode=3, stdout="",
                      stderr="Boot-out failed: 3: No such process (not loaded)"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                result = install_autostart(8321)
        assert plist.exists()
        assert mock_run.call_count == 2  # bootstrap still ran
        assert mock_run.call_args_list[1][0][0][:2] == ["launchctl", "bootstrap"]
        assert "running now" in result

    @patch("jacked.service.platform._service_is_running", return_value=False)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_bootstrap_failure_returns_honest_status(
        self, mock_run, _mock_running, tmp_path, monkeypatch,
    ):
        """When `launchctl bootstrap` FAILS the job never loaded and the bind
        the user set never took effect — the return string must NOT claim
        "running now", or the CLI prints a false success on a silent boot-time
        failure (the observability trap this fixes)."""
        import os
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # bootout ok
            MagicMock(returncode=5, stdout="",
                      stderr="Bootstrap failed: 5: Input/output error"),
        ]
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                result = install_autostart(8321)
        assert plist.exists()
        assert "running now" not in result
        assert "did not start" in result
        assert "5" in result  # surfaces the exit code for triage

    @patch("jacked.service.platform._service_is_running", return_value=True)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_running_service_rewrites_file_only(
        self, mock_run, _mock_running, tmp_path,
    ):
        """`jacked install --force` runs inside BOTH upgrade paths while the
        service is up — a bootout there would kill it mid-upgrade. Running
        service: rewrite the plist FILE only, no launchctl at all."""
        plist = tmp_path / "ai.hank.jacked.plist"
        plist.write_text(_LEGACY_PLIST, encoding="utf-8")
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                with patch(
                    "jacked.service.migrate.migrate_baked_host_to_db",
                    return_value="migrated",
                ):
                    result = install_autostart(8321)
        assert "--host" not in plist.read_text()
        mock_run.assert_not_called()  # no bootout, no bootstrap, no load
        assert "next login" in result

    @patch("jacked.service.platform._service_is_running", return_value=False)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_migrates_preexisting_baked_host(
        self, mock_run, _mock_running, tmp_path, monkeypatch,
    ):
        """A pre-existing plist with a baked --host gets its host captured into
        the settings DB BEFORE the host-free rewrite (data-preserving)."""
        import os
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        plist = tmp_path / "ai.hank.jacked.plist"
        plist.write_text(_LEGACY_PLIST, encoding="utf-8")
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                with patch(
                    "jacked.service.migrate.migrate_baked_host_to_db",
                    return_value="migrated",
                ) as mock_migrate:
                    install_autostart(8321)
        mock_migrate.assert_called_once_with("0.0.0.0")
        assert "--host" not in plist.read_text()

    @patch("sys.platform", "win32")
    def test_win32_writes_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            with patch("jacked.findbin.find_bin", return_value=r"C:\bin\jacked.exe"):
                install_autostart(8321)
        assert vbs.exists()
        content = vbs.read_text()
        # Launches jacked either way: pythonw -m jacked (preferred) or the
        # jacked.exe trampoline as fallback. Host-free in both.
        assert "jacked" in content
        assert "--host" not in content
        assert "--port 8321" in content

    @patch("sys.platform", "win32")
    def test_win32_migrates_preexisting_baked_host(self, tmp_path):
        """A pre-existing VBS with a baked --host gets migrated, then rewritten
        host-free unconditionally (Startup script only fires at next login —
        nothing is loaded, nothing to bootout)."""
        vbs = tmp_path / "jacked.vbs"
        vbs.write_text(_LEGACY_VBS, encoding="utf-8")
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            with patch("jacked.findbin.find_bin", return_value=r"C:\bin\jacked.exe"):
                with patch(
                    "jacked.service.migrate.migrate_baked_host_to_db",
                    return_value="migrated",
                ) as mock_migrate:
                    install_autostart(8321)
        mock_migrate.assert_called_once_with("0.0.0.0")
        assert "--host" not in vbs.read_text()


class TestUninstallAutostart:
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


class TestInstallAutostartFailure:
    """Tests for install_autostart error paths."""

    def test_returns_error_when_jacked_not_on_path(self):
        from jacked.service.platform import install_autostart
        with patch("jacked.findbin.find_bin", return_value=None):
            result = install_autostart(8321)
        assert "Could not find" in result

    @patch("jacked.service.platform._service_is_running", return_value=False)
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_plist_uses_resolved_binary_path(
        self, mock_run, _mock_running, tmp_path, monkeypatch,
    ):
        import os
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/Users/test/.local/bin/jacked"):
                install_autostart(8321)
        content = plist.read_text()
        assert "/Users/test/.local/bin/jacked" in content
