"""Add Account > Codex shows the fix that matches the refusal (DCR U-2).

The route answers a store jacked cannot manage (``"keyring"``, or ``"auto"``
with no usable ``auth.json``) with ``CODEX_STORE_UNSWITCHABLE``. The dashboard
used to replace every refusal with "run codex login": a keyring user did
that, the login wrote the keyring again, and the next add was refused again.
Now the banner shows the backend reason as it is, then the steps in order:
set the store to "file", run ``codex login``, add the account again. A remote
dashboard says that the steps happen on the computer that runs jacked.

Node runs the real component source; skipped when node is not installed.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_web_js_oauth_flow_guard import _run

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
OAUTH_JS = WEB / "js" / "components" / "oauth-flows.js"
CODEX_ADD_JS = WEB / "js" / "components" / "codex-add-flow.js"
INDEX_HTML = WEB / "index.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

REASON = (
    "Codex credentials are stored in the OS keyring (service 'Codex Auth'); set "
    'cli_auth_credentials_store = "file" in ~/.codex/config.toml so jacked can manage them'
)
LOGIN_REASON = "no ~/.codex/auth.json. Run `codex login` first."


def _run_codex(tmp_path, snippet):
    """The flow harness: oauth-flows.js, then codex-add-flow.js (index.html
    order), then the snippet."""
    return _run(tmp_path, CODEX_ADD_JS.read_text(encoding="utf-8") + "\n" + snippet)


def _refuse(tmp_path, code, message, hostname="localhost"):
    return _run_codex(tmp_path, r"""
(async () => {
    window.location.hostname = %(host)s;
    const toasts = [];
    global.showToast = (msg, type) => toasts.push({ msg, type });
    global.api.post = async (path) => {
        calls.post.push(path);
        const err = new Error(%(message)s);
        err.status = 400;
        err.code = %(code)s;
        throw err;
    };
    await startAddCodexFlow();
    out({ banner: textAll(statusEl), html: statusEl.innerHTML || '', toasts,
          posts: calls.post, guard: window.jackedState._accountActionInFlight });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""" % {"host": json.dumps(hostname), "message": json.dumps(message), "code": json.dumps(code)})


@needs_node
def test_a_store_refusal_shows_the_backend_reason_as_it_is(tmp_path):
    result = _refuse(tmp_path, "CODEX_STORE_UNSWITCHABLE", REASON)
    assert REASON in result["banner"]
    assert result["posts"] == ["/api/auth/accounts/add?provider=codex"]
    assert result["guard"] is False


@needs_node
def test_a_store_refusal_names_the_steps_in_order(tmp_path):
    banner = _refuse(tmp_path, "CODEX_STORE_UNSWITCHABLE", REASON)["banner"]
    steps = banner[banner.index(REASON) + len(REASON):]
    set_store = steps.index('cli_auth_credentials_store = "file"')
    config = steps.index("~/.codex/config.toml")
    login = steps.index("codex login")
    retry = steps.index("Add Account")
    assert config < login and set_store < login < retry
    # The generic login-only copy is what caused the loop.
    assert "Not signed in to Codex" not in banner


@needs_node
def test_a_local_dashboard_says_the_steps_happen_on_this_computer(tmp_path):
    banner = _refuse(tmp_path, "CODEX_STORE_UNSWITCHABLE", REASON)["banner"]
    assert "on this computer" in banner
    assert "computer that runs jacked" not in banner


@needs_node
def test_a_remote_dashboard_says_the_steps_happen_on_the_jacked_computer(tmp_path):
    banner = _refuse(
        tmp_path, "CODEX_STORE_UNSWITCHABLE", REASON, hostname="studio.tailnet.ts.net"
    )["banner"]
    assert "on the computer that runs jacked" in banner
    assert "on this computer" not in banner.replace("not on this computer", "")


@needs_node
def test_a_login_refusal_keeps_the_login_copy_and_names_the_computer(tmp_path):
    local = _refuse(tmp_path, "CODEX_LOGIN_REQUIRED", LOGIN_REASON)["banner"]
    assert "codex login" in local
    assert "cli_auth_credentials_store" not in local, "a file store needs no config change"
    assert "on this computer" in local
    remote = _refuse(
        tmp_path, "CODEX_LOGIN_REQUIRED", LOGIN_REASON, hostname="studio.tailnet.ts.net"
    )["banner"]
    assert "on the computer that runs jacked" in remote


@needs_node
def test_any_other_failure_shows_its_own_message(tmp_path):
    banner = _refuse(tmp_path, "NETWORK_ERROR", "Network error")["banner"]
    assert "Network error" in banner
    assert "codex login" not in banner


@needs_node
def test_the_banner_is_built_node_by_node(tmp_path):
    """The reason comes from the server; it must never reach innerHTML."""
    result = _refuse(tmp_path, "CODEX_STORE_UNSWITCHABLE", '<img src=x onerror=alert(1)>')
    assert "<img src=x onerror=alert(1)>" in result["banner"]
    assert "<img" not in result["html"]


@needs_node
@pytest.mark.parametrize("code", ["CODEX_STORE_UNSWITCHABLE", "CODEX_LOGIN_REQUIRED"])
@pytest.mark.parametrize("host", ["localhost", "studio.tailnet.ts.net"])
def test_no_em_dashes_in_the_refusal_copy(tmp_path, code, host):
    banner = _refuse(tmp_path, code, REASON, hostname=host)["banner"]
    assert "\u2014" not in banner


@needs_node
def test_with_no_banner_slot_the_toast_carries_the_whole_fix(tmp_path):
    result = _run_codex(tmp_path, r"""
(async () => {
    document.getElementById = () => null;
    const toasts = [];
    global.showToast = (msg, type) => toasts.push({ msg, type });
    global.api.post = async () => {
        const err = new Error(%s);
        err.code = 'CODEX_STORE_UNSWITCHABLE';
        throw err;
    };
    await startAddCodexFlow();
    out({ toasts, guard: window.jackedState._accountActionInFlight });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""" % json.dumps(REASON))
    assert len(result["toasts"]) == 1
    toast = result["toasts"][0]
    assert toast["type"] == "warning"
    assert REASON in toast["msg"]
    assert 'Set cli_auth_credentials_store = "file" in ~/.codex/config.toml.' in toast["msg"]
    assert "[object Object]" not in toast["msg"]
    assert result["guard"] is False


@needs_node
def test_a_successful_add_clears_the_banner_and_refreshes(tmp_path):
    result = _run_codex(tmp_path, r"""
(async () => {
    const toasts = [];
    global.showToast = (msg, type) => toasts.push({ msg, type });
    global.api.post = async () => ({ provider: 'codex', imported: true, email: 'a@b.com' });
    releaseRefresh();
    await startAddCodexFlow();
    out({ toasts, text: statusEl.textContent, refreshCalls,
          guard: window.jackedState._accountActionInFlight });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["toasts"] == [{"msg": "Added Codex account a@b.com", "type": "success"}]
    assert result["text"] == ""
    assert result["refreshCalls"] == 1
    assert result["guard"] is False



def test_the_flow_is_defined_once_in_its_own_module():
    module = CODEX_ADD_JS.read_text(encoding="utf-8")
    oauth = OAUTH_JS.read_text(encoding="utf-8")
    assert module.count("async function startAddCodexFlow()") == 1
    assert "startAddCodexFlow" not in oauth
    # The old copy replaced every refusal with the login-only sentence.
    assert "Not signed in to Codex" not in module + oauth


def test_the_dashboard_loads_the_module_after_its_helpers():
    """codex-add-flow.js calls buildOAuthBanner and isRemoteDashboard from
    oauth-flows.js, and account-actions.js calls startAddCodexFlow."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    oauth = scripts.index("/js/components/oauth-flows.js")
    codex = scripts.index("/js/components/codex-add-flow.js")
    actions = scripts.index("/js/components/account-actions.js")
    assert oauth < codex < actions
    assert scripts.count("/js/components/codex-add-flow.js") == 1


@needs_node
def test_the_module_reads_no_other_script_at_load_time(tmp_path):
    """Plain script tags, no bundler: loading the module alone, with none of
    the names it uses defined, must not throw."""
    script = tmp_path / "load-only.js"
    script.write_text(
        CODEX_ADD_JS.read_text(encoding="utf-8")
        + "\nprocess.stdout.write(typeof startAddCodexFlow);\n",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "function"
