"""Regression tests for the two Windows update batches.

Both `jacked upgrade` (jacked.cli._spawn_windows_upgrade_helper) and the tray's
update click (jacked.service.updater._spawn_windows_tray_updater) generate a
cmd.exe batch and spawn it with its stdout bound to an inherited handle on
~/.claude/jacked-update.log.

The bug these tests exist to prevent: cmd.exe cannot open a redirection target
that another handle already holds open for writing. Any `>> "%LOGFILE%"` inside
these batches therefore fails with "The process cannot access the file because
it is being used by another process" — and crucially cmd SKIPS the command
while leaving ERRORLEVEL at 0, so every `if errorlevel 1` guard sails right past
the step that never ran. Shipped once (v0.92.1), it silently no-opped the
package upgrade, the settings migration AND the service start, leaving the user
on the old version with no tray icon and a status file full of green "ok"s.

test_cmd_redirect_fails_when_log_handle_is_held is the load-bearing one: it
pins the cmd.exe behaviour the other assertions are derived from, so if that
platform assumption ever changes the reason for those assertions is visible.
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from click.testing import CliRunner

NO_WINDOW = 0x08000000


def _capture_batch(spawn) -> dict:
    """Run *spawn* with Popen stubbed; return the generated batch body."""
    seen = {}

    def fake_popen(args, **kwargs):
        seen["argv"] = args
        seen["kwargs"] = kwargs
        # newline="" — the batch is CRLF and the assertions compare against
        # CRLF-joined builders; universal-newline translation would break them.
        with open(args[2], encoding="utf-8", newline="") as fh:
            seen["body"] = fh.read()

        class _P:
            pid = 1

        return _P()

    spawn(fake_popen)
    return seen


def _tray_batch():
    def spawn(fake_popen):
        from jacked.service import updater

        with patch.object(updater.subprocess, "Popen", fake_popen), patch(
            "jacked.install_method.can_auto_upgrade", return_value=(True, "")
        ), patch(
            "jacked.install_method.detect_install_method", return_value="uv"
        ), patch(
            "jacked.findbin.find_bin",
            side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n),
        ):
            updater._spawn_windows_tray_updater(
                4242, "tray", target_version="9.9.9", port=8321
            )

    return _capture_batch(spawn)


def _cli_batch(extra_args=()):
    def spawn(fake_popen):
        from jacked.cli import main

        with patch("sys.platform", "win32"), patch(
            "jacked.install_method.detect_install_method", return_value="uv"
        ), patch(
            "jacked.findbin.find_bin",
            side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n),
        ), patch("subprocess.Popen", fake_popen):
            CliRunner().invoke(main, ["upgrade", *extra_args])

    return _capture_batch(spawn)


BATCH_BUILDERS = {"tray": _tray_batch, "cli": _cli_batch}


class TestNoLogfileRedirection:
    """The regression that cost a user their tray icon."""

    @pytest.mark.parametrize("which", sorted(BATCH_BUILDERS))
    def test_no_step_redirects_to_logfile(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        offenders = [
            line
            for line in body.splitlines()
            if "%LOGFILE%" in line and (">>" in line or ">" in line.split("%LOGFILE%")[0])
        ]
        # `echo ... See %LOGFILE% ... > recovery.txt` is fine — different file.
        offenders = [ln for ln in offenders if "jacked-update-failed.txt" not in ln]
        assert offenders == [], (
            f"{which} batch redirects to the log cmd.exe already holds open; "
            f"these steps will silently no-op: {offenders}"
        )

    @pytest.mark.parametrize("which", sorted(BATCH_BUILDERS))
    def test_spawn_binds_stdout_to_the_log(self, which):
        """stdout must be a real handle on the log, not DEVNULL.

        This is the other half of the contract: bare `echo` in the batch only
        reaches the log because cmd inherits this handle.
        """
        from jacked.service.updater import UPDATE_LOG

        kwargs = BATCH_BUILDERS[which]()["kwargs"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.STDOUT
        stdout = kwargs["stdout"]
        assert stdout != subprocess.DEVNULL
        assert Path(stdout.name) == UPDATE_LOG

    @pytest.mark.parametrize("which", sorted(BATCH_BUILDERS))
    def test_work_steps_are_present_and_unredirected(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        assert "jacked install --force 2>&1" in body
        assert '"tool" "install"' in body
        upgrade_lines = [ln for ln in body.splitlines() if '"tool" "install"' in ln]
        assert upgrade_lines and all(
            "%LOGFILE%" not in ln for ln in upgrade_lines
        ), upgrade_lines


class TestSleepIsConsoleFree:
    """`timeout` refuses a redirected stdin; these helpers always have one."""

    @pytest.mark.parametrize("which", sorted(BATCH_BUILDERS))
    def test_no_timeout_command(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        assert "timeout /t" not in body, (
            "timeout exits instantly with 'Input redirection is not supported' "
            "under stdin=DEVNULL, collapsing the bounded wait to zero"
        )

    @pytest.mark.parametrize("which", sorted(BATCH_BUILDERS))
    def test_uses_ping_sleep(self, which):
        assert "ping -n 2 127.0.0.1" in BATCH_BUILDERS[which]()["body"]


class TestWaitLoopIsShared:
    """Both helpers must build the poll loop from the same function."""

    def test_both_batches_embed_the_shared_block(self):
        from jacked.service.updater import wait_for_parent_block

        tray = _tray_batch()["body"]
        cli = _cli_batch()["body"]
        assert wait_for_parent_block(4242) in tray
        # the CLI helper waits on its own PID, so match the loop's fixed parts
        for marker in (":wait", ":waitdone", "JACKED_WAITED", "GEQ 120"):
            assert marker in cli

    def test_poll_uses_system32_binaries(self):
        """A shadowing find/tasklist would invert the loop's exit test."""
        from jacked.service.updater import wait_for_parent_block

        block = wait_for_parent_block(4242)
        assert r"%SystemRoot%\System32\tasklist.exe" in block
        assert r"%SystemRoot%\System32\find.exe" in block


class TestVerifyRetry:
    """A failed update must not leave the user with no service at all."""

    def test_tray_batch_retries_service_start_once(self):
        body = _tray_batch()["body"]
        assert body.count("start \"\" /B jacked service start") == 2
        assert "if not errorlevel 1 goto verifyok" in body
        assert ":verifyok" in body

    def test_retry_verify_is_not_nested_in_a_paren_block(self):
        """The verify line is a paren-heavy powershell one-liner; burying it in
        an `if (...)` block is a cmd.exe parsing trap."""
        body = _tray_batch()["body"]
        for line in body.splitlines():
            if line.strip().startswith("powershell"):
                assert not line.startswith(" "), (
                    "verify line is indented, i.e. inside a parenthesised block"
                )


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe redirection semantics")
class TestCmdRedirectionSemantics:
    """Pin the platform behaviour every assertion above is derived from."""

    def test_cmd_redirect_fails_when_log_handle_is_held(self):
        work = Path(tempfile.mkdtemp(prefix="jkredir-"))
        log = work / "held.log"
        log.write_text("", encoding="utf-8")
        bat = work / "t.bat"
        bat.write_text(
            "@echo off\r\n"
            f'set LOGFILE={log}\r\n'
            'echo REDIRECTED >> "%LOGFILE%"\r\n'
            "echo ERRORLEVEL_IS_%ERRORLEVEL%\r\n"
            "echo BARE-OUTPUT\r\n",
            encoding="utf-8",
            newline="",
        )

        held = open(log, "a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                ["cmd.exe", "/c", str(bat)],
                stdin=subprocess.DEVNULL,
                stdout=held,
                stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW,
                close_fds=True,
            )
            proc.wait()
        finally:
            held.close()

        text = log.read_text(encoding="utf-8", errors="replace")
        # The redirected echo never landed...
        assert "REDIRECTED" not in text
        assert "being used by another process" in text
        # ...but ERRORLEVEL stayed 0, so `if errorlevel 1` would NOT have caught it.
        assert "ERRORLEVEL_IS_0" in text
        # ...while bare output reaches the log just fine. Hence: no redirection.
        assert "BARE-OUTPUT" in text
