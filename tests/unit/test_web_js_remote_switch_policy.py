"""Dashboard side of the credential-mutation scope gate.

The server is the real gate (it answers 403), but a remote dashboard that
cannot switch must not offer a button that always fails. These tests assert
the three pieces of that contract in the component source and, where the
existing node harness makes it cheap, in rendered output:

- ``loadActiveCredential`` stores ``allow_credential_activation`` and never
  locks the local UI because a GET failed;
- ``renderActionButtons`` renders a disabled, explained Use Account button
  when the flag is false;
- the accounts view carries one muted hint saying why.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_web_js_swap_ui import _HARNESS

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"
ACCOUNT_ACTIONS_JS = WEB_JS / "components" / "account-actions.js"
REMOTE_ACCESS_JS = WEB_JS / "components" / "remote-access.js"

EM_DASH = "—"

HINT_OFF = (
    "Account switching from this browser is off. Turn on remote access under "
    "Settings > Advanced on the host machine."
)
HINT_OUTSIDE = (
    "This browser's address is outside the remote access scope. Widen the "
    "scope under Settings > Advanced on the host machine."
)
SETTINGS_LOCAL_ONLY = (
    "Remote access settings can only be changed from the machine running "
    "jacked. Open the dashboard on the host to change them."
)



pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _run_js(tmp_path, snippet, js_file=ACCOUNTS_JS):
    program = _HARNESS.replace("__TARGET__", json.dumps(str(js_file))) + "\n" + snippet
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@pytest.mark.parametrize(
    "js_file", [ACCOUNTS_JS, ACCOUNT_ACTIONS_JS, REMOTE_ACCESS_JS], ids=lambda p: p.name
)
def test_node_syntax_check(js_file):
    proc = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# loadActiveCredential
# ---------------------------------------------------------------------------


def test_load_active_credential_stores_the_activation_flag():
    source = ACCOUNT_ACTIONS_JS.read_text(encoding="utf-8")
    loader = source.split("async function loadActiveCredential()", 1)[1][:1200]

    # Only an explicit false locks the UI: an older server that omits the
    # field must keep working.
    assert (
        "window.jackedState.credentialActivationAllowed = (data.allow_credential_activation !== false)"
        in loader
    )
    # A failed GET must never lock the local UI.
    catch_block = loader.split("catch", 1)[1]
    assert "window.jackedState.credentialActivationAllowed = true" in catch_block


def test_load_active_credential_behaviour(tmp_path):
    result = _run_js(tmp_path, """
(async () => {
    const results = {};
    global.api.get = async () => ({ account_id: 3, allow_credential_activation: false });
    await loadActiveCredential();
    results.denied = window.jackedState.credentialActivationAllowed;

    global.api.get = async () => ({ account_id: 3, allow_credential_activation: true });
    await loadActiveCredential();
    results.allowed = window.jackedState.credentialActivationAllowed;

    global.api.get = async () => ({ account_id: 3 });
    await loadActiveCredential();
    results.missingField = window.jackedState.credentialActivationAllowed;

    global.api.get = async () => { throw new Error('offline'); };
    await loadActiveCredential();
    results.onFailure = window.jackedState.credentialActivationAllowed;
    results.idOnFailure = window.jackedState.activeCredentialAccountId;
    out(results);
})();
""", js_file=ACCOUNT_ACTIONS_JS)

    assert result["denied"] is False
    assert result["allowed"] is True
    assert result["missingField"] is True
    assert result["onFailure"] is True
    assert result["idOnFailure"] is None


# ---------------------------------------------------------------------------
# renderActionButtons
# ---------------------------------------------------------------------------


def test_render_action_buttons_disables_use_account_when_denied(tmp_path):
    result = _run_js(tmp_path, """
const acct = { id: 7, email: 'a@x.com', is_active: 1, provider: 'claude' };
window.jackedState.activeCredentialAccountId = null;

window.jackedState.credentialActivationAllowed = false;
const denied = renderActionButtons(acct);

window.jackedState.credentialActivationAllowed = true;
const allowed = renderActionButtons(acct);

delete window.jackedState.credentialActivationAllowed;
const unset = renderActionButtons(acct);

out({ denied, allowed, unset });
""")

    denied = result["denied"]
    allowed = result["allowed"]
    unset = result["unset"]

    # Denied: still a Use Account button (no layout jump), but inert + explained.
    assert "btn-use-account" in denied
    assert "disabled" in denied
    assert 'aria-disabled="true"' in denied
    assert f'title="{HINT_OFF}"' in denied
    assert "opacity-50" in denied
    assert "cursor-not-allowed" in denied
    assert 'data-id="7"' in denied

    # Allowed: the plain button, unchanged.
    assert "btn-use-account" in allowed
    assert "disabled" not in allowed
    assert "aria-disabled" not in allowed
    assert "opacity-50" not in allowed
    assert "cursor-not-allowed" not in allowed

    # An unset flag (first paint, before the GET lands) must not disable.
    assert "disabled" not in unset

    # The disabled variant keeps the same size so the card row does not shift.
    assert "px-3 py-1.5" in denied and "px-3 py-1.5" in allowed
    assert EM_DASH not in denied


def test_render_action_buttons_flag_does_not_affect_the_active_badge(tmp_path):
    result = _run_js(tmp_path, """
const acct = { id: 7, email: 'a@x.com', is_active: 1 };
window.jackedState.activeCredentialAccountId = 7;
window.jackedState.credentialActivationAllowed = false;
out({ html: renderActionButtons(acct) });
""")
    assert "Active in Claude Code" in result["html"]
    assert "btn-use-account" not in result["html"]


# ---------------------------------------------------------------------------
# The one-line hint on the accounts view
# ---------------------------------------------------------------------------


def test_accounts_view_shows_one_hint_only_when_switching_is_denied(tmp_path):
    result = _run_js(tmp_path, """
// Cards pull helpers from sibling component files; the hint does not depend
// on any of them, so a flat stub keeps this focused on the hint itself.
for (const fn of ['renderTokenPills', 'renderUsageBar', 'renderActiveSessions',
                  'providerBadge', 'usageTextClass', 'timeAgoFromUnix',
                  '_usageUpdateCardDOM']) {
    global[fn] = () => '';
}
global.computeElapsedFraction5h = () => 0;
global.computeElapsedFraction7d = () => 0;
// The optional sub-panels guard on `typeof x === 'function'`; leaving them
// undefined is the real "not loaded yet" path.

const accounts = [
    { id: 1, email: 'a@x.com', is_active: 1, priority: 0 },
    { id: 2, email: 'b@x.com', is_active: 1, priority: 1 },
];
window.jackedState.activeCredentialAccountId = null;
localStorage.setItem('jacked_tip_dismissed', '1');

window.jackedState.credentialActivationAllowed = false;
const denied = renderAccounts(accounts);

window.jackedState.credentialActivationAllowed = true;
const allowed = renderAccounts(accounts);

out({ denied, allowed });
""")

    denied = result["denied"]
    assert denied.count(f">{HINT_OFF}<") == 1, "exactly one visible hint on the view"
    assert "text-slate-500" in denied
    assert EM_DASH not in HINT_OFF
    assert f">{HINT_OFF}<" not in result["allowed"]


# ---------------------------------------------------------------------------
# The click handler
# ---------------------------------------------------------------------------


def test_use_account_handler_refuses_to_start_a_disabled_switch():
    """Disabled buttons fire no click, but the flag can flip between the
    render and the click, so the handler guards too."""
    source = ACCOUNT_ACTIONS_JS.read_text(encoding="utf-8")
    handler = source.split(".btn-use-account", 1)[1][:700]

    assert "btn.disabled" in handler
    assert "aria-disabled" in handler
    assert handler.index("return") < handler.index("activateAccountFromDashboard")


# ---------------------------------------------------------------------------
# Remote-access confirmation copy
# ---------------------------------------------------------------------------


def test_remote_access_dialogs_warn_that_viewers_can_switch_accounts():
    """Enabling remote access is now the switch that grants credential
    mutation, so both confirmations must say so."""
    source = REMOTE_ACCESS_JS.read_text(encoding="utf-8")
    options = source.split("function _remoteAccessConfirmOptions", 1)[1].split(
        "// --- State machine", 1
    )[0]

    all_interfaces = options.split("if (pending.scope === 'all')", 1)[1].split(
        "// enabled + tailscale", 1
    )[0]
    tailscale = options.split("// enabled + tailscale", 1)[1]

    assert "switch your accounts and trigger upgrades" in all_interfaces
    assert "switching accounts and triggering upgrades" in tailscale
    assert EM_DASH not in options


# ---------------------------------------------------------------------------
# Reason-specific copy
# ---------------------------------------------------------------------------

def test_load_active_credential_stores_the_reason_too():
    source = ACCOUNT_ACTIONS_JS.read_text(encoding="utf-8")
    loader = source.split("async function loadActiveCredential()", 1)[1][:1600]

    assert "credentialActivationReason" in loader
    catch_block = loader.split("catch", 1)[1]
    assert "window.jackedState.credentialActivationAllowed = true" in catch_block
    assert "credentialActivationReason = null" in catch_block


def test_reason_specific_hints_are_defined_once_and_clean():
    source = ACCOUNTS_JS.read_text(encoding="utf-8")

    assert HINT_OFF in source
    assert HINT_OUTSIDE in source
    for sentence in (HINT_OFF, HINT_OUTSIDE):
        assert EM_DASH not in sentence
        # Declared once as a constant, then reused for title + hint.
        assert source.count(sentence) == 1


@pytest.mark.parametrize(
    "reason,expected",
    [("remote_access_off", HINT_OFF), ("outside_scope", HINT_OUTSIDE), (None, HINT_OFF)],
)
def test_render_action_buttons_title_follows_the_reason(tmp_path, reason, expected):
    result = _run_js(tmp_path, """
const acct = { id: 7, email: 'a@x.com', is_active: 1 };
window.jackedState.activeCredentialAccountId = null;
window.jackedState.credentialActivationAllowed = false;
window.jackedState.credentialActivationReason = __REASON__;
out({ html: renderActionButtons(acct) });
""".replace("__REASON__", json.dumps(reason)))

    assert f'title="{expected}"' in result["html"]
    assert "disabled" in result["html"]


@pytest.mark.parametrize(
    "reason,expected",
    [("remote_access_off", HINT_OFF), ("outside_scope", HINT_OUTSIDE)],
)
def test_accounts_view_hint_follows_the_reason(tmp_path, reason, expected):
    result = _run_js(tmp_path, """
for (const fn of ['renderTokenPills', 'renderUsageBar', 'renderActiveSessions',
                  'providerBadge', 'usageTextClass', 'timeAgoFromUnix',
                  '_usageUpdateCardDOM']) {
    global[fn] = () => '';
}
global.computeElapsedFraction5h = () => 0;
global.computeElapsedFraction7d = () => 0;
localStorage.setItem('jacked_tip_dismissed', '1');
window.jackedState.activeCredentialAccountId = null;
window.jackedState.credentialActivationAllowed = false;
window.jackedState.credentialActivationReason = __REASON__;
out({ html: renderAccounts([{ id: 1, email: 'a@x.com', is_active: 1 }]) });
""".replace("__REASON__", json.dumps(reason)))

    assert result["html"].count(f">{expected}<") == 1


# ---------------------------------------------------------------------------
# The click guard reads the real flag, not only the DOM attribute
# ---------------------------------------------------------------------------


def test_use_account_handler_checks_the_flag_and_the_attribute():
    source = ACCOUNT_ACTIONS_JS.read_text(encoding="utf-8")
    handler = source.split(".btn-use-account", 1)[1][:900]

    assert "window.jackedState.credentialActivationAllowed === false" in handler
    assert "btn.disabled" in handler
    assert handler.index("return") < handler.index("activateAccountFromDashboard")
    # There is no websocket event that carries a remote-access change, so the
    # comment must not claim one.
    assert "websocket" not in handler.lower()


# ---------------------------------------------------------------------------
# The poll loop must re-read the permission
# ---------------------------------------------------------------------------


def test_poll_loop_refreshes_the_activation_permission():
    """A scope change made at the host must reach an open remote page without
    a manual refresh, or it keeps offering a button that now 403s."""
    source = (WEB_JS / "app.js").read_text(encoding="utf-8")
    body = source.split("function startPolling()", 1)[1].split(
        "function stopPolling()", 1
    )[0]

    assert "loadActiveCredential()" in body
    assert body.index("loadActiveCredential()") < body.index("rerenderAccountsView()")


# ---------------------------------------------------------------------------
# Rate-limit copy reaches the user
# ---------------------------------------------------------------------------


def test_rate_limited_switch_surfaces_the_server_message():
    source = ACCOUNT_ACTIONS_JS.read_text(encoding="utf-8")

    assert "CREDENTIAL_SWITCH_RATE_LIMITED" in source
    block = source.split("CREDENTIAL_SWITCH_RATE_LIMITED", 1)[1][:700]
    assert "showToast(" in block
    assert "'warning', 8000" in block


# ---------------------------------------------------------------------------
# Remote-access settings controls are host-only
# ---------------------------------------------------------------------------


def test_remote_access_card_disables_controls_when_not_editable():
    source = REMOTE_ACCESS_JS.read_text(encoding="utf-8")

    assert SETTINGS_LOCAL_ONLY in source
    assert EM_DASH not in SETTINGS_LOCAL_ONLY
    card = source.split("function renderRemoteAccessCardHTML", 1)[1].split(
        "// --- Confirm dialog copy", 1
    )[0]
    assert "editable" in card
    assert "disabled" in card


def test_remote_access_state_records_editable_from_the_get():
    source = REMOTE_ACCESS_JS.read_text(encoding="utf-8")
    loader = source.split("async function renderRemoteAccessCard", 1)[1][:1200]

    # Absent field (older server) must not lock the host's own controls.
    assert "state.editable !== false" in loader


def test_remote_access_scope_picker_is_disabled_when_not_editable():
    source = REMOTE_ACCESS_JS.read_text(encoding="utf-8")
    picker = source.split("function _remoteAccessScopePickerHTML", 1)[1].split(
        "function _remoteAccessStatusHTML", 1
    )[0]

    assert "editable" in picker
    assert "disabled" in picker
