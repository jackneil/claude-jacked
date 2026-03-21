"""Unit tests for Chrome DevTools MCP management functions."""

import subprocess
from unittest import mock

import pytest

from jacked.cli import (
    CHROME_DEVTOOLS_MODES,
    _get_chrome_devtools_mcp_status,
    _install_chrome_devtools_mcp,
    _remove_chrome_devtools_mcp,
    _run_claude_mcp,
    _set_chrome_devtools_mcp_mode,
)


# ---------------------------------------------------------------------------
# _run_claude_mcp
# ---------------------------------------------------------------------------


class TestRunClaudeMcp:
    def test_returns_none_when_claude_not_found(self):
        with mock.patch("shutil.which", return_value=None):
            assert _run_claude_mcp("get", "chrome-devtools") is None

    def test_returns_completed_process_on_success(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
             mock.patch("subprocess.run", return_value=fake) as run:
            result = _run_claude_mcp("get", "chrome-devtools")
            assert result is not None
            assert result.returncode == 0
            run.assert_called_once_with(
                ["/usr/bin/claude", "mcp", "get", "chrome-devtools"],
                capture_output=True, text=True, timeout=10,
            )

    def test_custom_timeout_passed_through(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
             mock.patch("subprocess.run", return_value=fake) as run:
            _run_claude_mcp("add", "foo", timeout=30)
            assert run.call_args[1]["timeout"] == 30

    def test_returns_none_on_timeout(self):
        with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
             mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
            assert _run_claude_mcp("get", "chrome-devtools") is None

    def test_returns_none_on_os_error(self):
        with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
             mock.patch("subprocess.run", side_effect=OSError("no such file")):
            assert _run_claude_mcp("get", "chrome-devtools") is None


# ---------------------------------------------------------------------------
# _get_chrome_devtools_mcp_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_not_installed_when_claude_missing(self):
        with mock.patch("jacked.cli._run_claude_mcp", return_value=None):
            status = _get_chrome_devtools_mcp_status()
            assert status["installed"] is False
            assert "not found" in status["details"]

    def test_not_configured_on_nonzero_exit(self):
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fake):
            status = _get_chrome_devtools_mcp_status()
            assert status["installed"] is False
            assert status["details"] == "Not configured"

    def test_detects_autoconnect_mode(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Args: chrome-devtools-mcp --autoConnect", stderr="",
        )
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fake):
            status = _get_chrome_devtools_mcp_status()
            assert status["installed"] is True
            assert status["mode"] == "autoConnect"

    def test_detects_browser_url_mode(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Args: chrome-devtools-mcp --browserUrl http://127.0.0.1:9222", stderr="",
        )
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fake):
            assert _get_chrome_devtools_mcp_status()["mode"] == "browserUrl"

    def test_detects_headless_mode(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Args: --headless", stderr="",
        )
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fake):
            assert _get_chrome_devtools_mcp_status()["mode"] == "headless"

    def test_defaults_to_launch_mode(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Args: chrome-devtools-mcp", stderr="",
        )
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fake):
            assert _get_chrome_devtools_mcp_status()["mode"] == "launch"


# ---------------------------------------------------------------------------
# _install_chrome_devtools_mcp
# ---------------------------------------------------------------------------


class TestInstall:
    def test_skips_when_already_installed(self):
        get_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=get_result) as m:
            _install_chrome_devtools_mcp(force=False)
            # Should only call get, not add
            m.assert_called_once_with("get", "chrome-devtools")

    def test_installs_when_not_configured(self):
        get_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        add_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[0] == "get":
                return get_result
            return add_result

        with mock.patch("jacked.cli._run_claude_mcp", side_effect=fake_run):
            _install_chrome_devtools_mcp()
            assert any("add" in c for c in calls)

    def test_force_removes_before_adding(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            return ok

        with mock.patch("jacked.cli._run_claude_mcp", side_effect=fake_run):
            _install_chrome_devtools_mcp(force=True)
            arg_strs = [str(c) for c in calls]
            assert any("remove" in s for s in arg_strs)
            assert any("add" in s for s in arg_strs)

    def test_handles_claude_not_found(self):
        with mock.patch("jacked.cli._run_claude_mcp", return_value=None):
            # Should not raise
            _install_chrome_devtools_mcp()


# ---------------------------------------------------------------------------
# _remove_chrome_devtools_mcp
# ---------------------------------------------------------------------------


class TestRemove:
    def test_returns_true_on_success(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=ok):
            assert _remove_chrome_devtools_mcp() is True

    def test_returns_false_on_failure(self):
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=fail):
            assert _remove_chrome_devtools_mcp() is False

    def test_returns_false_when_claude_not_found(self):
        with mock.patch("jacked.cli._run_claude_mcp", return_value=None):
            assert _remove_chrome_devtools_mcp() is False


# ---------------------------------------------------------------------------
# _set_chrome_devtools_mcp_mode
# ---------------------------------------------------------------------------


class TestSetMode:
    def test_rejects_invalid_mode(self):
        ok, msg = _set_chrome_devtools_mcp_mode("invalid")
        assert ok is False
        assert "Unknown mode" in msg

    def test_sets_autoconnect_mode(self):
        ok_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=ok_result):
            ok, msg = _set_chrome_devtools_mcp_mode("autoConnect")
            assert ok is True
            assert "autoConnect" in msg

    def test_sets_headless_mode(self):
        ok_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("jacked.cli._run_claude_mcp", return_value=ok_result):
            ok, msg = _set_chrome_devtools_mcp_mode("headless")
            assert ok is True

    def test_rollback_on_add_failure_restores_previous_mode(self):
        """If remove succeeds but add fails, should rollback to previous mode."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            # get → installed with --headless, remove → ok, first add → fail, rollback add → ok
            if args[0] == "get":
                return subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout="Args: chrome-devtools-mcp --headless", stderr="",
                )
            if args[0] == "remove":
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            if args[0] == "add":
                add_count = len([c for c in calls if c[0] == "add"])
                if add_count == 1:
                    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            return None

        with mock.patch("jacked.cli._run_claude_mcp", side_effect=fake_run):
            ok, msg = _set_chrome_devtools_mcp_mode("browserUrl")
            assert ok is False
            # Verify rollback was attempted with the ORIGINAL mode (--headless), not --autoConnect
            add_calls = [c for c in calls if c[0] == "add"]
            assert len(add_calls) == 2
            rollback_args = add_calls[1]
            assert "--headless" in rollback_args, f"Rollback should restore --headless but got {rollback_args}"
            assert "--autoConnect" not in rollback_args

    def test_fails_gracefully_when_claude_missing(self):
        with mock.patch("jacked.cli._run_claude_mcp", return_value=None):
            ok, msg = _set_chrome_devtools_mcp_mode("autoConnect")
            # get returns None → had_existing = False, add returns None → fail
            assert ok is False


# ---------------------------------------------------------------------------
# CHROME_DEVTOOLS_MODES constant
# ---------------------------------------------------------------------------


class TestModesConstant:
    def test_all_expected_modes_present(self):
        assert set(CHROME_DEVTOOLS_MODES.keys()) == {
            "autoConnect", "browserUrl", "launch", "headless",
        }

    def test_autoconnect_has_flag(self):
        assert "--autoConnect" in CHROME_DEVTOOLS_MODES["autoConnect"]

    def test_launch_has_no_flags(self):
        assert CHROME_DEVTOOLS_MODES["launch"] == []
