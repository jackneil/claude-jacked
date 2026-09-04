"""A re-auth is two server flows: the primary sign-in, then the chained Claude
Code token flow the server auto-starts. The dashboard must keep polling the
second one so the account card updates when the token lands, not on the next
manual click.

Node runs the real component source; skipped when node is not installed.
"""

import shutil

import pytest

from tests.unit.test_web_js_oauth_flow_guard import _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_completed_primary_with_cc_flow_id_keeps_polling_the_chained_flow(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    const paths = [];
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1', account_id: 7, email: 'a@b.com' },
        { status: 'pending' },
        { status: 'completed', account_id: 7, email: 'a@b.com' },
    ];
    global.api.get = async (p) => { paths.push(p); calls.get++; return answers.shift() || { status: 'completed' }; };
    releaseRefresh();  // refreshes resolve immediately in this test

    startReauthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');            // primary: completed + cc_flow_id
    await tick(); await tick();
    const guardAfterPrimary = window.jackedState._accountActionInFlight;
    const refreshesAfterPrimary = refreshCalls;
    const bannerWhileChained = textAll(statusEl);
    const hooksWhileChained = (listeners.doc['visibilitychange'] || []).length;
    fireDoc('visibilitychange');            // chained: pending
    await tick();
    fireDoc('visibilitychange');            // chained: completed
    await tick(); await tick();
    out({ paths, guardAfterPrimary, refreshesAfterPrimary, bannerWhileChained, hooksWhileChained,
          refreshCalls, banner: textAll(statusEl),
          hooksAtEnd: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["paths"] == ["/api/auth/flow/f1", "/api/auth/flow/cc1", "/api/auth/flow/cc1"]
    assert result["guardAfterPrimary"] is False, "the primary verdict releases the guard"
    assert result["refreshesAfterPrimary"] == 1
    assert "Claude Code token" in result["bannerWhileChained"]
    assert result["hooksWhileChained"] == 1, "still polling while the chained flow runs"
    assert result["refreshCalls"] == 2, "the chained completion refreshes the accounts view again"
    assert "Claude Code token" in result["banner"] and "authorized" in result["banner"].lower()
    assert result["hooksAtEnd"] == 0


def test_chained_flow_failure_names_the_failure_but_keeps_the_reauth(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1' },
        { status: 'error', error: 'CC auth email mismatch' },
    ];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'error' }; };
    releaseRefresh();

    startReauthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    out({ refreshCalls, banner: textAll(statusEl), guard: window.jackedState._accountActionInFlight,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["refreshCalls"] == 2, "the card must show the stored state after a chained failure too"
    assert "re-authenticated" in result["banner"].lower()
    assert "CC auth email mismatch" in result["banner"]
    assert result["guard"] is False
    assert result["hooks"] == 0


def test_completed_flow_without_cc_flow_id_ends_as_before(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    global.api.get = async () => { calls.get++; return { status: 'completed' }; };
    releaseRefresh();
    startCcAuthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    out({ refreshCalls, gets: calls.get, banner: textAll(statusEl),
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result == {"refreshCalls": 1, "gets": 1, "banner": result["banner"], "hooks": 0}
    assert "authorized successfully" in result["banner"]


# The harness's fake nodes keep their children when textContent is cleared, so a
# rebuilt banner stacks on top of the old one. These helpers read the newest.
_FIND_ALL = r"""
function findAllAttr(el, attr, acc) {
    acc = acc || [];
    if (el._attrs && el._attrs[attr]) acc.push(el);
    (el.children || []).forEach(c => findAllAttr(c, attr, acc));
    return acc;
}
"""


def test_add_account_chain_keeps_the_add_flows_own_copy(tmp_path):
    """Adding an account chains too (purpose defaults to primary server-side),
    so the chained banners must not claim the account was re-authenticated, and
    the add flow's org-redirect notice has to survive the chain."""
    result = _run(tmp_path, r"""
(async () => {
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1', email: 'a@b.com',
          organization_name: 'Acme', redirected_from_account_id: 3 },
        { status: 'completed' },
    ];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'completed' }; };
    releaseRefresh();

    startAddAccountFlow();
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    const bannerWhileChained = textAll(statusEl);
    fireDoc('visibilitychange');
    await tick(); await tick();
    out({ bannerWhileChained, banner: textAll(statusEl), refreshCalls });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    interim = result["bannerWhileChained"]
    assert "you authorized a different org than selected" in interim, "the org-redirect notice survives"
    assert "Updated a@b.com (Acme)" in interim
    assert "Claude Code token" in interim
    assert "re-authenticated" not in interim.lower(), "an added account was never re-authenticated"
    assert "Updated a@b.com (Acme)" in result["banner"]
    assert "Claude Code token authorized!" in result["banner"]
    assert "re-authenticated" not in result["banner"].lower()
    assert result["refreshCalls"] == 2


def test_chained_flow_not_found_reports_the_sign_in_as_stored(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1' },
        { status: 'not_found' },
    ];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'not_found' }; };
    releaseRefresh();

    startReauthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    out({ refreshCalls, banner: textAll(statusEl), guard: window.jackedState._accountActionInFlight,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    banner = result["banner"]
    assert "Account re-authenticated successfully!" in banner, "the sign-in itself is stored"
    assert "the token authorization flow was not found" in banner
    assert "authorize the Claude Code token from the account menu" in banner
    assert "Re-auth flow not found" not in banner, "the primary's copy would be a lie here"
    assert result["refreshCalls"] == 2
    assert result["guard"] is False
    assert result["hooks"] == 0


def test_cancel_while_chained_says_the_account_is_stored(tmp_path):
    result = _run(tmp_path, _FIND_ALL + r"""
(async () => {
    global.api.get = async () => { calls.get++; return { status: 'completed', cc_flow_id: 'cc1' }; };
    releaseRefresh();

    startReauthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    const buttons = findAllAttr(statusEl, 'data-oauth-cancel');
    (buttons[buttons.length - 1]._listeners['click'] || []).forEach(cb => cb());
    await tick(); await tick();
    out({ cancelButtons: buttons.length, banner: textAll(statusEl),
          guard: window.jackedState._accountActionInFlight,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["cancelButtons"] == 2, "the chained banner gets a Cancel button of its own"
    assert "Account re-authenticated successfully!" in result["banner"]
    assert "The Claude Code token step was cancelled" in result["banner"]
    assert "authorize the Claude Code token from the account menu" in result["banner"]
    assert result["guard"] is False
    assert result["hooks"] == 0
