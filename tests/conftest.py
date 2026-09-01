"""Shared fixtures for jacked tests."""

import pytest
from unittest.mock import patch



@pytest.fixture(autouse=True)
def _block_keychain_writes():
    """Prevent any test from writing to the real macOS Keychain.

    write_platform_credentials is imported by name in jacked.launch,
    so we must patch both the definition and the import site.
    """
    with patch(
        "jacked.api.credential_helpers.write_platform_credentials",
        return_value=True,
    ), patch(
        "jacked.launch.write_platform_credentials",
        return_value=True,
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
