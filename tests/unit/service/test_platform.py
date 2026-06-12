"""Tests for jacked.service.platform module."""

from unittest.mock import patch



class TestGenerateLaunchdPlist:
    def test_contains_label(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=8321)
        assert "ai.hank.jacked" in plist

    def test_contains_binary_path(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/opt/bin/jacked", host="127.0.0.1", port=8321)
        assert "/opt/bin/jacked" in plist

    def test_contains_run_at_load(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=8321)
        assert "<key>RunAtLoad</key>" in plist
        assert "<true/>" in plist

    def test_contains_keep_alive(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=8321)
        assert "<key>KeepAlive</key>" in plist

    def test_keep_alive_scoped_to_crash_only(self):
        """KeepAlive should only restart on crash, not on clean user stop."""
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=8321)
        assert "<key>SuccessfulExit</key>" in plist
        # SuccessfulExit false means "don't restart after successful exit"

    def test_custom_port_in_args(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=9000)
        assert "9000" in plist

    def test_log_path_in_plist(self):
        from jacked.service.platform import _generate_launchd_plist
        plist = _generate_launchd_plist(jacked_bin="/usr/local/bin/jacked", host="127.0.0.1", port=8321)
        assert "jacked-service.log" in plist


class TestGenerateWindowsVbs:
    def test_contains_jacked_path(self):
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\Users\test\.local\bin\jacked.exe", host="127.0.0.1", port=8321)
        assert r"C:\Users\test\.local\bin\jacked.exe" in vbs

    def test_hidden_window(self):
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", host="127.0.0.1", port=8321)
        assert ", 0," in vbs

    def test_custom_port_in_vbs(self):
        from jacked.service.platform import _generate_windows_vbs
        vbs = _generate_windows_vbs(jacked_bin=r"C:\bin\jacked.exe", host="127.0.0.1", port=9000)
        assert "--port 9000" in vbs


class TestDetectAutostart:
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
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_darwin_writes_plist(self, mock_run, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/usr/local/bin/jacked"):
                result = install_autostart("127.0.0.1", 8321)
        assert plist.exists()
        content = plist.read_text()
        assert "ai.hank.jacked" in content
        # launchctl load should have been called to start service immediately
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "launchctl" in args and "load" in args
        assert "running now" in result

    @patch("sys.platform", "win32")
    def test_win32_writes_vbs(self, tmp_path):
        vbs = tmp_path / "jacked.vbs"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_windows_startup_path", return_value=vbs):
            with patch("jacked.findbin.find_bin", return_value=r"C:\bin\jacked.exe"):
                install_autostart("127.0.0.1", 8321)
        assert vbs.exists()
        content = vbs.read_text()
        assert "jacked.exe" in content


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
            result = install_autostart("127.0.0.1", 8321)
        assert "Could not find" in result

    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_plist_uses_resolved_binary_path(self, mock_run, tmp_path):
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("jacked.findbin.find_bin", return_value="/Users/test/.local/bin/jacked"):
                install_autostart("127.0.0.1", 8321)
        content = plist.read_text()
        assert "/Users/test/.local/bin/jacked" in content
