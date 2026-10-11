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

    seen["result"] = spawn(fake_popen)
    return seen


TRAY_CREATION_ID = "windows-filetime:133000000000000000"


def _tray_identity(pid):
    from jacked.service.instance_models import ProcessIdentity

    return ProcessIdentity(
        pid=pid, creation_id=TRAY_CREATION_ID, executable=r"C:\py\python.exe"
    )


def _tray_batch(identity=_tray_identity, port=8321):
    """Build the tray batch. ``identity`` replaces process_identity."""

    def spawn(fake_popen):
        from jacked.service import updater

        with patch.object(updater.subprocess, "Popen", fake_popen), patch(
            "jacked.install_method.can_auto_upgrade", return_value=(True, "")
        ), patch(
            "jacked.install_method.detect_install_method", return_value="uv"
        ), patch(
            "jacked.findbin.find_bin",
            side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n),
        ), patch(
            "jacked.service.instance.process_identity", side_effect=identity
        ), patch.object(updater.sys, "platform", "win32"):
            updater._spawn_windows_tray_updater(
                4242, "tray", target_version="9.9.9", port=port
            )

    return _capture_batch(spawn)


def _released():
    from jacked.service.install_release import EXIT_RELEASED, ReleaseResult

    return ReleaseResult(EXIT_RELEASED, "No jacked service is running.")


def _cli_batch(extra_args=(), precheck=None):
    """Build the CLI batch. ``precheck`` replaces release_install in-process."""

    def spawn(fake_popen):
        from jacked.cli import main

        with patch("sys.platform", "win32"), patch(
            "jacked.install_method.detect_install_method", return_value="uv"
        ), patch(
            "jacked.findbin.find_bin",
            side_effect=lambda n: {"uv": r"C:\uv\uv.exe"}.get(n),
        ), patch(
            "jacked.service.install_release.release_install",
            side_effect=precheck or (lambda **_kw: _released()),
        ), patch("subprocess.Popen", fake_popen):
            seen_result = CliRunner().invoke(main, ["upgrade", *extra_args])
        return seen_result

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
        # Two on the success path (first start + one retry), one on the
        # rollback path that brings the restored build back up, and one on
        # the release-failure path that brings the untouched build back up.
        success_path, rest = body.split(":rollback_now", 1)
        rollback_path, release_path = rest.split(":release_failed\r\n", 1)
        assert success_path.count("start \"\" /B jacked service start") == 2
        assert rollback_path.count("start \"\" /B jacked service start") == 1
        assert release_path.count("start \"\" /B jacked service start") == 1
        assert 'if "%ERRORLEVEL%"=="0" goto verifyok' in body
        assert "if not errorlevel 1 goto verifyok" not in body
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



# ---------------------------------------------------------------------------
# Release-before-install, package retry, and package rollback.
#
# The 2026-10-10 Windows defect: the batch ran `uv tool install --force` while
# the owned service still ran from the tool venv. uv deleted part of the venv,
# stopped with "Access is denied (os error 5)", and the batch hit `exit /b 1`:
# a broken install, no rollback, and the old service still in memory.
#
# The string checks below pin the shape. `_simulate` pins the control flow: it
# walks the batch the way cmd.exe does for the small subset these batches use
# (labels, goto, `if [not] errorlevel N`, `if %VAR% GEQ N`, parenthesised
# `if` blocks, `set /a`, `exit /b`), and asks a fake for each command's exit
# code. It is not cmd.exe; it proves which lines a given outcome reaches.
# ---------------------------------------------------------------------------

import re  # noqa: E402

_PACKAGE_MARK = '"tool" "install" "claude-jacked[tray]" '
_ROLLBACK_MARK = '"claude-jacked[tray]=='


class _Run:
    def __init__(self):
        self.commands: list[str] = []
        self.echoes: list[str] = []
        self.exit_code: "int | None" = None
        self.env: dict[str, str] = {}

    def ran(self, needle: str) -> int:
        return sum(1 for c in self.commands if needle in c)

    @property
    def failed_file(self) -> str:
        return "\n".join(
            e for e in self.echoes if "jacked-update-failed.txt" in e
        )


def _simulate(body: str, outcome, max_steps: int = 5000) -> _Run:
    """Walk *body*; ``outcome(command) -> int`` gives each command's exit code."""
    lines = [ln.strip() for ln in body.split("\r\n")]
    labels = {ln[1:]: i for i, ln in enumerate(lines) if ln.startswith(":")}
    run = _Run()
    errorlevel = 0

    def expand(text: str) -> str:
        return re.sub(
            r"%(\w+)%", lambda m: run.env.get(m.group(1), m.group(0)), text
        )

    def condition(cond: str) -> bool:
        m = re.fullmatch(r"(not )?errorlevel (\d+)", cond)
        if m:
            hit = errorlevel >= int(m.group(2))
            return not hit if m.group(1) else hit
        m = re.fullmatch(r'(not )?"%ERRORLEVEL%"=="(-?\d+)"', cond)
        if m:
            hit = errorlevel == int(m.group(2))
            return not hit if m.group(1) else hit
        m = re.fullmatch(r"(\S+) GEQ (\d+)", expand(cond))
        if m:
            return int(m.group(1)) >= int(m.group(2))
        raise AssertionError(f"simulator: unsupported condition {cond!r}")

    def block_end(start: int) -> int:
        depth = 0
        for j in range(start, len(lines)):
            if lines[j].endswith("("):
                depth += 1
            elif lines[j] == ")":
                depth -= 1
                if depth == 0:
                    return j
        raise AssertionError("simulator: unbalanced block")

    i, steps = 0, 0
    while i < len(lines):
        steps += 1
        assert steps < max_steps, "simulator: the batch loops forever"
        line = lines[i]
        i += 1
        if not line or line.startswith(":") or line == ")" or line == "@echo off":
            continue
        if line.startswith("(goto) 2>nul"):
            run.exit_code = 0
            return run
        m = re.fullmatch(r"if (.+?) \($", line)
        if m:
            if not condition(m.group(1)):
                i = block_end(i - 1) + 1
            continue
        m = re.fullmatch(r"if (.+?) goto (\S+)", line)
        if m:
            if condition(m.group(1)):
                i = labels[m.group(2)]
            continue
        m = re.fullmatch(r"goto (\S+)", line)
        if m:
            i = labels[m.group(1)]
            continue
        m = re.fullmatch(r"exit /b (\d+)", line)
        if m:
            run.exit_code = int(m.group(1))
            return run
        m = re.fullmatch(r"set /a (\w+)=(\d+)", line)
        if m:
            run.env[m.group(1)] = m.group(2)
            continue
        m = re.fullmatch(r"set /a (\w+)\+=(\d+)", line)
        if m:
            run.env[m.group(1)] = str(
                int(run.env.get(m.group(1), "0")) + int(m.group(2))
            )
            continue
        m = re.fullmatch(r"set (\w+)=(.*)", line)
        if m:
            run.env[m.group(1)] = m.group(2)
            continue
        if line.startswith("echo "):
            run.echoes.append(expand(line[5:]))
            continue
        if line.startswith("if exist "):
            continue
        run.commands.append(line)
        errorlevel = outcome(line)
    raise AssertionError("simulator: the batch ran off its end")


def _outcomes(**overrides):
    """Exit codes by command; every unlisted command succeeds.

    A value may be an int or a list, which is consumed one item per call.
    """
    table = {
        "tasklist.exe": 1,  # the parent PID is gone
        "_release_install": 0,
        "package": 0,
        "rollback": 0,
        "jacked install --force": 0,
        "jacked service restart": 0,
        "jacked service preflight": 0,
        "powershell": 0,
        **overrides,
    }

    def key(command: str) -> str:
        if _ROLLBACK_MARK in command:
            return "rollback"
        if _PACKAGE_MARK in command:
            return "package"
        for name in table:
            # "package" and "rollback" match only through their argv marks:
            # `_update_status installing_package ...` is not the package step.
            if name not in ("package", "rollback") and name in command:
                return name
        return ""

    def outcome(command: str) -> int:
        value = table.get(key(command), 0)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    return outcome


class TestReleaseRunsBeforeThePackageStep:
    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_release_step_precedes_the_package_step(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        release = body.index("jacked _release_install --timeout 60 --port 8321")
        package = body.index(_PACKAGE_MARK)
        wait_done = body.index(":waitdone")
        assert wait_done < release < package

    def test_the_cli_skip_service_batch_only_checks(self):
        body = _cli_batch(("--skip-service",))["body"]
        release = body.index("jacked _release_install --check-only --timeout 60")
        assert release < body.index(_PACKAGE_MARK)
        assert "release_stopped_failed" not in body

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_clean_run_releases_installs_and_finishes(self, which):
        run = _simulate(BATCH_BUILDERS[which]()["body"], _outcomes())
        assert run.exit_code == 0
        assert run.ran("_release_install") == 1
        assert run.ran(_PACKAGE_MARK) == 1
        assert run.ran(_ROLLBACK_MARK) == 0
        release = next(
            i for i, c in enumerate(run.commands) if "_release_install" in c
        )
        package = next(
            i for i, c in enumerate(run.commands) if _PACKAGE_MARK in c
        )
        assert release < package


class TestAFailedReleaseNeverInstalls:
    def test_cli_exit_3_restarts_the_service_it_stopped(self):
        run = _simulate(_cli_batch()["body"], _outcomes(_release_install=3))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert run.ran(_ROLLBACK_MARK) == 0
        assert run.ran("jacked service restart") == 1
        assert "Nothing was installed" in run.failed_file

    @pytest.mark.parametrize("code", [1, 2])
    def test_cli_exit_1_restarts_nothing(self, code):
        """Exit 1 (or a usage error) means this step stopped nothing."""
        run = _simulate(_cli_batch()["body"], _outcomes(_release_install=code))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert run.ran("jacked service restart") == 0
        assert run.ran("jacked service start") == 0
        assert "did not stop it" in run.failed_file

    def test_cli_a_failed_restart_after_exit_3_names_the_step(self):
        run = _simulate(
            _cli_batch()["body"],
            _outcomes(_release_install=3, **{"jacked service restart": 1}),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert "service restart step failed" in run.failed_file
        assert "Run this first: jacked service restart" in run.failed_file

    def test_cli_skip_service_refusal_restarts_nothing(self):
        run = _simulate(
            _cli_batch(("--skip-service",))["body"],
            _outcomes(_release_install=1),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert run.ran("jacked service restart") == 0
        assert "--skip-service" in run.failed_file

    @pytest.mark.parametrize("code", [1, 3])
    def test_tray_any_failure_starts_the_previous_service(self, code):
        """The old tray stopped itself, so the tray batch always restarts."""
        run = _simulate(_tray_batch()["body"], _outcomes(_release_install=code))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert run.ran(_ROLLBACK_MARK) == 0
        assert run.ran('start "" /B jacked service start') == 1
        assert run.ran("_update_status waiting_for_parent failed") == 1
        assert "Nothing was installed" in run.failed_file
        assert "running again" in run.failed_file

    def test_tray_a_previous_service_that_does_not_start_is_reported(self):
        run = _simulate(
            _tray_batch()["body"],
            _outcomes(_release_install=3, powershell=1),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert "service start step failed" in run.failed_file
        assert "running again" not in run.failed_file


class TestThePackageStepRetries:
    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_transient_lock_is_retried_and_the_update_finishes(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"], _outcomes(package=[1, 1, 0])
        )
        assert run.exit_code == 0
        assert run.ran(_PACKAGE_MARK) == 3
        assert run.ran(_ROLLBACK_MARK) == 0
        attempts = [e for e in run.echoes if "package step: attempt" in e]
        assert [a.split("attempt ", 1)[1] for a in attempts] == [
            "1 of 3", "2 of 3", "3 of 3",
        ]
        assert sum("WARN: package step attempt" in e for e in run.echoes) == 2

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_attempts_are_separated_by_a_console_free_sleep(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        loop = body.split(":package_attempt", 1)[1].split(":package_done", 1)[0]
        assert "ping -n 4 127.0.0.1 >NUL" in loop
        assert "timeout /t" not in loop


class TestAFailedPackageStepRollsBack:
    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_three_failures_jump_to_the_rollback(self, which):
        import jacked

        run = _simulate(BATCH_BUILDERS[which]()["body"], _outcomes(package=1))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 3
        assert run.ran(f'"claude-jacked[tray]=={jacked.__version__}"') == 1
        assert run.ran("jacked install --force") == 1
        restart = "jacked service restart" if which == "cli" else (
            'start "" /B jacked service start'
        )
        assert run.ran(restart) == 1
        assert run.env["FAILREASON"] == "the package step failed after 3 attempts"
        assert "the package step failed after 3 attempts" in run.failed_file
        assert "rolled back" in run.failed_file
        # Neither batch may reach the preflight or settings migration of a
        # build that never installed.
        assert run.ran("jacked service preflight") == 0

    def test_the_tray_marks_the_package_phase_failed(self):
        run = _simulate(_tray_batch()["body"], _outcomes(package=1))
        assert run.ran("_update_status installing_package failed") == 1
        assert run.ran("_update_status installing_package ok") == 0
        assert run.ran("_update_status rolling_back ok") == 1

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_old_exit_without_rollback_is_gone(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        assert "ERROR: upgrade command failed" not in body
        loop = body.split(":package_attempt", 1)[1].split(":package_done", 1)[0]
        assert "exit /b" not in loop

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_failed_package_rollback_is_reported_by_step(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"], _outcomes(package=1, rollback=1)
        )
        assert run.exit_code == 1
        assert "package rollback step" in run.failed_file
        assert "rolled back to" not in run.failed_file


class TestAFailedPackageStepWithNoRollback:
    def test_cli_no_rollback_restarts_the_previous_service(self):
        run = _simulate(
            _cli_batch(("--no-rollback",))["body"], _outcomes(package=1)
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 3
        assert run.ran(_ROLLBACK_MARK) == 0
        assert run.ran("jacked service restart") == 1
        assert "no rollback command is available" in run.failed_file
        assert "rolled back" not in run.failed_file
        assert "NOT rolled back" not in run.failed_file

    def test_cli_no_rollback_reports_a_failed_restart(self):
        run = _simulate(
            _cli_batch(("--no-rollback",))["body"],
            _outcomes(package=1, **{"jacked service restart": 1}),
        )
        assert run.exit_code == 1
        assert "service restart step failed" in run.failed_file

    def test_cli_no_rollback_with_skip_service_restarts_nothing(self):
        run = _simulate(
            _cli_batch(("--no-rollback", "--skip-service"))["body"],
            _outcomes(package=1),
        )
        assert run.exit_code == 1
        assert run.ran("jacked service restart") == 0
        assert "no rollback command is available" in run.failed_file

    def test_tray_with_no_rollback_starts_the_previous_service(self):
        with patch("jacked.__version__", 'evil" & del C:\\ &'):
            body = _tray_batch()["body"]
        run = _simulate(body, _outcomes(package=1))
        assert run.exit_code == 1
        assert run.ran(_ROLLBACK_MARK) == 0
        assert run.ran('start "" /B jacked service start') == 1
        assert "no rollback command is available" in run.failed_file
        assert "rolled back" not in run.failed_file


class TestTheExistingFailurePathsStillWork:
    """The simulator agrees with the string checks above on the old paths."""

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_refused_preflight_rolls_back(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"],
            _outcomes(**{"jacked service preflight": 1}),
        )
        assert run.exit_code == 1
        assert run.ran(_ROLLBACK_MARK) == 1
        assert "rolled back" in run.failed_file

    def test_another_updater_in_flight_aborts_before_the_release(self):
        run = _simulate(
            _cli_batch()["body"], _outcomes(_update_status_init=2)
        )
        assert run.exit_code == 2
        assert run.ran("_release_install") == 0


# A crash exits with a negative NTSTATUS code. "if errorlevel N" means "at
# least N", so these codes pass every "if errorlevel 1" check.
_ACCESS_VIOLATION = -1073741819  # 0xC0000005
_CTRL_C_EXIT = -1073741510  # 0xC000013A


class TestANegativeExitIsNotSuccess:
    @pytest.mark.parametrize("code", [_ACCESS_VIOLATION, _CTRL_C_EXIT])
    def test_cli_a_crashed_release_step_installs_nothing(self, code):
        run = _simulate(_cli_batch()["body"], _outcomes(_release_install=code))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        # Nothing proves that this step stopped the service: restart nothing.
        assert run.ran("jacked service restart") == 0
        assert "did not stop it" in run.failed_file

    @pytest.mark.parametrize("code", [_ACCESS_VIOLATION, _CTRL_C_EXIT])
    def test_cli_skip_service_a_crashed_check_installs_nothing(self, code):
        run = _simulate(
            _cli_batch(("--skip-service",))["body"],
            _outcomes(_release_install=code),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0

    @pytest.mark.parametrize("code", [_ACCESS_VIOLATION, _CTRL_C_EXIT])
    def test_tray_a_crashed_release_step_installs_nothing(self, code):
        run = _simulate(_tray_batch()["body"], _outcomes(_release_install=code))
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert run.ran('start "" /B jacked service start') == 1
        assert "Nothing was installed" in run.failed_file

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_the_zero_check_follows_the_release_line_directly(self, which):
        """Nothing between the release line and the check resets ERRORLEVEL."""
        body = BATCH_BUILDERS[which]()["body"]
        lines = body.split("\r\n")
        start = next(
            i for i, ln in enumerate(lines)
            if ln.startswith("jacked _release_install")
        )
        following = []
        for ln in lines[start + 1:]:
            following.append(ln)
            if ln.startswith('if not "%ERRORLEVEL%"=="0" goto '):
                break
        assert following[-1].startswith('if not "%ERRORLEVEL%"=="0" goto ')
        assert all(ln.startswith("if ") for ln in following), following

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_crashed_package_attempt_is_retried_then_rolled_back(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"], _outcomes(package=_ACCESS_VIOLATION)
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 3
        assert run.ran(_ROLLBACK_MARK) == 1
        assert run.ran("jacked service preflight") == 0


class TestACrashedStepTakesTheFailurePath:
    """Every step check catches a negative NTSTATUS exit, not only exit 1."""

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_every_flat_check_is_paired_with_an_exact_compare(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        lines = body.split("\r\n")
        checks = [
            i for i, ln in enumerate(lines)
            if ln.startswith("if errorlevel 1 goto ")
            and ln != "if errorlevel 1 goto waitdone"
            and ln != "if errorlevel 1 goto release_refused"
        ]
        assert checks, "no step checks found"
        for i in checks:
            label = lines[i].rsplit(" ", 1)[1]
            assert lines[i + 1] == f'if not "%ERRORLEVEL%"=="0" goto {label}', (
                f"{which}: {lines[i]!r} has no exact compare after it"
            )

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_no_success_check_reads_a_crash_as_success(self, which):
        body = BATCH_BUILDERS[which]()["body"]
        assert "if not errorlevel 1" not in body
        assert "if errorlevel 1 (" not in body

    @pytest.mark.parametrize("which", ["tray", "cli"])
    @pytest.mark.parametrize("code", [_ACCESS_VIOLATION, _CTRL_C_EXIT])
    def test_a_crashed_preflight_rolls_back(self, which, code):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"],
            _outcomes(**{"jacked service preflight": code}),
        )
        assert run.exit_code == 1
        assert run.ran(_ROLLBACK_MARK) == 1
        assert run.ran("jacked install --force") == 1  # the rollback only
        assert "rolled back to" in run.failed_file

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_crashed_settings_migration_rolls_back(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"],
            _outcomes(**{"jacked install --force": [_ACCESS_VIOLATION, 0]}),
        )
        assert run.exit_code == 1
        assert run.ran(_ROLLBACK_MARK) == 1
        assert "settings migration failed" in run.env["FAILREASON"]
        assert "rolled back to" in run.failed_file

    def test_tray_a_crashed_verify_is_retried_then_rolled_back(self):
        run = _simulate(
            _tray_batch()["body"],
            _outcomes(powershell=[_ACCESS_VIOLATION, _ACCESS_VIOLATION, 0]),
        )
        assert run.exit_code == 1
        # First start, one retry, then the start of the restored build.
        assert run.ran('start "" /B jacked service start') == 3
        assert run.ran("_update_status verifying_service failed") == 1
        assert run.ran("_update_status verifying_service ok") == 0
        assert run.ran(_ROLLBACK_MARK) == 1
        assert "rolled back to" in run.failed_file

    def test_tray_a_crashed_first_verify_then_a_good_retry_finishes(self):
        run = _simulate(
            _tray_batch()["body"], _outcomes(powershell=[_ACCESS_VIOLATION, 0])
        )
        assert run.exit_code == 0
        assert run.ran('start "" /B jacked service start') == 2
        assert run.ran(_ROLLBACK_MARK) == 0

    def test_cli_a_crashed_restart_rolls_back(self):
        run = _simulate(
            _cli_batch()["body"],
            _outcomes(**{"jacked service restart": [_ACCESS_VIOLATION, 0]}),
        )
        assert run.exit_code == 1
        assert run.env["FAILREASON"] == "the new service did not restart"
        assert run.ran(_ROLLBACK_MARK) == 1
        assert "rolled back to" in run.failed_file

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_crashed_package_rollback_is_reported_by_step(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"],
            _outcomes(package=1, rollback=_ACCESS_VIOLATION),
        )
        assert run.exit_code == 1
        assert "package rollback step" in run.failed_file
        assert "rolled back to" not in run.failed_file

    @pytest.mark.parametrize("which", ["tray", "cli"])
    def test_a_crashed_rollback_migration_is_reported_by_step(self, which):
        run = _simulate(
            BATCH_BUILDERS[which]()["body"],
            _outcomes(package=1, **{"jacked install --force": _ACCESS_VIOLATION}),
        )
        assert run.exit_code == 1
        assert "settings migration step" in run.failed_file
        assert "rolled back to" not in run.failed_file

    def test_tray_a_crashed_rollback_verify_is_reported_by_step(self):
        run = _simulate(
            _tray_batch()["body"],
            _outcomes(package=1, powershell=_ACCESS_VIOLATION),
        )
        assert run.exit_code == 1
        assert run.ran("_update_status rolling_back ok") == 0
        assert "service restart step" in run.failed_file
        assert "rolled back to" not in run.failed_file

    def test_cli_a_crashed_rollback_restart_is_reported_by_step(self):
        run = _simulate(
            _cli_batch()["body"],
            _outcomes(package=1, **{"jacked service restart": _ACCESS_VIOLATION}),
        )
        assert run.exit_code == 1
        assert "service restart step" in run.failed_file
        assert "rolled back to" not in run.failed_file

    def test_cli_a_crashed_restart_after_a_release_timeout_is_reported(self):
        run = _simulate(
            _cli_batch()["body"],
            _outcomes(
                _release_install=3,
                **{"jacked service restart": _ACCESS_VIOLATION},
            ),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert "service restart step failed" in run.failed_file

    def test_cli_a_crashed_restart_with_no_rollback_is_reported(self):
        run = _simulate(
            _cli_batch(("--no-rollback",))["body"],
            _outcomes(package=1, **{"jacked service restart": _ACCESS_VIOLATION}),
        )
        assert run.exit_code == 1
        assert "service restart step failed" in run.failed_file

    def test_tray_a_crashed_start_of_the_previous_service_is_reported(self):
        run = _simulate(
            _tray_batch()["body"],
            _outcomes(_release_install=1, powershell=_ACCESS_VIOLATION),
        )
        assert run.exit_code == 1
        assert run.ran(_PACKAGE_MARK) == 0
        assert "service start step failed" in run.failed_file
        assert "running again" not in run.failed_file

    def test_tray_a_crashed_status_shim_is_logged_and_the_update_continues(self):
        run = _simulate(
            _tray_batch()["body"],
            _outcomes(
                **{"_update_status waiting_for_parent in_progress": _ACCESS_VIOLATION}
            ),
        )
        assert run.exit_code == 0
        assert any("_update_status shim returned non-zero" in e for e in run.echoes)


class TestTheTrayReleaseStepWaitsForTheTray:
    def test_the_release_line_carries_the_tray_identity_and_port(self):
        body = _tray_batch(port=9555)["body"]
        line = next(
            ln for ln in body.split("\r\n")
            if ln.startswith("jacked _release_install")
        )
        assert "--port 9555" in line
        assert f"--wait-process 4242:{TRAY_CREATION_ID}" in line

    def test_an_unreadable_identity_still_checks_the_port(self):
        def gone(_pid):
            raise ProcessLookupError(_pid)

        body = _tray_batch(identity=gone)["body"]
        line = next(
            ln for ln in body.split("\r\n")
            if ln.startswith("jacked _release_install")
        )
        assert "--wait-process" not in line
        assert "--port 8321" in line

    def test_an_identity_that_is_unsafe_for_cmd_is_not_written(self):
        from jacked.service.instance_models import ProcessIdentity

        def odd(pid):
            return ProcessIdentity(pid, "windows-filetime:1 & del x", "x")

        body = _tray_batch(identity=odd)["body"]
        assert "del x" not in body
        assert "--wait-process" not in body

    def test_the_cli_release_line_names_the_default_port(self):
        from jacked.service import DEFAULT_PORT

        body = _cli_batch()["body"]
        assert f"jacked _release_install --timeout 60 --port {DEFAULT_PORT}" in body
        assert "--wait-process" not in body


class TestTheCliBatchClosesTheStatusRecord:
    """The CLI batch claims the record, so every exit must close it."""

    def test_a_clean_run_marks_the_update_succeeded(self):
        run = _simulate(_cli_batch()["body"], _outcomes())
        assert run.exit_code == 0
        assert run.ran("jacked _update_status_succeed") == 1
        assert run.ran("_update_status_fail") == 0

    @pytest.mark.parametrize(
        ("args", "overrides", "reason"),
        [
            ((), {"_release_install": 1}, "still in use"),
            ((), {"_release_install": 3}, "did not release the install"),
            (
                (),
                {"_release_install": 3, "jacked service restart": 1},
                "did not restart",
            ),
            ((), {"package": 1}, "rolled back"),
            ((), {"package": 1, "rollback": 1}, "package rollback step"),
            ((), {"jacked service preflight": 1}, "preflight failed"),
            (("--no-rollback",), {"package": 1}, "after 3 attempts"),
            (
                ("--no-rollback",),
                {"package": 1, "jacked service restart": 1},
                "after 3 attempts",
            ),
            (("--no-rollback",), {"jacked service preflight": 1}, "disabled"),
            (("--skip-service",), {"_release_install": 1}, "still in use"),
        ],
    )
    def test_every_failure_exit_marks_the_update_failed(
        self, args, overrides, reason
    ):
        run = _simulate(_cli_batch(args)["body"], _outcomes(**overrides))
        assert run.exit_code == 1
        marks = [c for c in run.commands if "_update_status_fail" in c]
        assert len(marks) == 1, marks
        assert run.ran("jacked _update_status_succeed") == 0
        expanded = re.sub(
            r"%(\w+)%", lambda m: run.env.get(m.group(1), m.group(0)), marks[0]
        )
        assert reason in expanded, expanded
        # The mark is the last command before the exit.
        assert "_update_status_fail" in run.commands[-1]

    def test_another_updater_in_flight_does_not_touch_its_record(self):
        run = _simulate(_cli_batch()["body"], _outcomes(_update_status_init=2))
        assert run.exit_code == 2
        assert run.ran("_update_status_fail") == 0


class TestUpdateStatusFailCommand:
    def test_it_marks_the_record_failed(self, tmp_path, monkeypatch):
        import json

        from jacked.cli import main
        from jacked.service import update_status as us

        path = tmp_path / "update-status.json"
        path.write_text(json.dumps({"overall": "in_progress"}), encoding="utf-8")
        monkeypatch.setattr(us, "UPDATE_STATUS_FILE", path)
        result = CliRunner().invoke(
            main, ["_update_status_fail", "--error", "the install is in use"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["overall"] == "failed"
        assert data["error"] == "the install is in use"

    def test_a_missing_record_is_left_alone(self, tmp_path, monkeypatch):
        from jacked.cli import main
        from jacked.service import update_status as us

        path = tmp_path / "update-status.json"
        monkeypatch.setattr(us, "UPDATE_STATUS_FILE", path)
        result = CliRunner().invoke(main, ["_update_status_fail", "--error", "x"])
        assert result.exit_code == 0
        assert not path.exists()

    def test_the_command_is_hidden(self):
        from jacked.cli import main

        assert main.commands["_update_status_fail"].hidden
