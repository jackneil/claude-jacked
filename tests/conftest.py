"""Shared fixtures for jacked tests."""

import re

import pytest
from unittest.mock import patch

from jacked.credentials.models import (
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)



@pytest.fixture(autouse=True)
def _no_real_service_processes(request, monkeypatch):
    """Tripwire: a test must never start the real jacked service or drive a
    real supervisor.

    On 2026-09-04 a `service restart` test spawned a detached tray from the
    repo venv against the developer's real ~/.claude and another wrote a
    launchd transition lock into the real ~/Library/LaunchAgents. Tests that
    deliberately exec a sandboxed stub opt out with ``@pytest.mark.real_process``.
    """
    import subprocess

    if request.node.get_closest_marker("real_process"):
        return
    real_popen = subprocess.Popen
    real_run = subprocess.run
    supervisors = ("launchctl", "systemctl", "schtasks", "schtasks.exe")

    def _text(args):
        if isinstance(args, (list, tuple)):
            return " ".join(str(part) for part in args)
        return str(args)

    def _refuse(args) -> None:
        """One predicate for every subprocess entry point.

        A supervisor binary anywhere in argv[0], or a jacked service
        lifecycle command (start/restart/preflight/recover/install) reached
        through any launcher, is refused. subprocess.call/check_call/
        check_output route through Popen, so guarding run and Popen covers
        them too.
        """
        text = _text(args)
        head = str(args[0]) if isinstance(args, (list, tuple)) and args else text.split(" ", 1)[0]
        first = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if first in supervisors:
            raise RuntimeError("test tried to drive a real supervisor: " + text)
        padded = f" {text} "
        after_service = " " + padded.split(" service ", 1)[1] if " service " in padded else ""
        if "jacked" in text and any(
            f" {verb} " in after_service
            for verb in ("start", "restart", "preflight", "recover", "install")
        ):
            raise RuntimeError("test tried to spawn a real jacked service: " + text)
        # `jacked install` rewrites the real ~/.claude/settings.json.
        if "jacked" in text and re.search(r"jacked(?:\.exe)?\"? install(?: |$)", padded):
            raise RuntimeError("test tried to run a real jacked install: " + text)

    class _GuardedPopen(real_popen):
        def __init__(self, args, *popen_args, **popen_kwargs):
            _refuse(args)
            super().__init__(args, *popen_args, **popen_kwargs)

    def _guarded_run(args, *run_args, **run_kwargs):
        _refuse(args)
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    monkeypatch.setattr(subprocess, "run", _guarded_run)


@pytest.fixture(autouse=True)
def _block_keychain_writes():
    """Prevent tests from mutating the real credential authority."""
    def launch_result(account, _db):
        account_id = int(account["id"])
        identity = CredentialIdentity(
            account_id=account_id,
            email=account.get("email"),
            organization_id=account.get("organization_uuid") or None,
        )
        return SwitchResult(
            operation_id="launch-test-operation",
            outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED,
            desired_default=IdentityAxis(account_id, "desired"),
            storage=IdentityAxis(account_id, "observed"),
            committed_authority=IdentityAxis(None, "uncommitted"),
            existing_session_activation=SessionActivationState.RESTART_REQUIRED,
            provider_verification=ProviderVerificationState.UNVERIFIED,
            observed_identity=identity,
            message="test credential observation",
        )

    with patch(
        "jacked.api.credential_helpers.write_platform_credentials",
        return_value=True,
    ), patch(
        "jacked.launch._activate_launch_credentials",
        side_effect=launch_result,
    ), patch(
        # prepare_account_dir asks the real Claude install which authority it
        # uses; keep every launch test on the deterministic global-activation
        # path instead of the developer's own machine.
        "jacked.launch.scoped_launch_needs_global_activation",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_claude_identity_cache():
    from jacked.credentials.runtime import clear_identity_cache

    clear_identity_cache()
    yield
    clear_identity_cache()


@pytest.fixture(autouse=True)
def _reset_keychain_latches():
    from jacked.credentials.macos_store import (
        clear_keychain_latches,
        reset_argv_fallback_warning,
    )

    clear_keychain_latches()
    reset_argv_fallback_warning()
    yield
    clear_keychain_latches()
    reset_argv_fallback_warning()


@pytest.fixture(autouse=True)
def _block_browser_open():
    """Prevent any test from opening a real browser window.

    The OAuth flow calls webbrowser.open() which pops up the Anthropic
    login page during test runs. Block it globally.
    """
    with patch("webbrowser.open", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _block_profile_browser_launch():
    """Prevent any test from spawning a real browser process.

    Browser-mode OAuth now launches a per-account browser profile through
    jacked.web.browser_launch, which uses subprocess.Popen rather than
    webbrowser.open. Reporting "no browser installed" sends open_auth_url
    down its webbrowser fallback, which the fixture above already blocks.
    Tests that exercise the launcher itself patch find_browser locally,
    which overrides this.
    """
    with patch("jacked.web.browser_launch.find_browser", return_value=None):
        yield
