"""Provider CLI sign-in opened in the account's browser profile.

The sample output below is the real shape the CLIs print (captured
2026-11-04 from codex-cli 0.164.0 and grok 1.0.50 run in a scratch home,
codes redacted). Codex colors the URL with ANSI escapes, so a URL match that
keeps the escape opens a broken address.
"""

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jacked.web import external_login as el
from jacked.web import profile_opener

CODEX_OUT = (
    "Follow these steps to sign in with ChatGPT using device code authorization:\n"
    "\n"
    "1. Open this link in your browser and sign in to your account\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n"
)
GROK_OUT = (
    "To sign in, open this URL in your browser:\n"
    "\n"
    "  https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH\n"
    "\n"
    "Waiting for authorization...\n"
)


class _Proc:
    def __init__(self, text: str, code=0):
        self.stdout = io.StringIO(text)
        self._code = code
        self.pid = 4242

    def poll(self):
        return self._code


@pytest.mark.parametrize("text, url", [
    (CODEX_OUT, "https://auth.openai.com/codex/device"),
    (GROK_OUT, "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"),
])
def test_the_watcher_opens_the_clean_url_once(text, url):
    opened = []
    with patch("jacked.web.browser_launch.open_auth_url",
               side_effect=lambda u, account, db: opened.append(u)):
        el._watch_for_url(_Proc(text + text), {"provider": "grok"}, db=None)
    assert opened == [url]


def test_the_watcher_stops_at_end_of_output_without_spinning():
    """A closed pipe returns "" at once. The loop must end, not spin to the deadline."""
    proc = _Proc("", code=None)
    with patch("jacked.web.browser_launch.open_auth_url") as opener, \
            patch.object(el.time, "time", side_effect=[0.0, 0.0, 1.0, 2.0, 999.0]):
        el._watch_for_url(proc, {"provider": "grok"}, db=None)
    opener.assert_not_called()


def test_an_unknown_provider_is_refused():
    with pytest.raises(el.LoginStartError):
        el.start_provider_login("claude", {"email": "a@x.com"}, db=None)


def test_a_missing_cli_is_refused(tmp_path):
    with patch.object(el, "find_bin", return_value=None), \
            pytest.raises(el.LoginStartError, match="not installed"):
        el.start_provider_login("grok", {"email": "a@x.com"}, db=None)


def test_the_cli_runs_with_its_resolved_path_and_device_auth(tmp_path, monkeypatch):
    monkeypatch.setattr("jacked.web.browser_launch.PROFILE_ROOT", tmp_path / "profiles")
    calls = {}

    def _popen(argv, **kwargs):
        calls["argv"] = argv
        calls["env"] = kwargs["env"]
        return _Proc(GROK_OUT)

    with patch.object(el, "find_bin", return_value="/home/u/.grok/bin/grok"), \
            patch.object(el.subprocess, "Popen", side_effect=_popen), \
            patch.object(el.threading, "Thread"):
        result = el.start_provider_login("grok", {"email": "g@x.com", "id": 3}, db=None,
                                         env={"PATH": "/usr/bin"})
    assert calls["argv"] == ["/home/u/.grok/bin/grok", "login", "--device-auth"]
    assert result["profile_login"] is True and result["provider"] == "grok"
    if sys.platform != "win32":
        opener = Path(calls["env"]["BROWSER"])
        assert opener.parent == tmp_path / "profiles" / "grok" / "g@x.com"
    else:
        assert "BROWSER" not in calls["env"]


def test_the_profile_opener_never_falls_back_to_the_system_browser(tmp_path):
    """The system browser would keep the login in the wrong profile."""
    with patch("jacked.web.browser_launch.find_browser", return_value=None), \
            patch("webbrowser.open") as system:
        status = profile_opener.open_in_profile(tmp_path / "p", "https://x.ai/device")
    system.assert_not_called()
    assert status != 0


def test_the_profile_opener_refuses_a_non_https_url(tmp_path):
    assert profile_opener.open_in_profile(tmp_path / "p", "file:///etc/passwd") == 2
