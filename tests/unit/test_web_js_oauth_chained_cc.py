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
    assert "click Authorize Claude Code Token in the account menu" in banner
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
    assert "click Authorize Claude Code Token in the account menu" in result["banner"]
    assert result["guard"] is False
    assert result["hooks"] == 0


def test_chaining_re_arms_the_poll_interval(tmp_path):
    """refreshAndRender goes through renderRoute, which clears
    window.jackedState.flowPolling, so chaining without re-arming would leave the
    chained flow polled only when the tab regains focus (and its timeout dead)."""
    result = _run(tmp_path, r"""
(async () => {
    const cleared = [];
    let nextId = 0;
    global.setInterval = () => ++nextId;
    global.clearInterval = (id) => { cleared.push(id); };
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1' },
        { status: 'completed' },
    ];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'completed' }; };
    releaseRefresh();

    startReauthFlow(7, 'a@b.com');
    await tick();
    const startsBeforeChain = nextId;
    fireDoc('visibilitychange');
    await tick(); await tick();
    out2 = { startsBeforeChain, startsAfterChain: nextId,
             pollingAfterChain: window.jackedState.flowPolling,
             hooksAfterChain: (listeners.doc['visibilitychange'] || []).length };
    fireDoc('visibilitychange');
    await tick(); await tick();
    out2.clearedLast = cleared[cleared.length - 1];
    out2.pollingAtEnd = window.jackedState.flowPolling;
    out2.hooksAtEnd = (listeners.doc['visibilitychange'] || []).length;
    out(out2);
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["startsBeforeChain"] == 1
    assert result["startsAfterChain"] == 2, "the chained flow gets its own interval"
    assert result["pollingAfterChain"] == 2, "the new interval is the one renderRoute would clean up"
    assert result["hooksAfterChain"] == 1, "the visibility hooks are registered once, not twice"
    assert result["clearedLast"] == 2, "the flow's terminal exit clears the interval it is actually using"
    assert result["pollingAtEnd"] is None
    assert result["hooksAtEnd"] == 0


def test_a_failed_refresh_still_renders_a_verdict_and_frees_the_guard(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    const answers = [
        { status: 'completed', cc_flow_id: 'cc1' },
        { status: 'not_found' },
    ];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'not_found' }; };
    let refreshes = 0;
    global.refreshAndRender = async () => {
        refreshes++;
        if (refreshes > 1) throw new Error('network down');
    };

    startReauthFlow(7, 'a@b.com');
    await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    fireDoc('visibilitychange');
    await tick(); await tick();
    out({ refreshes, banner: textAll(statusEl), guard: window.jackedState._accountActionInFlight,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["refreshes"] == 2, "the chained exit still tries to refresh"
    assert "the token authorization flow was not found" in result["banner"], "the verdict survives"
    assert "reload the page to see the stored state" in result["banner"]
    assert result["guard"] is False, "a failed refresh must not leave every account action refused"
    assert result["hooks"] == 0


# ---------------------------------------------------------------------------
# The chained step gets the same controls as the primary one (DCR U-5)
#
# When the launcher opens no window for the Claude Code token step (no
# supported browser, or it did not start), the chained banner used to show a
# spinner and Cancel only: no link, no code field. The first chained poll
# carries the chained flow's own auth_url and browser_mode, and the banner now
# renders the same entry the primary flow renders, from those fields.
# ---------------------------------------------------------------------------

_CHAINED_ENTRY = r"""
function findAll(el, pred, acc) {
    acc = acc || [];
    if (pred(el)) acc.push(el);
    (el.children || []).forEach(c => findAll(c, pred, acc));
    return acc;
}
const lastBanner = () => statusEl.children[statusEl.children.length - 1];
const CC_URL = 'https://claude.com/cai/oauth/authorize?step=cc';
const PRIMARY_URL = 'https://claude.com/cai/oauth/authorize?step=primary';
const posts = [];
global.api.post = async (p) => {
    posts.push(p);
    if (p.endsWith('/code')) return { status: 'completed', flow_id: 'cc1', account_id: 7 };
    if (p.endsWith('/open')) return { status: 'pending', flow_id: 'cc1' };
    return { flow_id: 'f1', auth_url: PRIMARY_URL, mode: 'browser' };
};
function chainedPending(extra) {
    return Object.assign({ status: 'pending', flow_id: 'cc1', mode: 'browser',
                           purpose: 'claude_code', auth_url: CC_URL,
                           target_email: 'a@b.com' }, extra || {});
}
async function startAndChain(chainedPolls) {
    const answers = [{ status: 'completed', flow_id: 'f1', cc_flow_id: 'cc1', email: 'a@b.com' }]
        .concat(chainedPolls);
    global.api.get = async () => { calls.get++; return answers.shift() || chainedPending(); };
    releaseRefresh();
    startReauthFlow(7, 'a@b.com');
    await tick();
    for (let i = 0; i < 1 + chainedPolls.length; i++) {
        fireDoc('visibilitychange');
        await tick(); await tick();
    }
    return lastBanner();
}
"""


def test_no_window_chained_step_renders_link_copy_and_code_field(tmp_path):
    result = _run(tmp_path, _CHAINED_ENTRY + r"""
(async () => {
    const banner = await startAndChain([
        chainedPending({ browser_mode: 'manual' }),
        chainedPending({ browser_mode: 'manual' }),
    ]);
    const inputs = findAll(banner, e => e.tagName === 'input');
    out({ text: textAll(banner),
          hrefs: findAll(banner, e => e.tagName === 'a').map(a => a.href),
          inputs: inputs.length, inputName: inputs[0] && inputs[0].name,
          reopen: findAll(banner, e => e.textContent === 'Bring up the sign-in window').length,
          cancel: findAll(banner, e => e._attrs && e._attrs['data-oauth-cancel']).length,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    text = result["text"]
    assert "Authorizing the Claude Code token" in text, "the waiting banner keeps its title"
    assert "jacked could not open a browser window for this account" in text
    assert "Open the authorization page for a@b.com" in text
    assert "already signed in to" in text
    assert result["hrefs"] == ["https://claude.com/cai/oauth/authorize?step=cc"], "the chained URL, not the primary's"
    assert result["inputs"] == 1, "one code field, even after a second pending poll"
    assert result["inputName"] == "oauth-authorization-code"
    assert result["reopen"] == 0, "no window opened, so there is nothing to bring up"
    assert result["cancel"] == 1, "the chained Cancel survives"
    assert result["hooks"] == 1, "still polling"


def test_a_code_pasted_into_the_chained_entry_goes_to_the_chained_flow(tmp_path):
    result = _run(tmp_path, _CHAINED_ENTRY + r"""
(async () => {
    const banner = await startAndChain([chainedPending({ browser_mode: 'manual' })]);
    const input = findAll(banner, e => e.tagName === 'input')[0];
    input.value = 'the-code#state';
    const submit = findAll(banner, e => e.tagName === 'button' && e.textContent === 'Submit code')[0];
    (submit._listeners['click'] || []).forEach(cb => cb());
    await tick(); await tick();
    out({ posts, banner: textAll(lastBanner()), refreshCalls,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert "/api/auth/flow/cc1/code" in result["posts"]
    assert "/api/auth/flow/f1/code" not in result["posts"]
    assert "Claude Code token authorized!" in result["banner"]
    assert result["refreshCalls"] == 2
    assert result["hooks"] == 0


def test_profile_chained_step_renders_the_reopen_button_for_the_chained_flow(tmp_path):
    result = _run(tmp_path, _CHAINED_ENTRY + r"""
(async () => {
    const banner = await startAndChain([
        chainedPending({ browser_mode: 'profile', browser_name: 'Chrome' }),
    ]);
    const reopen = findAll(banner, e => e.textContent === 'Bring up the sign-in window')[0];
    const text = textAll(banner);
    const hrefs = findAll(banner, e => e.tagName === 'a').map(a => a.href);
    (reopen._listeners['click'] || []).forEach(cb => cb());
    await tick();
    out({ text, hrefs, posts,
          inputs: findAll(banner, e => e.tagName === 'input').length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert "dedicated Chrome window opened for a@b.com" in result["text"]
    assert "Open in this browser instead" in result["text"]
    assert result["hrefs"] == ["https://claude.com/cai/oauth/authorize?step=cc"]
    assert "/api/auth/flow/cc1/open" in result["posts"]
    assert result["inputs"] == 1


def test_a_chained_poll_with_no_link_renders_no_entry(tmp_path):
    result = _run(tmp_path, _CHAINED_ENTRY + r"""
(async () => {
    const banner = await startAndChain([{ status: 'pending', flow_id: 'cc1' }]);
    out({ inputs: findAll(banner, e => e.tagName === 'input').length, text: textAll(banner) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["inputs"] == 0
    assert "Authorizing the Claude Code token" in result["text"]


def test_a_stale_primary_verdict_cannot_end_or_redraw_the_token_step(tmp_path):
    """A code submission that raced the poller answers for the PRIMARY flow
    after the chain started. It must not end the token step as a success,
    and its auth_url must not become the chained link."""
    result = _run(tmp_path, _CHAINED_ENTRY + r"""
(async () => {
    let resolveSubmit;
    global.api.post = async (p) => {
        posts.push(p);
        if (p.endsWith('/code')) return new Promise(r => { resolveSubmit = r; });
        return { flow_id: 'f1', auth_url: PRIMARY_URL, mode: 'browser' };
    };
    const answers = [{ status: 'completed', flow_id: 'f1', cc_flow_id: 'cc1' }];
    global.api.get = async () => { calls.get++; return answers.shift() || { status: 'pending', flow_id: 'cc1' }; };
    releaseRefresh();
    startReauthFlow(7, 'a@b.com');
    await tick();
    const input = findAll(statusEl, e => e.tagName === 'input')[0];
    input.value = 'primary-code#state';
    const submit = findAll(statusEl, e => e.tagName === 'button' && e.textContent === 'Submit code')[0];
    (submit._listeners['click'] || []).forEach(cb => cb());
    await tick();
    fireDoc('visibilitychange');            // the poller wins: chain to cc1
    await tick(); await tick();
    resolveSubmit({ status: 'completed', flow_id: 'f1', cc_flow_id: 'cc1',
                    auth_url: PRIMARY_URL, mode: 'browser' });
    await tick(); await tick();
    const banner = lastBanner();
    out({ text: textAll(banner), refreshCalls,
          hrefs: findAll(banner, e => e.tagName === 'a').map(a => a.href),
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert "Claude Code token authorized!" not in result["text"]
    assert "Authorizing the Claude Code token" in result["text"]
    assert result["hrefs"] == []
    assert result["refreshCalls"] == 1
    assert result["hooks"] == 1, "the chained flow is still polled"
