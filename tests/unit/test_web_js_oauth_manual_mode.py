"""The OAuth banner says so when jacked opened no window (#149i).

In profile and incognito mode the launcher no longer falls back to the
system browser (it can hold a different account). Its result is then
"manual": nothing opened. The banner must not promise a window that never
came; it tells the user to open the link in the right browser profile or to
paste the code, and the raw link carries the same account caveat the
reopen path shows. Node runs the real component source. Skipped when node
is not installed.
"""

import json
import shutil
import subprocess

import pytest

from tests.unit.test_web_js_oauth_identity import ACCENT, AUTH_URL, DOM_STUB, OAUTH_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _build(tmp_path, identity):
    harness = (
        "const textDiv = makeEl('div');\n"
        f"buildOAuthCodeEntry(textDiv, {json.dumps(ACCENT)}, "
        f"{json.dumps(AUTH_URL)}, false, {json.dumps(identity)});\n"
        "process.stdout.write('\\n'+JSON.stringify(textAll(textDiv))+'\\n');\n"
    )
    script = tmp_path / "h.js"
    script.write_text(
        DOM_STUB + OAUTH_JS.read_text(encoding="utf-8") + "\n" + harness,
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def test_a_manual_result_says_no_window_opened(tmp_path):
    text = _build(tmp_path, {"email": "a@b.com", "browserMode": "manual", "flowId": "flow-1"})
    assert "jacked could not open a browser window for this account" in text
    assert "A browser window should open" not in text
    assert "window opened" not in text


def test_a_manual_result_offers_the_link_with_the_account_caveat(tmp_path):
    text = _build(tmp_path, {"email": "a@b.com", "browserMode": "manual", "flowId": "flow-1"})
    assert "Open the authorization page for a@b.com" in text
    assert "already signed in to" in text
    assert "Bring up the sign-in window" not in text  # nothing to bring up


def test_the_default_browser_result_keeps_the_plain_copy(tmp_path):
    text = _build(tmp_path, {"email": "a@b.com", "browserMode": "default", "flowId": "flow-1"})
    assert "A browser window should open on this machine" in text
    assert "could not open" not in text
    assert "already signed in to" not in text
