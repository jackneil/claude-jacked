import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_web_js_swap_ui import _HARNESS

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
UTILS_JS = WEB_JS / "utils.js"
ACCOUNT_ACTIONS_JS = WEB_JS / "components" / "account-actions.js"

UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_use_account_sends_distinct_idempotency_headers() -> None:
    """Ids come from ``generateUuid`` and are minted inside the ``try``.

    Regression 2026-09-11: ``crypto.randomUUID`` is secure-context only, so
    on ``http://<hostname>:8321`` (remote access on) the three id calls threw
    a ``TypeError`` *before* the ``try``, leaving the button stuck on
    "Switching..." and ``_accountActionInFlight`` latched true forever.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    minter = source.split("function mintSwitchIds()", 1)[1].split(
        "async function requestSwitch", 1
    )[0]
    assert "const actionId = generateUuid()" in minter
    assert "const operationId = generateUuid()" in minter
    assert "pageSessionId = generateUuid()" in minter
    assert "sessionStorage.getItem('jacked-page-session-id')" in minter
    assert "crypto.randomUUID()" not in source

    requester = source.split("async function requestSwitch", 1)[1].split(
        "async function activateAccountFromDashboard", 1
    )[0]
    assert "'X-Jacked-Action-Id': actionId" in requester
    assert "'X-Jacked-Operation-Id': operationId" in requester
    assert "'X-Jacked-Page-Session': pageSessionId" in requester
    assert "/api/auth/credential-operations/${actionId}" in source

    # The ids are minted INSIDE the outer try, after the latch is set and
    # before the POST that carries them.
    activate = source.index("async function activateAccountFromDashboard")
    flag_index = source.index("_accountActionInFlight = true", activate)
    try_index = source.index("try {", flag_index)
    mint_index = source.index("mintSwitchIds()", try_index)
    post_index = source.index("`/api/auth/accounts/${id}/use`", 0)
    assert flag_index < try_index < mint_index
    assert requester.index("const { actionId") < requester.index(
        "`/api/auth/accounts/${id}/use`"
    )
    assert post_index < activate  # the POST lives in requestSwitch


OUTER_CATCH_TOAST = (
    "showToast('Could not start the switch: '"
)


def test_use_account_restores_the_button_and_clears_the_in_flight_flag() -> None:
    """A failure must not strand the button or the in-flight latch."""
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    body = source.split("async function activateAccountFromDashboard", 1)[1].split(
        "function showAutoSwapRecommendation", 1
    )[0]

    assert "const originalButtonText = sourceButton" in body
    finally_block = body.split("} finally {", 1)[1]
    assert "sourceButton.disabled = false" in finally_block
    assert "sourceButton.textContent = originalButtonText" in finally_block
    restore_index = finally_block.index("sourceButton.disabled = false")
    clear_index = finally_block.index("_accountActionInFlight = false")
    assert restore_index < clear_index
    assert clear_index < finally_block.index("refreshAndRender()")


def test_use_account_reports_a_synchronous_failure_instead_of_swallowing_it() -> None:
    """The wrapper's own catch, not the API outcome handler, owns this."""
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    body = source.split("async function activateAccountFromDashboard", 1)[1].split(
        "function showAutoSwapRecommendation", 1
    )[0]

    outer_catch = body.rsplit("} catch (e) {", 1)[1].split("} finally {", 1)[0]
    assert "console.error('Account switch failed:', e)" in outer_catch
    assert OUTER_CATCH_TOAST in outer_catch
    assert "'error', 8000" in outer_catch

    # The API outcome handler must not be the thing that reports it.
    requester = source.split("async function requestSwitch", 1)[1].split(
        "async function activateAccountFromDashboard", 1
    )[0]
    assert OUTER_CATCH_TOAST not in requester


def test_use_account_gives_the_403_outcome_the_long_toast_and_a_final_fallback() -> None:
    """CREDENTIAL_MUTATION_LOCAL_ONLY is what most remote users hit.

    Its message is long, so it needs the same 8000 ms the sibling branches
    use, and the branch must never be able to render the literal
    ``undefined``.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    requester = source.split("async function requestSwitch", 1)[1].split(
        "async function activateAccountFromDashboard", 1
    )[0]
    fallback = requester.rsplit("} else {", 1)[1]

    assert (
        "showToast(outcome.message || e.message || "
        "'The switch could not be completed.', 'error', 8000)"
    ) in fallback


def test_finally_refresh_failure_is_reported_rather_than_unhandled() -> None:
    """A dropped GET over the tailnet must not become an unhandled rejection."""
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    body = source.split("async function activateAccountFromDashboard", 1)[1].split(
        "function showAutoSwapRecommendation", 1
    )[0]
    finally_block = body.split("} finally {", 1)[1]

    assert "await refreshAndRender().catch(" in finally_block
    assert "'The page could not refresh: '" in finally_block
    assert "'warning', 8000" in finally_block


def test_copy_command_fallback_is_reachable_without_navigator_clipboard() -> None:
    """``navigator.clipboard`` is undefined in a non-secure context.

    The property access must therefore happen *inside* the ``try`` so the
    resulting TypeError lands in the ``execCommand`` fallback.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    handler = source.split(".btn-copy-cmd", 1)[1].split("btn-dismiss-tip", 1)[0]

    try_index = handler.index("try {")
    assert try_index < handler.index("navigator.clipboard.writeText(cmd)")
    catch_block = handler.split("} catch {", 1)[1]
    assert "document.execCommand('copy')" in catch_block
    # User-facing copy: no em-dashes in the toasts (comments are exempt).
    toasts = [ln for ln in handler.splitlines() if "showToast(" in ln]
    assert toasts
    assert all("\u2014" not in ln for ln in toasts)
    assert "Copy failed. Run manually:" in handler


def test_use_account_ui_handles_truthful_outcomes_without_blanket_switched_claim() -> None:
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    for outcome in (
        "committed",
        "committed_degraded",
        "observed_target_unfenced",
        "interactive_required",
        "unsupported",
    ):
        assert outcome in source
    assert "showToast(`Switched to ${email}`" not in source
    assert "switches all Claude Code sessions" not in source


def test_api_client_preserves_structured_error_outcome_and_custom_headers() -> None:
    source = (WEB_JS / "app.js").read_text()

    assert "...headers" in source
    assert "apiError.payload = err" in source
    assert "/api/auth/credential-operations/${actionId}" in (
        WEB_JS / "components" / "account-actions.js"
    ).read_text()


def test_lost_response_polls_until_terminal_result_with_same_action_binding() -> None:
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    poller = source.split("async function pollCredentialOperation", 1)[1].split(
        "async function activateAccountFromDashboard", 1
    )[0]

    assert "while (Date.now() < deadline)" in poller
    assert "/api/auth/credential-operations/${actionId}" in poller
    assert "'X-Jacked-Page-Session': pageSessionId" in poller
    assert "operation.state === 'complete'" in poller
    assert "operation.state === 'expired'" in poller
    assert "statusError.status === 404" in poller
    assert "setTimeout(resolve, 1500)" in poller
    assert "showCredentialActivationResult(operation.result, email)" in poller

    timeout_branch = source.split("if (e.code === 'TIMEOUT')", 1)[1].split(
        "} else if", 1
    )[0]
    assert "await pollCredentialOperation(" in timeout_branch
    assert "actionId, operationId, pageSessionId, email" in timeout_branch


def test_auto_swap_recommendation_is_visible_and_uses_safe_activation_path() -> None:
    actions = (WEB_JS / "components" / "account-actions.js").read_text()
    websocket = (WEB_JS / "websocket.js").read_text()

    assert "jackedWS.on('auto_swap_recommended'" in websocket
    assert "showAutoSwapRecommendation(data)" in websocket
    assert "function showAutoSwapRecommendation(data)" in actions
    assert "Account switch recommended:" in actions
    assert "Use Account" in actions
    assert "await activateAccountFromDashboard(" in actions
    assert "banner.setAttribute('role', 'region')" in actions
    assert "banner.setAttribute('aria-label', 'Account switch recommendation')" in actions
    assert "text.setAttribute('aria-live', 'polite')" in actions
    assert "focus-visible:outline" in actions
    assert "close.setAttribute('aria-label', 'Dismiss account switch recommendation')" in actions
    assert "setTimeout(function() { if (banner.parentNode)" not in actions.split(
        "function showAutoSwapRecommendation(data)", 1
    )[1].split("function bindAccountEvents", 1)[0]


def test_use_account_ui_tells_the_user_whether_open_sessions_follow() -> None:
    """The headline is the switch; the second sentence is what open sessions do.

    Regression 2026-09-04: the engine wrote the Keychain but not the identity
    Claude Code watches, so every open session kept the old account while the
    toast said the switch was observed. The UI must key the session sentence
    on the API's ``existing_sessions`` field, never on the raw engine message.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    assert "function sessionsFollowCopy(" in source
    assert "result.existing_sessions === 'pending_next_activity'" in source
    assert "pick it up on their next message" in source
    assert "Restart them to use this account" in source
    # The engine's diagnostic text is not the user's headline.
    assert "concurrent writers cannot be excluded" not in source
    body = source.split("function showCredentialActivationResult")[1].split(
        "async function pollCredentialOperation"
    )[0]
    for outcome in ("committed", "committed_degraded", "observed_target_unfenced"):
        branch = body.split(f"result.status === '{outcome}'")[1].split("} else")[0]
        assert "sessionsFollowCopy(" in branch
        assert "result.message ||" not in branch



# ---------------------------------------------------------------------------
# Behavioral tests: the fix is a runtime invariant, so drive the real function
# under node with a non-secure-context crypto (getRandomValues, no randomUUID).
# ---------------------------------------------------------------------------
_STUBS = """
const { webcrypto } = require('node:crypto');
// A crypto WITHOUT randomUUID is exactly what a non-secure context (plain
// http on a hostname) hands the page.
global.crypto = { getRandomValues: (a) => webcrypto.getRandomValues(a) };
const _session = {};
global.sessionStorage = {
    getItem: (k) => Object.prototype.hasOwnProperty.call(_session, k) ? _session[k] : null,
    setItem: (k, v) => { _session[k] = String(v); },
};
const _posts = [];
let _refreshed = 0;
global.refreshAndRender = async () => { _refreshed++; };
loadActiveCredential = async () => {};
let _shownResults = 0;
showCredentialActivationResult = () => { _shownResults++; };
const btn = __makeEl('button');
btn.textContent = 'Use Account';
btn.disabled = false;
window.jackedState._accountActionInFlight = false;
const report = () => ({
    text: btn.textContent,
    disabled: btn.disabled,
    inFlight: window.jackedState._accountActionInFlight,
    posts: _posts.map(p => ({ url: p.url, headers: p.opts && p.opts.headers })),
    toasts: __getToasts(),
    refreshed: _refreshed,
    shownResults: _shownResults,
});
"""


def _run_switch(tmp_path, snippet):
    """Eval utils.js then account-actions.js in the swap-ui DOM harness."""
    program = (
        _HARNESS.replace("__TARGET__", json.dumps(str(ACCOUNT_ACTIONS_JS)))
        + "\neval(fs.readFileSync(%s, 'utf8'));\n" % json.dumps(str(UTILS_JS))
        + _STUBS
        + snippet
    )
    script = tmp_path / "switch-harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, (
        f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switch_over_plain_http_recovers_from_a_403_without_stranding_the_button(
    tmp_path,
) -> None:
    result = _run_switch(tmp_path, """
const MSG = 'Credential switching is local-only on this host. Open the dashboard on the Mac running jacked, or enable remote credential mutation, then try again.';
global.api.post = async (url, body, opts) => {
    _posts.push({ url, opts });
    const err = new Error(MSG);
    err.status = 403;
    err.code = 'CREDENTIAL_MUTATION_LOCAL_ONLY';
    err.payload = { error: { code: 'CREDENTIAL_MUTATION_LOCAL_ONLY' } };
    throw err;
};
activateAccountFromDashboard('5', 'x@y', btn).then(() => out(report()));
""")

    assert result["text"] == "Use Account"
    assert result["disabled"] is False
    assert result["inFlight"] is False
    assert result["refreshed"] == 1

    assert len(result["posts"]) == 1
    headers = result["posts"][0]["headers"]
    ids = [
        headers["X-Jacked-Action-Id"],
        headers["X-Jacked-Operation-Id"],
        headers["X-Jacked-Page-Session"],
    ]
    for value in ids:
        assert UUID_V4_RE.match(value), value
    assert len(set(ids)) == 3

    assert len(result["toasts"]) == 1
    toast = result["toasts"][0]
    assert "local-only" in toast["message"]
    assert toast["type"] == "error"
    assert toast["duration"] == 8000


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switch_over_plain_http_restores_the_button_on_the_success_path(
    tmp_path,
) -> None:
    result = _run_switch(tmp_path, """
global.api.post = async (url, body, opts) => {
    _posts.push({ url, opts });
    return { status: 'committed', existing_sessions: 'pending_next_activity' };
};
activateAccountFromDashboard('5', 'x@y', btn).then(() => out(report()));
""")

    assert result["text"] == "Use Account"
    assert result["disabled"] is False
    assert result["inFlight"] is False
    assert result["shownResults"] == 1
    assert result["refreshed"] == 1
    assert len(result["posts"]) == 1
    assert result["toasts"] == []
