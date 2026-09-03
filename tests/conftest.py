"""Shared fixtures for jacked tests."""

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
    ):
        yield


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
