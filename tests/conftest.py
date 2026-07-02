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
