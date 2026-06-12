"""Tests for the auto-swap dashboard JS (drain advisor, swap history
status/residency, pause indicator, 24h swap counter) and the
account-actions auto-refresh guards (hard floor, fire-rate guard,
cross-tab storage sync, overdue-fire circuit breaker).

The web UI is plain browser JS with no bundler or JS test harness, so
these tests drive ``node`` from pytest: each case evals the component
source inside a minimal DOM stub and asserts on rendered output and
handler behavior. Skipped when node is not on PATH."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
AUTO_SWAP_JS = WEB_JS / "components" / "auto-swap.js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"
ACCOUNT_ACTIONS_JS = WEB_JS / "components" / "account-actions.js"
WEBSOCKET_JS = WEB_JS / "websocket.js"

EM_DASH = "—"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

# Minimal DOM/window/api stubs, then a direct (sloppy-mode) eval of the
# target file so its function declarations leak into module scope where
# the appended test snippet can call them. eval is safe here: it only
# executes first-party component source checked into this repo (the
# files under test), never external or user-supplied input — and it is
# the only way to load non-module browser scripts for unit testing.
_HARNESS = r"""
const fs = require('fs');
const TARGET = __TARGET__;

function makeEl(tag) {
    const el = {
        tagName: (tag || '').toUpperCase(),
        children: [],
        dataset: {},
        attributes: {},
        id: '',
        className: '',
        textContent: '',
        parentNode: null,
        style: {},
        _classes: new Set(),
    };
    el.classList = {
        add: (...cs) => cs.forEach(c => el._classes.add(c)),
        remove: (...cs) => cs.forEach(c => el._classes.delete(c)),
        contains: (c) => el._classes.has(c),
        toggle: (c) => el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c),
    };
    el.setAttribute = (k, v) => { el.attributes[k] = v; };
    el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
    el.prepend = (c) => { c.parentNode = el; el.children.unshift(c); return c; };
    el.remove = () => {
        if (el.parentNode) {
            const i = el.parentNode.children.indexOf(el);
            if (i >= 0) el.parentNode.children.splice(i, 1);
        }
        el.parentNode = null;
    };
    el.addEventListener = () => {};
    el.querySelector = () => null;
    el.insertAdjacentHTML = () => {};
    return el;
}

function findById(root, id) {
    if (!root) return null;
    if (root.id === id) return root;
    for (const c of root.children || []) {
        const hit = findById(c, id);
        if (hit) return hit;
    }
    return null;
}

const body = makeEl('body');
const _toasts = [];
const _windowListeners = {};
const _localStore = {};

global.window = {
    jackedState: {},
    addEventListener: (type, cb) => {
        (_windowListeners[type] = _windowListeners[type] || []).push(cb);
    },
};
global.__windowListeners = _windowListeners;
global.localStorage = {
    getItem: (k) => Object.prototype.hasOwnProperty.call(_localStore, k) ? _localStore[k] : null,
    setItem: (k, v) => { _localStore[k] = String(v); },
    removeItem: (k) => { delete _localStore[k]; },
};
global.document = {
    body,
    createElement: makeEl,
    createElementNS: (_ns, tag) => makeEl(tag),
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t), parentNode: null }),
    getElementById: (id) => findById(body, id),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
};
global.api = {
    get: async () => { throw new Error('api.get stub not configured'); },
    post: async () => ({}),
    put: async () => ({}),
};
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.showToast = (message, type, duration) => _toasts.push({ message, type, duration });
global.__getToasts = () => _toasts;
global.__makeEl = makeEl;
global.__body = body;
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');

const SRC = fs.readFileSync(TARGET, 'utf8');
if (TARGET.endsWith('websocket.js')) {
    eval(SRC + '\n;global.__jackedWS = jackedWS;');
} else {
    eval(SRC);
}
"""


def _run_js(tmp_path, snippet, js_file=AUTO_SWAP_JS):
    program = _HARNESS.replace("__TARGET__", json.dumps(str(js_file))) + "\n" + snippet
    script = tmp_path / "harness.js"
    script.write_text(program)
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@pytest.mark.parametrize(
    "js_file",
    [AUTO_SWAP_JS, ACCOUNTS_JS, ACCOUNT_ACTIONS_JS, WEBSOCKET_JS],
    ids=lambda p: p.name,
)
def test_node_syntax_check(js_file):
    proc = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


class TestFormatResidency:
    def test_human_durations(self, tmp_path):
        result = _run_js(tmp_path, """
out({
    r1: formatResidency(272),
    r2: formatResidency(3725),
    r3: formatResidency(59),
    r4: formatResidency(null),
    r5: formatResidency(-5),
    r6: formatResidency(0),
    r7: formatResidency(900),
});
""")
        assert result["r1"] == "4m 32s"
        assert result["r2"] == "1h 2m"
        assert result["r3"] == "59s"
        assert result["r4"] == EM_DASH
        assert result["r5"] == EM_DASH
        assert result["r6"] == "0s"
        assert result["r7"] == "15m 0s"


class TestSwapLogTable:
    def test_status_badges_and_residency(self, tmp_path):
        result = _run_js(tmp_path, """
const html = renderSwapLogTable([
    { timestamp: '2026-06-10T01:00:00Z', from_email: 'a@x.com', to_email: 'b@x.com',
      reason: 'r1', status: 'failed', residency_seconds: 272 },
    { timestamp: '2026-06-10T02:00:00Z', from_email: 'b@x.com', to_email: 'c@x.com',
      reason: 'r2', status: 'pending', residency_seconds: null },
    { timestamp: '2026-06-10T03:00:00Z', from_email: 'c@x.com', to_email: 'a@x.com',
      reason: 'r3', status: 'committed', residency_seconds: 3725 },
    { timestamp: '2026-06-10T04:00:00Z', from_email: 'a@x.com', to_email: 'c@x.com',
      reason: 'r4', residency_seconds: 1200 },
]);
out({ html });
""")
        html = result["html"]
        # Status badges: failed = muted red, pending = grey, committed = teal
        assert 'bg-red-900/50 text-red-300">failed</span>' in html
        assert 'bg-slate-700/50 text-slate-400">pending</span>' in html
        assert 'bg-teal-900/50 text-teal-300">committed</span>' in html
        # Rows without a status (pre-migration) render as committed
        assert html.count("committed</span>") == 2
        # Residency under 15 min highlighted red; longer ones muted
        assert 'text-red-400 whitespace-nowrap">4m 32s' in html
        assert 'text-slate-400 whitespace-nowrap">1h 2m' in html
        assert 'text-slate-400 whitespace-nowrap">20m 0s' in html
        # Null residency renders an em-dash placeholder
        assert f'text-slate-400 whitespace-nowrap">{EM_DASH}' in html
        # New header columns
        assert ">Status</th>" in html
        assert ">Residency</th>" in html


class TestLoadSwapLog:
    def test_new_response_shape_and_counter(self, tmp_path):
        result = _run_js(tmp_path, """
(async () => {
    const counter = __makeEl('span');
    counter.id = 'auto-swap-24h-counter';
    __body.appendChild(counter);
    global.api.get = async () => ({
        swaps: [{ timestamp: '2026-06-10T01:00:00Z', status: 'committed' }],
        swaps_last_24h: 7,
    });
    const entries = await loadSwapLog();
    // Legacy bare-array shape must still pass through untouched
    global.api.get = async () => ([{ id: 1 }, { id: 2 }]);
    const legacy = await loadSwapLog();
    out({
        count: entries.length,
        state: window.jackedState.swapsLast24h,
        counterText: counter.textContent,
        legacyCount: legacy.length,
    });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["count"] == 1
        assert result["state"] == 7
        assert result["counterText"] == "7 swaps / 24h"
        assert result["legacyCount"] == 2


class TestDrainAdvisorBanner:
    def test_show_dismiss_clear_lifecycle(self, tmp_path):
        result = _run_js(tmp_path, """
(async () => {
    const resetsAt = new Date(Date.now() + 2 * 3600 * 1000 + 60000).toISOString();
    const evt = { account_id: 3, email: 'a@x.com', label: 'a@x.com (Acme)',
                  deficit: 14.2, achievable: 6.0, stranding: 12.5, resets_at: resetsAt };
    handleDrainAdvisorEvent(evt);
    const banner = document.getElementById('drain-advisor-banner');
    const text = banner ? banner.children.map(c => c.textContent).join(' ') : '';
    const shownFor = banner ? banner.dataset.accountId : null;
    const amber = banner ? banner.className : '';

    // Dismissible via the close button
    banner.children[2].onclick();
    const dismissed = !document.getElementById('drain-advisor-banner');

    // A newer event re-shows it
    handleDrainAdvisorEvent(evt);
    const reshown = !!document.getElementById('drain-advisor-banner');

    // Low stranding for a DIFFERENT account must not clear it
    handleDrainAdvisorEvent({ account_id: 9, stranding: 0.5, resets_at: resetsAt });
    const stillShown = !!document.getElementById('drain-advisor-banner');

    // Newer event for the same account at/below threshold clears it
    handleDrainAdvisorEvent({ account_id: 3, stranding: 1.5, resets_at: resetsAt });
    const clearedAfterLow = !document.getElementById('drain-advisor-banner');

    // Auto-clears once the 7d window resets
    const soon = new Date(Date.now() + 50).toISOString();
    handleDrainAdvisorEvent({ account_id: 5, email: 'b@x.com', stranding: 8, resets_at: soon });
    const shownBeforeReset = !!document.getElementById('drain-advisor-banner');
    await new Promise(r => setTimeout(r, 250));
    const goneAfterReset = !document.getElementById('drain-advisor-banner');

    out({ text, shownFor, amber, dismissed, reshown, stillShown,
          clearedAfterLow, shownBeforeReset, goneAfterReset });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert "Account a@x.com (Acme) expires in 2h" in result["text"]
        assert "~13% capacity" in result["text"]
        assert "won't be drained at current pace" in result["text"]
        assert "route work there now" in result["text"]
        assert result["shownFor"] == "3"
        # Amber/info styling, distinct from the red stall banner
        assert "bg-amber-900/95" in result["amber"]
        assert "border-amber-600" in result["amber"]
        assert result["dismissed"] is True
        assert result["reshown"] is True
        assert result["stillShown"] is True
        assert result["clearedAfterLow"] is True
        assert result["shownBeforeReset"] is True
        assert result["goneAfterReset"] is True


class TestSameTierAdvisory:
    def test_info_toast_not_banner(self, tmp_path):
        result = _run_js(tmp_path, """
showSameTierAdvisory({ active_account_id: 1, best_account_id: 2,
                       best_tier: 2, best_deficit: 7.3 });
out({
    toasts: __getToasts(),
    bannerExists: !!document.getElementById('drain-advisor-banner'),
});
""")
        assert len(result["toasts"]) == 1
        toast = result["toasts"][0]
        assert toast["type"] == "info"
        assert "account 2" in toast["message"]
        assert "T2" in toast["message"]
        assert "7.3%" in toast["message"]
        assert result["bannerExists"] is False


class TestPausedIndicatorAndCounter:
    def test_panel_renders_pause_and_counter(self, tmp_path):
        result = _run_js(tmp_path, """
const futureIso = new Date(Date.now() + 10 * 60000).toISOString();

// Pause not initiated through the dashboard -> manual-switch hold
window.jackedState.swapSettings = { auto_swap_paused_until: futureIso };
const manualHtml = renderAutoSwapPanel();

// Same pause but tracked as UI-initiated -> no manual-switch suffix
window.jackedState._uiPauseUntil = futureIso;
const uiHtml = renderAutoSwapPanel();

// Expired pause -> no notice at all
window.jackedState._uiPauseUntil = null;
window.jackedState.swapSettings = {
    auto_swap_paused_until: new Date(Date.now() - 60000).toISOString(),
};
const expiredHtml = renderAutoSwapPanel();

// 24h counter next to the Enable Auto-Swap toggle
window.jackedState.swapsLast24h = 4;
window.jackedState.swapSettings = {};
const counterHtml = renderAutoSwapPanel();

out({
    manualHasNotice: manualHtml.includes('Auto-swap paused until'),
    manualHasSuffix: manualHtml.includes('(manual switch)'),
    uiHasNotice: uiHtml.includes('Auto-swap paused until'),
    uiHasSuffix: uiHtml.includes('(manual switch)'),
    expiredHasNotice: expiredHtml.includes('Auto-swap paused until'),
    counterShown: counterHtml.includes('4 swaps / 24h'),
    counterId: counterHtml.includes('id="auto-swap-24h-counter"'),
});
""")
        assert result["manualHasNotice"] is True
        assert result["manualHasSuffix"] is True
        assert result["uiHasNotice"] is True
        assert result["uiHasSuffix"] is False
        assert result["expiredHasNotice"] is False
        assert result["counterShown"] is True
        assert result["counterId"] is True


class TestWebsocketHandlers:
    def test_new_events_dispatch_to_handlers(self, tmp_path):
        result = _run_js(tmp_path, """
const ws = global.__jackedWS;
const dispatch = (type, payload) =>
    (ws.handlers[type] || []).forEach(cb => cb({ type, payload }));
const drained = [];
const advisories = [];
global.handleDrainAdvisorEvent = (d) => drained.push(d);
global.showSameTierAdvisory = (d) => advisories.push(d);
dispatch('expiring_with_stranded_capacity',
         { account_id: 3, stranding: 9.9, resets_at: '2026-06-12T00:00:00Z' });
dispatch('same_tier_deficit_advisory',
         { active_account_id: 1, best_account_id: 2, best_tier: 1, best_deficit: 5.0 });
dispatch('auto_swap_failed', { to_account_id: 4, failure: 'write error' });
out({
    strandedHandlers: (ws.handlers['expiring_with_stranded_capacity'] || []).length,
    advisoryHandlers: (ws.handlers['same_tier_deficit_advisory'] || []).length,
    drained,
    advisories,
    toasts: __getToasts(),
});
""", js_file=WEBSOCKET_JS)
        assert result["strandedHandlers"] == 1
        assert result["advisoryHandlers"] == 1
        assert result["drained"] == [
            {"account_id": 3, "stranding": 9.9, "resets_at": "2026-06-12T00:00:00Z"}
        ]
        assert result["advisories"] == [
            {"active_account_id": 1, "best_account_id": 2,
             "best_tier": 1, "best_deficit": 5.0}
        ]
        # auto_swap_failed now surfaces through showToast (the old `toast`
        # guard referenced a function that never existed)
        assert len(result["toasts"]) == 1
        assert result["toasts"][0]["type"] == "error"
        assert "account 4 failed: write error" in result["toasts"][0]["message"]


# Shared setup for the auto-refresh guard tests: deterministic timers (so a real
# 1s interval never keeps node alive), a refresh button + interval dropdown in
# the stub DOM, and a counting api.post stub.
_REFRESH_SETUP = r"""
const _ticks = [];
global.setInterval = (cb) => { _ticks.push(cb); return _ticks.length; };
global.clearInterval = () => {};
const btn = __makeEl('button'); btn.id = 'btn-refresh-all-usage'; __body.appendChild(btn);
const sel = __makeEl('select'); sel.id = 'sel-auto-refresh'; __body.appendChild(sel);
const posts = [];
global.api.post = async (url) => { posts.push(url); return { refreshed: 1, failed: 0, results: [] }; };
global.refreshAndRender = async () => {};
// Button label is appended as a text node on every update; read the most recent one.
const lastLabel = (el) => {
    const texts = el.children.filter(c => c.nodeType === 3);
    return texts.length ? texts[texts.length - 1].textContent : String(el.textContent || '');
};
"""


class TestAutoRefreshGuards:
    """Regression tests for the 2026-06 usage-fetch storm: a '0' interval in the
    shared localStorage key (written by another tab's circuit breaker) made every
    1s tick fire a bulk refresh pass back-to-back, indefinitely."""

    def test_source_guards(self):
        src = ACCOUNT_ACTIONS_JS.read_text()
        # Hard floor matching the smallest dropdown option
        assert "const _AUTO_REFRESH_FLOOR_SECS = 120" in src
        # Cross-tab re-sync of the shared interval key
        assert "window.addEventListener('storage'" in src
        # The overdue fire must not swallow failures with a bare catch
        assert "_triggerUsageRefresh().catch(() => {})" not in src
        # The manual button is exempt from the fire-rate guard
        assert "userInitiated: true" in src
        # Server bulk-lock contention (HTTP 429 REFRESH_IN_PROGRESS) is benign
        # cross-tab contention, never a circuit-break — breaking writes '0' to
        # the SHARED localStorage key and kills auto-refresh in every tab
        assert "REFRESH_IN_PROGRESS" in src

    def test_zero_interval_circuit_breaker(self, tmp_path):
        # Root cause (a): success path reset the countdown straight from
        # localStorage; another tab writing '0' turned every tick into a fire.
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
    _startAutoRefresh();
    const runningLabel = lastLabel(btn);

    // Another tab's circuit breaker writes '0' to the SHARED key while our timer runs
    localStorage.setItem('jacked_auto_refresh_interval', '0');
    await _triggerUsageRefresh();   // success path must not reset countdown to 0
    const postsAfterTrigger = posts.length;
    await _autoRefreshTick();       // pre-fix: countdown <= 0 -> fires another bulk pass
    await _autoRefreshTick();
    out({
        runningLabel,
        postsAfterTrigger,
        postsAfterTicks: posts.length,
        stoppedLabel: lastLabel(btn),
        selValue: sel.value,
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        assert "Refresh now" in result["runningLabel"]
        assert result["postsAfterTrigger"] == 1
        # No runaway: the 0 interval stops the timer instead of firing every tick
        assert result["postsAfterTicks"] == 1
        assert "Refresh All Usage" in result["stoppedLabel"]
        assert result["selValue"] == "0"

    def test_automated_fire_rate_guard(self, tmp_path):
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    const warns = [];
    console.warn = (...a) => warns.push(a.join(' '));
    await _triggerUsageRefresh();                         // automated, first start -> allowed
    await _triggerUsageRefresh();                         // back-to-back automated -> suppressed
    await _triggerUsageRefresh();                         // suppressed again, warn only once
    const postsAutomated = posts.length;
    await _triggerUsageRefresh({ userInitiated: true });  // manual click stays allowed
    out({
        postsAutomated,
        postsTotal: posts.length,
        warns: warns.length,
        toastTypes: __getToasts().map(t => t.type),
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        assert result["postsAutomated"] == 1
        assert result["postsTotal"] == 2
        assert result["warns"] == 1
        # Suppression is silent — console.warn only, no toast spam
        assert "warning" not in result["toastTypes"]

    def test_storage_event_cross_tab_sync(self, tmp_path):
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    const listeners = __windowListeners['storage'] || [];
    // Deterministic phase jitter: floor(0.5 * 11) = 5s offset
    Math.random = () => 0.5;
    localStorage.setItem('jacked_auto_refresh_interval', '120');
    _startAutoRefresh();
    const localLabel = lastLabel(btn);

    // Another tab turns auto-refresh off -> storage event fires here
    localStorage.setItem('jacked_auto_refresh_interval', '0');
    listeners.forEach(cb => cb({ key: 'jacked_auto_refresh_interval', newValue: '0' }));
    const stoppedLabel = lastLabel(btn);
    const selAfterOff = sel.value;

    // Another tab picks a new interval -> this tab re-arms at that value
    localStorage.setItem('jacked_auto_refresh_interval', '300');
    listeners.forEach(cb => cb({ key: 'jacked_auto_refresh_interval', newValue: '300' }));
    const rearmedLabel = lastLabel(btn);
    const selAfterOn = sel.value;

    // Unrelated keys are ignored (no crash, no timer change)
    listeners.forEach(cb => cb({ key: 'jacked_tip_dismissed', newValue: '1' }));
    out({ listenerCount: listeners.length, localLabel, stoppedLabel, selAfterOff,
          rearmedLabel, selAfterOn, finalLabel: lastLabel(btn) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        assert result["listenerCount"] >= 1
        # Local (user-initiated) arm has no jitter — exactly the chosen interval
        assert "2:00" in result["localLabel"]
        assert "Refresh All Usage" in result["stoppedLabel"]
        assert result["selAfterOff"] == "0"
        assert "Refresh now" in result["rearmedLabel"]
        # Storage-synced arm carries a random phase offset (5s with the stubbed
        # Math.random) so tabs armed by the same write de-phase instead of
        # firing in the same ~1s window and contending for the server bulk
        # lock on every cycle
        assert "5:05" in result["rearmedLabel"]
        assert result["selAfterOn"] == "300"
        assert "Refresh now" in result["finalLabel"]

    def test_tick_refresh_in_progress_is_benign_skip(self, tmp_path):
        # Two tabs phase-locked by the storage listener fire in the same ~1s
        # window; the loser of the server-side bulk lock race gets HTTP 429
        # REFRESH_IN_PROGRESS. That is benign contention — the tick catch must
        # NOT trip the circuit breaker (which writes '0' to the SHARED
        # localStorage key and kills auto-refresh in EVERY tab).
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
    _startAutoRefresh();

    global.api.post = async () => {
        throw Object.assign(new Error('Usage refresh already in progress'),
                            { code: 'REFRESH_IN_PROGRESS', status: 429 });
    };
    for (let i = 0; i < 120; i++) await _autoRefreshTick();  // countdown 120 -> fire
    const afterContention = {
        stored: localStorage.getItem('jacked_auto_refresh_interval'),
        label: lastLabel(btn),
        errorToasts: __getToasts().filter(t => t.type === 'error').length,
        infoToasts: __getToasts().filter(t => t.type === 'info').length,
    };

    // A genuine backend failure must still trip the breaker.
    const realNow = Date.now;
    Date.now = () => realNow() + 200000;   // clear the fire-rate guard window
    global.api.post = async () => { throw new Error('backend down'); };
    for (let i = 0; i < 120; i++) await _autoRefreshTick();
    Date.now = realNow;
    out({
        afterContention,
        storedAfterFailure: localStorage.getItem('jacked_auto_refresh_interval'),
        labelAfterFailure: lastLabel(btn),
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        # Benign contention: interval untouched, timer still armed, no error
        # toast — an info toast tells the user another refresh already ran
        assert result["afterContention"]["stored"] == "120"
        assert "Refresh now" in result["afterContention"]["label"]
        assert result["afterContention"]["errorToasts"] == 0
        assert result["afterContention"]["infoToasts"] == 1
        # Genuine failures still circuit-break
        assert result["storedAfterFailure"] == "0"
        assert "Refresh All Usage" in result["labelAfterFailure"]

    def test_overdue_fire_refresh_in_progress_is_benign_skip(self, tmp_path):
        # The overdue-fire .catch is the SILENT variant of the same kill:
        # returning to the accounts route while another tab's pass is in
        # flight must not disable auto-refresh everywhere.
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
    _startAutoRefresh();   // sets the elapsed-time anchor

    // Jump the clock past the interval so the next change takes the overdue path
    const realNow = Date.now;
    Date.now = () => realNow() + 600000;
    global.api.post = async () => {
        throw Object.assign(new Error('Usage refresh already in progress'),
                            { code: 'REFRESH_IN_PROGRESS', status: 429 });
    };
    _changeAutoRefreshInterval(120);                 // overdue -> fires immediately, 429s
    await new Promise(r => setTimeout(r, 20));       // let the rejection settle
    Date.now = realNow;
    out({
        storedInterval: localStorage.getItem('jacked_auto_refresh_interval'),
        selValue: String(sel.value),
        label: lastLabel(btn),
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        assert result["storedInterval"] == "120"
        assert result["selValue"] != "0"
        assert "Refresh now" in result["label"]

    def test_overdue_fire_failure_trips_circuit_breaker(self, tmp_path):
        # Root cause (b): the overdue fire's bare .catch(() => {}) left the timer
        # running against a failing backend with no circuit breaker.
        result = _run_js(tmp_path, _REFRESH_SETUP + r"""
(async () => {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
    _startAutoRefresh();   // sets the elapsed-time anchor

    // Jump the clock past the interval so the next change takes the overdue path
    const realNow = Date.now;
    Date.now = () => realNow() + 600000;
    global.api.post = async () => { throw new Error('backend down'); };
    _changeAutoRefreshInterval(120);                 // overdue -> fires immediately, fails
    await new Promise(r => setTimeout(r, 20));       // let the rejection settle
    Date.now = realNow;
    out({
        storedInterval: localStorage.getItem('jacked_auto_refresh_interval'),
        selValue: sel.value,
        label: lastLabel(btn),
    });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""", js_file=ACCOUNT_ACTIONS_JS)
        assert result["storedInterval"] == "0"
        assert result["selValue"] == "0"
        assert "Refresh All Usage" in result["label"]
