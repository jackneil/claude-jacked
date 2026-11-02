"""#149i: profile and private-window sign-ins never use the system browser.

The user picks a per-account profile or a private window to keep each Claude
sign-in apart. The system default browser can hold a different Claude account,
so opening it when the chosen browser cannot start can authorize the wrong
account. When jacked cannot open the chosen window, it opens nothing and
reports "manual", and the dashboard shows the sign-in link instead.

The "default" mode still hands the URL to the system browser
(test_browser_launch.py::test_default_mode_hands_the_url_to_the_os).
"""

from unittest.mock import patch

import pytest

from jacked.web import browser_launch as bl
from jacked.web.browser_launch import (
    DEFAULT_MODE,
    MODES,
    LaunchResult,
    normalize_mode,
    open_auth_url,
)
from tests.unit.test_browser_launch import CHROME, URL, FakeDb

MANUAL = LaunchResult("manual", None)


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    """Redirect PROFILE_ROOT so no test writes into the real ~/.claude."""
    root = tmp_path / "jacked-browser-profiles"
    monkeypatch.setattr(bl, "PROFILE_ROOT", root)
    return root


@pytest.mark.parametrize("mode", ["profile", "incognito"])
def test_no_supported_browser_reports_manual_and_opens_nothing(profile_root, mode):
    with patch.object(bl, "find_browser", return_value=None), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb(mode))

    assert result == MANUAL
    wb.assert_not_called()
    popen.assert_not_called()
    assert not profile_root.exists()


@pytest.mark.parametrize(
    "mode, account",
    [("profile", {"email": "j@x.com"}), ("profile", None), ("incognito", {"email": "j@x.com"})],
    ids=["profile", "add-account", "incognito"],
)
def test_a_failed_launch_reports_manual_and_opens_nothing(profile_root, mode, account):
    """A missing DLL or a browser mid-upgrade gives the dashboard link, not
    the system browser."""
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen", side_effect=OSError("boom")), \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, account, FakeDb(mode))

    assert result == MANUAL
    wb.assert_not_called()


def test_an_unusable_profile_directory_reports_manual(profile_root):
    """A file where the profile root must be makes the mkdir fail for real."""
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    profile_root.write_text("not a directory")
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == MANUAL
    popen.assert_not_called()
    wb.assert_not_called()


def test_a_failed_spawn_starts_no_focus_thread(profile_root, monkeypatch):
    """There is no window to raise when nothing launched."""
    monkeypatch.setattr(bl.sys, "platform", "win32")
    # subprocess.DETACHED_PROCESS only exists on Windows; the platform is
    # faked here so the Popen kwargs must be too.
    monkeypatch.setattr(bl, "_detach_kwargs", lambda: {})
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen", side_effect=OSError("boom")), \
            patch.object(bl.threading, "Thread") as thread, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == MANUAL
    wb.assert_not_called()
    thread.assert_not_called()


def test_the_launch_failure_log_names_the_mode_and_the_error_type_only(profile_root, caplog):
    """The error text can name the profile path, and the path holds the email."""
    leak = OSError("[Errno 13] Permission denied: '/h/jacked-browser-profiles/j@x.com'")
    with caplog.at_level("DEBUG", logger="jacked.oauth.browser"), \
            patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen", side_effect=leak), \
            patch.object(bl.webbrowser, "open"):
        open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert "mode=profile" in caplog.text
    assert "launch failed with OSError" in caplog.text
    assert "j@x.com" not in caplog.text
    assert "Permission denied" not in caplog.text
    assert "state=secret" not in caplog.text


def test_the_launch_log_names_the_account_id_not_the_profile_path(profile_root, caplog):
    """The profile directory is named for the email, so the log carries the id."""
    with caplog.at_level("INFO", logger="jacked.oauth.browser"), \
            patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen"), \
            patch.object(bl.webbrowser, "open"):
        result = open_auth_url(URL, {"id": 4, "email": "j@x.com"}, FakeDb("profile"))

    assert result == LaunchResult("profile", "Chrome")
    assert "mode=profile browser=Chrome account=4" in caplog.text
    assert "j@x.com" not in caplog.text


def test_the_no_browser_log_names_the_mode_and_the_reason(profile_root, caplog):
    with caplog.at_level("INFO", logger="jacked.oauth.browser"), \
            patch.object(bl, "find_browser", return_value=None), \
            patch.object(bl.webbrowser, "open"):
        open_auth_url(URL, {"email": "j@x.com"}, FakeDb("incognito"))

    assert "mode=incognito" in caplog.text
    assert "no supported browser" in caplog.text
    assert "state=secret" not in caplog.text


def test_manual_is_a_result_never_a_setting():
    """A stored "manual" must not switch the launcher off: it is not a mode
    the user can choose, so it reads as the default."""
    assert "manual" not in MODES
    assert normalize_mode("manual") == DEFAULT_MODE
