"""The dashboard's Grok flows (js/components/grok-add-flow.js).

Add Account > Grok imports the signed-in Grok login. When nobody is signed
in, the server opens the Grok sign-in in a browser profile and says so. The
Grok Re-auth row asks the server to open that sign-in for one account. A
refusal shows the server's reason as it is, node by node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_web_js_oauth_flow_guard import _run

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
GROK_JS = WEB / "js" / "components" / "grok-add-flow.js"
INDEX_HTML = WEB / "index.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run_grok(tmp_path, body, post):
    return _run(tmp_path, GROK_JS.read_text(encoding="utf-8") + "\n" + r"""
(async () => {
    const toasts = [];
    global.showToast = (msg, type) => toasts.push({ msg, type });
    global.api.post = %(post)s;
    releaseRefresh();
    %(body)s
    out({ toasts, text: textAll(statusEl), html: statusEl.innerHTML || '', posts: calls.post,
          refreshCalls, guard: window.jackedState._accountActionInFlight });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""" % {"post": post, "body": body})


@needs_node
def test_an_imported_grok_account_is_announced_and_rendered(tmp_path):
    result = _run_grok(tmp_path, "await startAddGrokFlow();", """async (path) => {
        calls.post.push(path);
        return { provider: 'grok', imported: true, email: 'g@x.com' };
    }""")
    assert result["posts"] == ["/api/auth/accounts/add?provider=grok"]
    assert result["toasts"] == [{"msg": "Added Grok account g@x.com", "type": "success"}]
    assert result["refreshCalls"] == 1
    assert result["guard"] is False


@needs_node
def test_a_profile_sign_in_shows_the_server_message(tmp_path):
    result = _run_grok(tmp_path, "await startAddGrokFlow();", """async (path) => {
        calls.post.push(path);
        return { profile_login: true, needs_login: true, provider: 'grok',
                 message: 'The Grok sign-in opened. jacked adds the account when the sign-in is done.' };
    }""")
    assert "jacked adds the account when the sign-in is done." in result["text"]
    assert result["refreshCalls"] == 0
    assert result["guard"] is False


@needs_node
def test_a_refusal_shows_the_reason_and_never_uses_inner_html(tmp_path):
    result = _run_grok(tmp_path, "await startAddGrokFlow();", """async (path) => {
        calls.post.push(path);
        const err = new Error('Run `grok login` <b>on</b> the host.');
        err.code = 'GROK_LOGIN_REQUIRED';
        throw err;
    }""")
    assert "Run `grok login` <b>on</b> the host." in result["text"]
    assert "<b>" not in result["html"]
    assert result["guard"] is False


@needs_node
def test_reauth_posts_to_the_account_and_shows_the_message(tmp_path):
    result = _run_grok(tmp_path, "await startGrokReauthFlow('7', 'g@x.com');", """async (path) => {
        calls.post.push(path);
        return { profile_login: true, provider: 'grok', message: 'Finish the sign-in there.' };
    }""")
    assert result["posts"] == ["/api/auth/accounts/7/reauth"]
    assert "Finish the sign-in there." in result["text"]


@needs_node
def test_a_second_sign_in_while_one_runs_shows_why(tmp_path):
    result = _run_grok(tmp_path, "await startGrokReauthFlow('7', 'g@x.com');", """async (path) => {
        calls.post.push(path);
        const err = new Error('A Grok sign-in is already open.');
        err.code = 'GROK_LOGIN_IN_PROGRESS';
        throw err;
    }""")
    assert "A Grok sign-in is already open." in result["text"]


@needs_node
def test_a_pushed_success_is_announced_and_rendered(tmp_path):
    result = _run_grok(tmp_path, """await handleProviderLoginFinished({ payload: {
        provider: 'grok', ok: true, message: 'Signed in to Grok as n@x.com.' } });""",
        "async () => ({})")
    assert result["toasts"] == [{"msg": "Signed in to Grok as n@x.com.", "type": "success"}]
    assert result["refreshCalls"] == 1


@needs_node
def test_a_pushed_failure_shows_the_reason_in_the_banner(tmp_path):
    result = _run_grok(tmp_path, """await handleProviderLoginFinished({ payload: {
        provider: 'grok', ok: false, message: 'You signed in as <b>b@x.com</b>.' } });""",
        "async () => ({})")
    assert "You signed in as <b>b@x.com</b>." in result["text"]
    assert "<b>" not in result["html"]
    assert result["refreshCalls"] == 1


@needs_node
def test_a_push_for_another_provider_is_ignored(tmp_path):
    result = _run_grok(tmp_path, """await handleProviderLoginFinished({ payload: {
        provider: 'codex', ok: true, message: 'x' } });""", "async () => ({})")
    assert result["toasts"] == [] and result["refreshCalls"] == 0


def test_the_websocket_routes_the_push_to_the_grok_handler():
    ws = (WEB / "js" / "websocket.js").read_text(encoding="utf-8")
    assert "jackedWS.on('provider_login_finished'" in ws
    assert "handleProviderLoginFinished(msg)" in ws


def test_no_em_dashes_in_the_grok_copy():
    assert "—" not in GROK_JS.read_text(encoding="utf-8")


def test_the_dashboard_loads_the_module_before_account_actions():
    html = INDEX_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    oauth = scripts.index("/js/components/oauth-flows.js")
    grok = scripts.index("/js/components/grok-add-flow.js")
    actions = scripts.index("/js/components/account-actions.js")
    assert oauth < grok < actions
    assert scripts.count("/js/components/grok-add-flow.js") == 1


@needs_node
def test_the_module_reads_no_other_script_at_load_time(tmp_path):
    script = tmp_path / "load-only.js"
    script.write_text(
        GROK_JS.read_text(encoding="utf-8")
        + "\nprocess.stdout.write(typeof startAddGrokFlow + ' ' + typeof startGrokReauthFlow + ' ' + typeof handleProviderLoginFinished);\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "function function function"
    assert json.dumps  # keep the import used
