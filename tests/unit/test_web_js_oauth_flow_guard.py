"""The OAuth flow's account-action guard must not outlive the server's verdict.

While a flow is pending, window.jackedState._accountActionInFlight makes every
other account action (Use Account included) bail with a short toast. Three
things used to keep that guard up after the sign-in had actually finished:

1. The verdict was only read by a 1s setInterval, and the sign-in window takes
   the foreground, so a throttled or frozen dashboard tab could sit on a
   completed flow for minutes. Returning to the tab now polls immediately.
2. The guard was released only after refreshAndRender() resolved, so a Use
   Account click during that refresh (Keychain reconcile on macOS) was refused.
   The guard now drops the moment the poll says completed.
3. There was no way out short of a reload. The banner now has a Cancel button.

Node runs the real component source; skipped when node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

OAUTH_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "components" / "oauth-flows.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = r"""
const listeners = { doc: {}, win: {} };
function makeEl(tag) {
    return {
        tagName: tag, className: '', textContent: '', type: '', name: '',
        placeholder: '', autocomplete: '', spellcheck: true, href: '', target: '',
        rel: '', hidden: false, value: '', disabled: false, isConnected: true,
        parentNode: null, children: [], _attrs: {}, _listeners: {},
        appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
        addEventListener(t, cb) { (this._listeners[t] = this._listeners[t] || []).push(cb); },
        setAttribute(k, v) { this._attrs[k] = v; },
        remove() {},
    };
}
const statusEl = makeEl('div');
global.document = {
    hidden: false,
    createElement: makeEl,
    createTextNode: (t) => ({ textContent: t, children: [] }),
    getElementById: (id) => id === 'oauth-flow-status' ? statusEl : null,
    addEventListener(t, cb) { (listeners.doc[t] = listeners.doc[t] || []).push(cb); },
    removeEventListener(t, cb) { listeners.doc[t] = (listeners.doc[t] || []).filter(f => f !== cb); },
};
global.window = {
    jackedState: {}, location: { hostname: 'localhost' },
    addEventListener(t, cb) { (listeners.win[t] = listeners.win[t] || []).push(cb); },
    removeEventListener(t, cb) { listeners.win[t] = (listeners.win[t] || []).filter(f => f !== cb); },
};
// The interval never fires here: every poll in these tests is driven by the
// visibility hook, which is the behaviour under test.
global.setInterval = () => 1;
global.clearInterval = () => {};
const calls = { get: 0, post: [] };
let pollStatus = 'pending';
global.api = {
    post: async (p) => { calls.post.push(p); return { flow_id: 'f1', auth_url: 'u', mode: 'browser' }; },
    get: async () => { calls.get++; return { status: pollStatus }; },
};
let releaseRefresh;
const refreshGate = new Promise(r => { releaseRefresh = r; });
let refreshCalls = 0;
global.refreshAndRender = () => { refreshCalls++; return refreshGate; };
global.showToast = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
function fireDoc(type) { (listeners.doc[type] || []).slice().forEach(cb => cb()); }
function findAttr(el, attr) {
    if (el._attrs && el._attrs[attr]) return el;
    for (const c of (el.children || [])) { const hit = findAttr(c, attr); if (hit) return hit; }
    return null;
}
function textAll(el) { return (el.textContent || '') + (el.children || []).map(textAll).join(' '); }
const tick = () => new Promise(r => setTimeout(r, 5));
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
"""


def _run(tmp_path, snippet):
    script = tmp_path / "guard.js"
    script.write_text(
        HARNESS + OAUTH_JS.read_text(encoding="utf-8") + "\n" + snippet,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_returning_to_the_tab_polls_at_once_and_releases_the_guard_before_the_refresh(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    startCcAuthFlow(7, 'a@b.com');
    await tick();
    const guardDuringFlow = window.jackedState._accountActionInFlight;
    const hooked = (listeners.doc['visibilitychange'] || []).length + (listeners.win['focus'] || []).length;
    pollStatus = 'completed';
    fireDoc('visibilitychange');
    await tick();
    const guardWhileRefreshPending = window.jackedState._accountActionInFlight;
    const unhooked = (listeners.doc['visibilitychange'] || []).length + (listeners.win['focus'] || []).length;
    releaseRefresh();
    await tick();
    out({ guardDuringFlow, hooked, gets: calls.get, guardWhileRefreshPending,
          refreshCalls, unhooked, banner: textAll(statusEl) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["guardDuringFlow"] is True
    assert result["hooked"] == 2, "visibilitychange + focus both re-poll"
    assert result["gets"] == 1, "coming back to the tab polls without waiting for the interval"
    assert result["guardWhileRefreshPending"] is False, "the verdict, not the refresh, releases the guard"
    assert result["refreshCalls"] == 1
    assert result["unhooked"] == 0, "listeners are removed once the flow is terminal"
    assert "authorized successfully" in result["banner"]


def test_a_hidden_tab_does_not_poll_on_the_hook(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    startCcAuthFlow(7, 'a@b.com');
    await tick();
    document.hidden = true;
    fireDoc('visibilitychange');
    await tick();
    const getsWhileHidden = calls.get;
    document.hidden = false;
    fireDoc('visibilitychange');
    await tick();
    out({ getsWhileHidden, getsWhenVisible: calls.get });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result == {"getsWhileHidden": 0, "getsWhenVisible": 1}


def test_cancel_ends_the_flow_locally_and_releases_the_guard(tmp_path):
    result = _run(tmp_path, r"""
(async () => {
    startCcAuthFlow(7, 'a@b.com');
    await tick();
    const cancel = findAttr(statusEl, 'data-oauth-cancel');
    const hadCancel = !!cancel;
    const guardBefore = window.jackedState._accountActionInFlight;
    (cancel._listeners['click'] || []).forEach(cb => cb());
    await tick();
    const guardAfter = window.jackedState._accountActionInFlight;
    const banner = textAll(statusEl);
    // A poll after cancel must be inert: the flow is terminal on this side.
    pollStatus = 'completed';
    fireDoc('visibilitychange');
    await tick();
    out({ hadCancel, guardBefore, guardAfter, banner, gets: calls.get, refreshCalls,
          hooks: (listeners.doc['visibilitychange'] || []).length });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
    assert result["hadCancel"] is True
    assert result["guardBefore"] is True
    assert result["guardAfter"] is False
    assert "cancelled" in result["banner"].lower()
    assert result["gets"] == 0
    assert result["hooks"] == 0


def test_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(OAUTH_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
