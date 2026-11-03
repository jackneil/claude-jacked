"""The dashboard tells the user what a CC_REAUTH_REQUIRED refusal means (#149i).

The switch route answers the refusal with HTTP 422 and the body carries
`code`. The dashboard must act on the code (a notice that names the fix), not
only echo the server sentence. The notice must name a control that exists:
the "Authorize Claude Code Token" row of the account menu (DCR U-3). Every
path that renders a switch result shows the same notice: the direct answer,
and the result read back by the poll after a timed-out POST (DCR U-4).
After a sign-in, an activation the engine refused must not hide behind a
success banner. The copy under test is the copy that ships.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_web_js_account_menu import _actions, _item, _menu_panel
from tests.unit.test_web_js_swap_ui import _HARNESS

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
COMPONENTS = WEB_JS / "components"
ACTIONS_JS = COMPONENTS / "account-actions.js"
OAUTH_JS = COMPONENTS / "oauth-flows.js"
UTILS_JS = WEB_JS / "utils.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ENGINE_MESSAGE = "not publishing a null refresh token over a live one (cc re-auth required)"
REFUSAL = {"status": "unusable", "code": "CC_REAUTH_REQUIRED", "message": ENGINE_MESSAGE,
           "existing_sessions": "unverified"}


def _notice() -> str:
    src = ACTIONS_JS.read_text(encoding="utf-8")
    notice = re.search(r"const CC_REAUTH_REQUIRED_NOTICE = '([^']+)';", src)
    assert notice, "the notice constant is missing"
    return notice.group(1)


def _run(tmp_path, snippet):
    """Eval account-actions.js and utils.js in the swap-ui DOM harness."""
    program = (
        _HARNESS.replace("__TARGET__", json.dumps(str(ACTIONS_JS)))
        + "\neval(fs.readFileSync(%s, 'utf8'));\n" % json.dumps(str(UTILS_JS))
        + "global.sessionStorage = { getItem: () => null, setItem: () => {} };\n"
        + "loadActiveCredential = async () => {};\n"
        + snippet
    )
    script = tmp_path / "notice-harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@needs_node
@pytest.mark.parametrize("path", [ACTIONS_JS, OAUTH_JS], ids=lambda p: p.name)
def test_node_syntax_check(path):
    proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@needs_node
def test_the_notice_names_a_menu_row_that_exists(tmp_path):
    """U-3: the advertised recovery must be a control the user can find."""
    item = _item(_menu_panel(_actions(tmp_path)), "btn-authorize-cc")
    assert item is not None, "a Claude account menu offers the Claude Code token row"
    label = re.search(r"<span>([^<]+)</span>", item).group(1)
    assert label == "Authorize Claude Code Token"
    notice = _notice()
    assert f"account menu ({label})" in notice, "the notice names the exact row"
    assert "\u2014" not in notice


def test_the_notice_says_to_retry_once_before_the_authorization():
    """A retry can help: for about 10 s after a token rotation, the guard
    pass has not yet adopted the new token. Only a second refusal means the
    Claude Code token needs a new authorization."""
    notice = _notice()
    retry = notice.index("Try the switch again in a moment.")
    authorize = notice.index("If jacked refuses it again, authorize the Claude Code token")
    assert retry == 0 and retry < authorize
    assert notice.endswith(
        "A running session holds a working token, and jacked does not replace it."
    )


@needs_node
def test_the_shared_renderer_shows_the_notice_for_the_code(tmp_path):
    result = _run(tmp_path, """
showCredentialActivationResult(%s, 'a@b.com');
out(__getToasts());
""" % json.dumps(REFUSAL))
    assert result == [{"message": _notice(), "type": "warning", "duration": 10000}]


@needs_node
def test_the_direct_and_the_polled_paths_show_the_same_notice(tmp_path):
    """U-4: a timed-out POST reads the result back through the poll, and that
    result must not fall through to the raw engine sentence."""
    result = _run(tmp_path, r"""
(async () => {
    const ids = { actionId: 'act-1', operationId: 'op-1', pageSessionId: 'page-1' };
    global.api.post = async () => {
        const err = new Error(%(msg)s);
        err.status = 422;
        err.payload = %(refusal)s;
        throw err;
    };
    await requestSwitch('5', 'a@b.com', ids);
    const direct = __getToasts().splice(0);

    global.api.post = async () => { const err = new Error('Request timed out'); err.code = 'TIMEOUT'; throw err; };
    global.api.get = async () => ({ state: 'complete', operation_id: 'op-1', result: %(refusal)s });
    await requestSwitch('5', 'a@b.com', ids);
    const polled = __getToasts().splice(0);
    out({ direct, polled });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""" % {"msg": json.dumps(ENGINE_MESSAGE), "refusal": json.dumps(REFUSAL)})
    expected = [{"message": _notice(), "type": "warning", "duration": 10000}]
    assert result["direct"] == expected
    assert result["polled"] == expected
    assert all(ENGINE_MESSAGE not in t["message"] for t in result["polled"])


@needs_node
def test_other_unusable_results_keep_the_server_sentence(tmp_path):
    result = _run(tmp_path, """
showCredentialActivationResult({ status: 'unusable', message: 'store is locked' }, 'a@b.com');
out(__getToasts());
""")
    assert result == [{"message": "store is locked", "type": "warning", "duration": 7000}]


def test_a_refused_activation_after_a_sign_in_is_noticed():
    src = OAUTH_JS.read_text(encoding="utf-8")
    assert "function noticeRefusedActivation(poll)" in src
    assert "noticeRefusedActivation(poll);" in src
    ok = re.search(r"const ACTIVATION_OK = \[([^\]]+)\];", src)
    assert ok, "the accepted-outcome list is missing"
    accepted = {item.strip().strip("'") for item in ok.group(1).split(",")}
    assert accepted == {"committed", "committed_degraded", "observed_target_unfenced", "local_only"}
    assert "The account was saved, but it was not made active" in src
