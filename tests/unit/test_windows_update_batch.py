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
        # Two on the success path (first start + one retry), plus one on the
        # rollback path that brings the restored build back up.
        success_path, rollback_path = body.split(":rollback_now", 1)
        assert success_path.count("start \"\" /B jacked service start") == 2
        assert rollback_path.count("start \"\" /B jacked service start") == 1
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


class TestPreflightGateAndRollback:
    """Both batches must run the transaction gate and undo a bad build.

    A refused preflight on Windows has to be handled inside the batch: the
    Python process that started the upgrade is already gone by then.
    """

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_preflight_runs_before_the_settings_migration(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        assert "jacked service preflight --timeout 120 2>&1" in body
        # Only the FIRST occurrence matters: the rollback tail can mention
        # neither before the gate.
        gate = body.index("jacked service preflight --timeout 120 2>&1")
        migrate = body.index("jacked install --force 2>&1")
        assert gate < migrate

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_refused_preflight_jumps_to_the_rollback_label(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        gate = body.index("jacked service preflight --timeout 120 2>&1")
        tail = body[gate:]
        first_check = tail.split("\r\n")[1]
        assert first_check.startswith("if errorlevel 1 goto ")
        label = first_check.rsplit(" ", 1)[1]
        assert ":" + label in body, f"batch has no :{label} label"

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_rollback_pins_the_exact_previous_version(self, which):
        import jacked

        body = BATCH_BUILDERS[which]()["body"]
        # Both helpers pin the build that is running right now: that is the
        # one a failed upgrade has to restore.
        previous = jacked.__version__
        pin = f'"claude-jacked[tray]=={previous}"'
        assert pin in body, f"rollback does not pin {previous}"
        # Every argv element is individually quoted so cmd.exe cannot split a
        # path containing spaces.
        rollback_line = [
            line for line in body.splitlines()
            if pin in line and line.strip().startswith('"')
        ]
        assert rollback_line, "rollback line is not fully quoted"
        for token in rollback_line[0].replace(" 2>&1", "").split('" "'):
            assert token.strip('"'), "empty argv element in the rollback line"

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_rollback_reinstalls_and_exits_nonzero(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        _success, rollback = body.split(
            ":rollback_now" if which == "tray" else ":preflight_failed", 1
        )
        assert "jacked install --force" in rollback
        assert rollback.rstrip().endswith("exit /b 1")

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_rollback_writes_the_failed_file_with_a_reason(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        marker = ":rollback_now" if which == "tray" else ":preflight_failed"
        rollback = body.split(marker, 1)[1]
        assert "jacked-update-failed.txt" in rollback
        assert "Recovery:" in rollback

    def test_cli_batch_skips_the_rollback_restart_with_skip_service(self):
        """--skip-service must not restart the service on the rollback path."""
        body = _cli_batch(("--skip-service",))["body"]
        rollback = body.split(":preflight_failed", 1)[1]
        assert "set SKIP_SERVICE=1" in body
        # No restart COMMAND on the rollback path, guarded or bare. (The
        # recovery text may still mention it as a manual step.)
        assert not [
            ln for ln in rollback.split("\r\n")
            if ln.strip().startswith("jacked service restart")
        ]
        assert "goto rollback_done" in rollback

    def test_cli_batch_restarts_on_the_rollback_path_by_default(self):
        body = _cli_batch()["body"]
        rollback = body.split(":preflight_failed", 1)[1]
        assert "jacked service restart 2>&1" in rollback

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_success_path_never_falls_into_the_rollback_block(self, which):
        """The rollback tail sits after the self-delete, reachable only by goto."""
        body = BATCH_BUILDERS[which]()["body"]
        marker = ":rollback_now" if which == "tray" else ":preflight_failed"
        self_delete = body.index('(goto) 2>nul & del "%~f0"')
        assert self_delete < body.index(marker)


class TestEveryRollbackStepIsChecked:
    """A rollback that stopped halfway must never write "rolled back".

    Both batches used to run `jacked install --force` and the guarded
    `jacked service restart` with no `if errorlevel 1` check at all, then write
    the "rolled back" recovery file regardless. The tray batch went further and
    marked the `rolling_back` phase ok BEFORE it restarted anything.
    """

    @staticmethod
    def _rollback_tail(which: str) -> str:
        body = BATCH_BUILDERS[which]()["body"]
        marker = ":rollback_now" if which == "tray" else ":preflight_failed"
        return body.split(marker, 1)[1]

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_each_rollback_step_is_followed_by_an_errorlevel_check(self, which):
        tail = self._rollback_tail(which)
        lines = [ln for ln in tail.split("\r\n") if ln]
        steps = [
            i for i, ln in enumerate(lines)
            if ln.startswith(('"', "jacked install --force", "jacked service restart"))
        ]
        assert steps, "no rollback work steps found"
        for i in steps:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            assert nxt.startswith("if errorlevel 1 goto "), (
                f"{which} rollback step {lines[i]!r} is unchecked; "
                f"next line is {nxt!r}"
            )

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_each_failure_label_exists_and_exits_nonzero(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        tail = self._rollback_tail(which)
        targets = {
            ln.rsplit(" ", 1)[1]
            for ln in tail.split("\r\n")
            if ln.startswith("if errorlevel 1 goto ")
        }
        assert targets
        for label in targets:
            assert ":" + label + "\r\n" in body, f"missing :{label} label"
            after = body.split(":" + label + "\r\n", 1)[1].split("\r\n")
            block_lines = []
            for line in after:
                block_lines.append(line)
                if line.strip() == "exit /b 1":
                    break
            block = "\r\n".join(block_lines)
            assert "jacked-update-failed.txt" in block
            assert "exit /b 1" in block
            # It names the step that failed rather than claiming a rollback.
            assert "step" in block
            assert "rolled back" not in block

    def test_the_tray_marks_rolling_back_ok_only_after_the_verify(self):
        tail = self._rollback_tail("tray")
        ok = tail.index("jacked _update_status rolling_back ok")
        verify = tail.index("Invoke-WebRequest")
        assert verify < ok, (
            "the rollback phase is marked ok before the restored service is "
            "verified, so a dead service reads as a restoration"
        )

    def test_a_version_that_cannot_be_validated_never_reaches_a_batch_line(self):
        """The version is validated once, at the top, and used everywhere."""
        with patch("jacked.__version__", 'evil" & del C:\\ &'):
            body = _tray_batch()["body"]
        assert "del C:" not in body
        assert "previous" in body
        # With no usable version there is no rollback command either.
        assert "claude-jacked[tray]==" not in body


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


def test_cli_batch_never_carries_an_invalid_previous_version():
    """A bad previous version must not reach cmd.exe through the CLI batch."""
    with patch("jacked.__version__", 'evil" & del C:\\ &'):
        body = _cli_batch()["body"]
    assert "del C:" not in body
    assert "claude-jacked[tray]==" not in body


def test_upgrade_refuses_shell_text_in_extras():
    from click.testing import CliRunner

    from jacked.cli import main

    result = CliRunner().invoke(main, ["upgrade", "--extras", 'x" & echo pwned'])
    assert result.exit_code == 2
    assert "Invalid --extras" in result.output



class TestEveryUpgradeStepIsChecked:
    """A failed step after the preflight must roll back, not report success.

    Both batches ran `jacked install --force` (and, in the CLI helper, the
    service restart) with NO `if errorlevel 1` guard at all, then printed
    "upgrade complete" and deleted themselves. The user was left on a build
    whose settings never migrated, with no breadcrumb and nothing rolled back.
    """

    @staticmethod
    def _success_path(which: str) -> str:
        body = BATCH_BUILDERS[which]()["body"]
        marker = ":rollback_now" if which == "tray" else ":preflight_failed"
        return body.split(marker, 1)[0]

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_settings_migration_is_checked(self, which):
        lines = [ln for ln in self._success_path(which).split("\r\n") if ln]
        migrate = [
            i for i, ln in enumerate(lines) if ln == "jacked install --force 2>&1"
        ]
        assert migrate, "the settings migration step is missing"
        nxt = lines[migrate[0] + 1]
        assert nxt.startswith("if errorlevel 1 goto "), (
            f"{which}: a failed settings migration is unchecked; next line is "
            f"{nxt!r}"
        )

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_settings_migration_failure_label_rolls_back(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        lines = [ln for ln in self._success_path(which).split("\r\n") if ln]
        migrate = lines.index("jacked install --force 2>&1")
        label = lines[migrate + 1].rsplit(" ", 1)[1]
        assert ":" + label + "\r\n" in body, f"missing :{label} label"
        block = body.split(":" + label + "\r\n", 1)[1]
        # It reaches the rollback rather than exiting with the new build in
        # place and the old service gone. Either by an explicit goto or by
        # falling straight into the shared block on the next line.
        rollback_entry = "rollback_now" if which == "tray" else "do_rollback"
        assert ":" + rollback_entry + "\r\n" in body
        before = block.split(":" + rollback_entry + "\r\n", 1)[0]
        lines = [ln for ln in before.split("\r\n") if ln]
        assert "exit /b 1" not in lines, (
            f"{which}: a failed settings migration exits without rolling back"
        )

    def test_the_cli_service_restart_is_checked(self):
        lines = [ln for ln in self._success_path("cli").split("\r\n") if ln]
        restart = [
            i for i, ln in enumerate(lines) if ln == "jacked service restart 2>&1"
        ]
        assert restart, "the restart step is missing from the success path"
        nxt = lines[restart[0] + 1]
        assert nxt.startswith("if errorlevel 1 goto "), (
            f"a failed service restart is unchecked; next line is {nxt!r}"
        )

    def test_the_cli_restart_failure_label_rolls_back(self):
        body = _cli_batch()["body"]
        assert ":restart_failed\r\n" in body
        block = body.split(":restart_failed\r\n", 1)[1]
        assert "goto do_rollback" in block.split(":do_rollback")[0]

    def test_the_cli_skip_service_batch_has_no_dangling_restart_goto(self):
        """With --skip-service nothing restarts, so nothing may jump there."""
        body = _cli_batch(("--skip-service",))["body"]
        assert "goto restart_failed" not in body

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_rollback_breadcrumb_names_the_step_that_failed(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        marker = ":rollback_now" if which == "tray" else ":preflight_failed"
        # Each entry label sets its own reason, and the shared breadcrumb
        # prints it, so a failed migration is never reported as a refused
        # preflight.
        reasons = [
            ln for ln in body.split("\r\n") if ln.startswith("set FAILREASON=")
        ]
        assert len(reasons) >= 3, reasons
        assert any("settings migration" in ln for ln in reasons), reasons
        tail = body.split(marker, 1)[1]
        assert "%FAILREASON%" in tail

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_success_path_clears_a_stale_failed_file(self, which):
        """A repaired failure must not warn the user on the next boot."""
        success = self._success_path(which)
        deletes = [
            ln
            for ln in success.split("\r\n")
            if ln.startswith("if exist ") and "jacked-update-failed.txt" in ln
            and " del " in ln
        ]
        assert deletes, (
            f"{which}: a successful update leaves the old "
            "jacked-update-failed.txt in place, so the tray warns about a "
            "failure the user already repaired"
        )


def test_cli_batch_claims_the_status_record_before_installing():
    """Windows cannot hold an OS lock across batch steps; the status-record
    claim (`_update_status_init`, exit 2 when busy) is the in-flight guard."""
    body = _cli_batch()["body"]
    claim = body.index("jacked _update_status_init")
    guard = body.index("if errorlevel 2", claim)
    assert body.index("uv.exe", guard) > guard or body.index("tool install", guard) > guard
    assert "exit /b 2" in body[guard:guard + 400]

