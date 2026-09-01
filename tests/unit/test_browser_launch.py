"""Tests for jacked/web/browser_launch.py — the re-auth browser launcher.

Re-auth opens the Claude authorize URL in a browser profile dedicated to the
account being re-authed, so each Claude login keeps its own cookies and a
re-auth is a single Authorize click. Three properties matter enough to pin:

* the argv is right for each browser family and mode (a wrong private-window
  flag silently opens a normal window in the wrong profile);
* an unknown identity (Add Account) never reuses a shared profile;
* every failure path still gets the user to a browser, because the alternative
  is an OAuth flow with no way to finish.

subprocess.Popen and webbrowser.open are patched in every test that reaches
them: this module's whole job is spawning a real browser.
"""

import doctest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jacked.web import browser_launch as bl
from jacked.web import window_focus
from jacked.web.browser_launch import (
    DEFAULT_MODE,
    MODES,
    BrowserSpec,
    LaunchResult,
    _app_bundle_for,
    build_command,
    find_browser,
    launch_argv,
    normalize_mode,
    open_auth_url,
    profile_dir_for,
    profile_slug,
    read_mode,
)

URL = "https://claude.com/cai/oauth/authorize?state=secret"

CHROME = BrowserSpec("Chrome", "/usr/bin/google-chrome", "chromium")
EDGE = BrowserSpec("Edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "chromium")
FIREFOX = BrowserSpec("Firefox", "/usr/bin/firefox", "firefox")


class FakeDb:
    """Minimal settings reader; ``raises=True`` models a broken DB."""

    def __init__(self, value=None, raises=False):
        self.value = value
        self.raises = raises

    def get_setting(self, key):
        if self.raises:
            raise RuntimeError("database is locked")
        return self.value


@pytest.fixture
def profile_root(tmp_path, monkeypatch):
    """Redirect PROFILE_ROOT so no test writes into the real ~/.claude."""
    root = tmp_path / "jacked-browser-profiles"
    monkeypatch.setattr(bl, "PROFILE_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# build_command: the argv per family and mode
# ---------------------------------------------------------------------------


def test_chromium_profile_command_pins_the_user_data_dir():
    cmd = build_command(CHROME, URL, "profile", Path("/tmp/p"))
    assert cmd[0] == CHROME.path
    assert f"--user-data-dir={Path('/tmp/p')}" in cmd
    assert "--no-first-run" in cmd
    assert "--no-default-browser-check" in cmd
    assert "--new-window" in cmd
    assert cmd[-1] == URL
    # A profile launch must never also be private, or the cookies die on exit.
    assert "--incognito" not in cmd


def test_chromium_incognito_command_uses_incognito():
    cmd = build_command(CHROME, URL, "incognito", None)
    assert cmd == [CHROME.path, "--incognito", "--new-window", URL]


def test_edge_incognito_command_uses_inprivate():
    """Edge ignores --incognito, so the wrong flag opens a normal window."""
    cmd = build_command(EDGE, URL, "incognito", None)
    assert cmd == [EDGE.path, "--inprivate", "--new-window", URL]


def test_firefox_profile_command_uses_profile_and_no_remote():
    cmd = build_command(FIREFOX, URL, "profile", Path("/tmp/p"))
    assert cmd == [
        FIREFOX.path, "-profile", str(Path("/tmp/p")), "-no-remote", "-new-window", URL,
    ]


def test_firefox_incognito_command_uses_private_window():
    cmd = build_command(FIREFOX, URL, "incognito", None)
    assert cmd == [FIREFOX.path, "-private-window", URL]


def test_build_command_refuses_the_default_mode():
    """"default" means hand the URL to the OS, not to an argv."""
    with pytest.raises(ValueError):
        build_command(CHROME, URL, "default", None)


def test_build_command_refuses_profile_mode_without_a_directory():
    """Chrome with no --user-data-dir would land in the user's real profile."""
    with pytest.raises(ValueError):
        build_command(CHROME, URL, "profile", None)


# ---------------------------------------------------------------------------
# profile_slug / profile_dir_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "account,expected",
    [
        (None, None),
        ({"email": "jack@example.com"}, "jack@example.com"),
        ({"email": "JACK@Example.COM"}, "jack@example.com"),
        ({"email": "  jack@example.com \n"}, "jack@example.com"),
        ({"email": "jack+work@example.com"}, "jack+work@example.com"),
        ({"email": "we/ird\\name@example.com"}, "we_ird_name@example.com"),
        ({"email": "a b@example.com"}, "a_b@example.com"),
        ({"email": "", "id": 12}, "account-12"),
        ({"email": None, "id": 12}, "account-12"),
        ({"email": ""}, None),
        ({}, None),
    ],
)
def test_profile_slug(account, expected):
    assert profile_slug(account) == expected


def test_slug_never_escapes_the_profile_root(profile_root):
    """An email is external input; path separators must not survive it."""
    path = profile_dir_for({"email": "../../etc/passwd@x.com"})
    assert path.parent == profile_root
    assert "/" not in path.name and "\\" not in path.name
    assert profile_root.resolve() in path.resolve().parents


def test_a_dots_only_email_does_not_name_the_parent_directory(profile_root):
    path = profile_dir_for({"email": "..", "id": 3})
    assert path == profile_root / "account-3"


def test_same_email_different_org_shares_one_profile(profile_root):
    """One claude.ai login covers every org that email belongs to, so the two
    accounts must not fight over separate cookie jars."""
    a = profile_dir_for({"id": 1, "email": "j@x.com", "organization_uuid": "org-1"})
    b = profile_dir_for({"id": 2, "email": "j@x.com", "organization_uuid": "org-2"})
    assert a == b


def test_different_emails_get_different_profiles(profile_root):
    a = profile_dir_for({"email": "a@x.com"})
    b = profile_dir_for({"email": "b@x.com"})
    assert a != b
    assert a.parent == b.parent == profile_root


def test_profile_dir_for_unknown_identity_is_none():
    assert profile_dir_for(None) is None
    assert profile_dir_for({"email": ""}) is None


# ---------------------------------------------------------------------------
# read_mode / normalize_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_read_mode_accepts_every_known_mode(mode):
    assert read_mode(FakeDb(mode)) == mode


def test_read_mode_accepts_the_json_quoted_form():
    """The generic settings endpoint stores JSON, so the dashboard writes the
    value with its quotes still on."""
    assert read_mode(FakeDb('"incognito"')) == "incognito"


@pytest.mark.parametrize("stored", ["", "  ", "nonsense", "PROFILE_MODE", None])
def test_read_mode_falls_back_on_junk(stored):
    assert read_mode(FakeDb(stored)) == DEFAULT_MODE


def test_read_mode_without_a_db():
    assert read_mode(None) == DEFAULT_MODE


def test_read_mode_survives_a_failing_db():
    """A settings read must never be the reason an OAuth flow cannot start."""
    assert read_mode(FakeDb(raises=True)) == DEFAULT_MODE


def test_normalize_mode_is_case_insensitive():
    assert normalize_mode("Incognito") == "incognito"


# ---------------------------------------------------------------------------
# find_browser: the env override
# ---------------------------------------------------------------------------


def test_env_override_wins_and_guesses_the_family(tmp_path, monkeypatch):
    exe = tmp_path / "firefox-nightly"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setenv(bl.BROWSER_ENV_VAR, str(exe))
    # The imported name is the real function: this binding was captured at
    # import time, before conftest's guard patched the module attribute.
    spec = find_browser()
    assert spec is not None
    assert spec.path == str(exe)
    assert spec.family == "firefox"


def test_env_override_ignored_when_the_path_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(bl.BROWSER_ENV_VAR, str(tmp_path / "nope"))
    with patch.object(bl, "_candidates", return_value=[CHROME]):
        assert find_browser() == CHROME


def test_find_browser_returns_none_when_nothing_is_installed(monkeypatch):
    with patch.object(bl, "_candidates", return_value=[]):
        monkeypatch.delenv(bl.BROWSER_ENV_VAR, raising=False)
        assert find_browser() is None


# ---------------------------------------------------------------------------
# open_auth_url: modes, fallbacks, and what actually gets spawned
# ---------------------------------------------------------------------------


def test_default_mode_hands_the_url_to_the_os(profile_root):
    with patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("default"))
    assert result == LaunchResult("default", None)
    wb.assert_called_once_with(URL)
    popen.assert_not_called()


def test_no_supported_browser_falls_back_to_the_os(profile_root):
    """find_browser is None here courtesy of the conftest guard."""
    with patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))
    assert result == LaunchResult("default", None)
    wb.assert_called_once_with(URL)
    popen.assert_not_called()


def test_profile_mode_spawns_the_browser_in_the_accounts_profile(profile_root):
    account = {"id": 4, "email": "Jack@Example.com"}
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, account, FakeDb("profile"))

    assert result == LaunchResult("profile", "Chrome")
    wb.assert_not_called()
    cmd = popen.call_args.args[0]
    expected_dir = profile_root / "jack@example.com"
    assert f"--user-data-dir={expected_dir}" in cmd
    assert cmd[-1] == URL
    assert expected_dir.is_dir(), "the profile directory is created before launch"


def test_profile_mode_detaches_the_browser(profile_root):
    """The browser must outlive the request that spawned it."""
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open"):
        open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    kwargs = popen.call_args.kwargs
    assert kwargs["close_fds"] is True
    assert "creationflags" in kwargs or kwargs.get("start_new_session") is True


def test_unknown_identity_downgrades_to_a_private_window(profile_root):
    """Add Account has no account yet. Reusing a shared profile would
    authorize whoever is already signed in there."""
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open"):
        result = open_auth_url(URL, None, FakeDb("profile"))

    assert result == LaunchResult("incognito", "Chrome")
    cmd = popen.call_args.args[0]
    assert "--incognito" in cmd
    assert not any(arg.startswith("--user-data-dir=") for arg in cmd)
    assert not profile_root.exists(), "no profile directory for an unknown identity"


def test_incognito_mode_never_creates_a_profile(profile_root):
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen") as popen, \
            patch.object(bl.webbrowser, "open"):
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("incognito"))

    assert result == LaunchResult("incognito", "Chrome")
    assert "--incognito" in popen.call_args.args[0]
    assert not profile_root.exists()


def test_a_failed_spawn_still_opens_a_browser(profile_root):
    """A missing DLL or a browser mid-upgrade must not strand the flow."""
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen", side_effect=OSError("boom")), \
            patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == LaunchResult("default", None)
    wb.assert_called_once_with(URL)


def test_the_authorize_url_is_never_logged(profile_root, caplog):
    """The URL carries PKCE state; logs are shipped around."""
    caplog.set_level("DEBUG")
    with patch.object(bl, "find_browser", return_value=CHROME), \
            patch.object(bl.subprocess, "Popen"), \
            patch.object(bl.webbrowser, "open"):
        open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert URL not in caplog.text
    assert "state=secret" not in caplog.text


def test_mode_is_read_from_the_database(profile_root):
    """The setting, not a caller argument, decides the mode."""
    db = MagicMock()
    db.get_setting.return_value = "default"
    with patch.object(bl.webbrowser, "open"):
        assert open_auth_url(URL, {"email": "j@x.com"}, db).mode == "default"
    db.get_setting.assert_called_once_with(bl.SETTING_KEY)


# ---------------------------------------------------------------------------
# _app_bundle_for / launch_argv: how each platform activates a new window
# ---------------------------------------------------------------------------

MAC_CHROME = BrowserSpec(
    "Chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "chromium",
)


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome.app",
        ),
        (
            "/Users/j/Applications/Firefox.app/Contents/MacOS/firefox",
            "/Users/j/Applications/Firefox.app",
        ),
        ("/usr/bin/google-chrome", None),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", None),
        # A bundle path with no executable inside it is not a launchable exe.
        ("/Applications/Firefox.app", None),
        # No .app segment at all, despite the macOS-looking suffix.
        ("/opt/thing/Contents/MacOS/thing", None),
    ],
)
def test_app_bundle_for(path, expected):
    assert _app_bundle_for(path) == expected


def test_darwin_launch_goes_through_open_so_the_window_is_activated():
    """``open -n -a`` asks Launch Services to activate the app; spawning the
    executable directly leaves the sign-in window behind the dashboard."""
    argv = launch_argv(MAC_CHROME, URL, "incognito", None, platform="darwin")
    assert argv[:5] == [
        "open", "-n", "-a", "/Applications/Google Chrome.app", "--args",
    ]
    # The flags survive intact, and the executable itself is dropped.
    assert argv[5:] == ["--incognito", "--new-window", URL]
    assert MAC_CHROME.path not in argv


def test_darwin_profile_launch_keeps_the_user_data_dir_after_args():
    argv = launch_argv(MAC_CHROME, URL, "profile", Path("/tmp/p"), platform="darwin")
    assert argv[4] == "--args"
    assert f"--user-data-dir={Path('/tmp/p')}" in argv[5:]
    assert argv[-1] == URL


def test_darwin_without_a_bundle_invokes_the_executable():
    """A Homebrew/binary install has no .app, so there is nothing to open."""
    argv = launch_argv(CHROME, URL, "incognito", None, platform="darwin")
    assert argv == build_command(CHROME, URL, "incognito", None)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_non_darwin_platforms_invoke_the_executable(platform):
    argv = launch_argv(MAC_CHROME, URL, "incognito", None, platform=platform)
    assert argv == build_command(MAC_CHROME, URL, "incognito", None)
    assert "open" not in argv


def test_launch_argv_defaults_to_this_platform():
    argv = launch_argv(CHROME, URL, "incognito", None)
    assert argv == build_command(CHROME, URL, "incognito", None)


# ---------------------------------------------------------------------------
# The Windows focus pass
# ---------------------------------------------------------------------------


def test_windows_starts_a_focus_thread_for_the_spawned_browser(profile_root, monkeypatch):
    """Windows denies foreground to a window opened by the tray service, so
    the launcher has to go raise it — on a thread, because the caller is
    answering an HTTP request."""
    monkeypatch.setattr(bl.sys, "platform", "win32")
    # subprocess.DETACHED_PROCESS only exists on Windows; the platform is
    # faked here so the Popen kwargs must be too, or Linux CI takes the
    # launch-failed fallback path instead of the one under test.
    monkeypatch.setattr(bl, "_detach_kwargs", lambda: {})
    with patch.object(bl, "find_browser", return_value=CHROME),             patch.object(bl.subprocess, "Popen") as popen,             patch.object(bl, "_bring_to_front_windows") as focus,             patch.object(bl.threading, "Thread") as thread,             patch.object(bl.webbrowser, "open"):
        popen.return_value.pid = 4321
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == LaunchResult("profile", "Chrome")
    kwargs = thread.call_args.kwargs
    assert kwargs["target"] is focus
    assert kwargs["args"] == (4321, CHROME.path)
    assert kwargs["daemon"] is True
    thread.return_value.start.assert_called_once()
    # The focus pass runs on the thread, not inline — a blocking poll here
    # would stall the API response for seconds.
    focus.assert_not_called()


def test_non_windows_platforms_start_no_focus_thread(profile_root, monkeypatch):
    monkeypatch.setattr(bl.sys, "platform", "darwin")
    with patch.object(bl, "find_browser", return_value=CHROME),             patch.object(bl.subprocess, "Popen"),             patch.object(bl, "_bring_to_front_windows") as focus,             patch.object(bl.threading, "Thread") as thread,             patch.object(bl.webbrowser, "open"):
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == LaunchResult("profile", "Chrome")
    thread.assert_not_called()
    focus.assert_not_called()


def test_a_failed_spawn_starts_no_focus_thread(profile_root, monkeypatch):
    """There is no window to raise when nothing launched."""
    monkeypatch.setattr(bl.sys, "platform", "win32")
    # subprocess.DETACHED_PROCESS only exists on Windows; the platform is
    # faked here so the Popen kwargs must be too, or Linux CI takes the
    # launch-failed fallback path instead of the one under test.
    monkeypatch.setattr(bl, "_detach_kwargs", lambda: {})
    with patch.object(bl, "find_browser", return_value=CHROME),             patch.object(bl.subprocess, "Popen", side_effect=OSError("boom")),             patch.object(bl.threading, "Thread") as thread,             patch.object(bl.webbrowser, "open") as wb:
        result = open_auth_url(URL, {"email": "j@x.com"}, FakeDb("profile"))

    assert result == LaunchResult("default", None)
    wb.assert_called_once_with(URL)
    thread.assert_not_called()


def test_bring_to_front_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(window_focus.sys, "platform", "darwin")
    assert window_focus.bring_to_front_windows(1234, "/usr/bin/google-chrome") == "unsupported"


@pytest.mark.parametrize(
    "windows,pid,exe,allow,expected",
    [
        # An exact pid match wins, and needs no title check.
        ([(11, 7, "whatever")], 7, "c:/chrome.exe", False, 11),
        ([(11, 7, "Sign in - Claude")], 99, "c:/chrome.exe", False, None),
        # The exe fallback is only allowed after the grace period.
        ([(11, 9, "Sign in - Claude")], 99, "c:/chrome.exe", False, None),
        ([(11, 9, "Sign in - Claude")], 99, "c:/chrome.exe", True, 11),
        # ...and never drags an unrelated window of the same browser forward.
        ([(11, 9, "Inbox - Chrome")], 99, "c:/chrome.exe", True, None),
        # A different browser's window is not ours either.
        ([(11, 9, "Sign in - Claude")], 99, "c:/edge.exe", True, None),
    ],
)
def test_match_window(windows, pid, exe, allow, expected):
    assert window_focus.match_window(
        windows, pid, exe, allow, image_of=lambda _pid: "C:/Chrome.exe"
    ) == expected


def test_window_focus_doctests():
    results = doctest.testmod(window_focus)
    assert results.failed == 0
