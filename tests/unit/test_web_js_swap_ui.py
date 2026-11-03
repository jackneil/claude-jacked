"""Tests for the auto-swap dashboard JS (drain advisor, swap history
status/residency, pause indicator, 24h swap counter, the 5-hour rules
toggle) and the account-actions auto-refresh guards (hard floor, fire-rate
guard, cross-tab storage sync, overdue-fire circuit breaker).

The web UI is plain browser JS with no bundler or JS test harness, so
these tests drive ``node`` from pytest: each case evals the component
source inside a minimal DOM stub and asserts on rendered output and
handler behavior. Skipped when node is not on PATH."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
AUTO_SWAP_JS = WEB_JS / "components" / "auto-swap.js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"
ACCOUNT_ACTIONS_JS = WEB_JS / "components" / "account-actions.js"
WEBSOCKET_JS = WEB_JS / "websocket.js"
UTILS_JS = WEB_JS / "utils.js"

PLACEHOLDER = "-"  # an empty value; never an em-dash

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
        _listeners: {},
    };
    el.classList = {
        add: (...cs) => cs.forEach(c => el._classes.add(c)),
        remove: (...cs) => cs.forEach(c => el._classes.delete(c)),
        contains: (c) => el._classes.has(c),
        // Same contract as DOMTokenList.toggle, including the force argument.
        toggle: (c, force) => {
            const on = force === undefined ? !el._classes.has(c) : Boolean(force);
            if (on) el._classes.add(c); else el._classes.delete(c);
            return on;
        },
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
    // Listeners are recorded, never fired on their own: a test fires one
    // with __fire(el, type).
    el.addEventListener = (type, cb) => {
        (el._listeners[type] = el._listeners[type] || []).push(cb);
    };
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
global.__fire = (el, type) => (el._listeners[type] || []).forEach(cb => cb({ type, target: el }));
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
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


@pytest.mark.parametrize(
    "js_file",
    [AUTO_SWAP_JS, ACCOUNTS_JS, ACCOUNT_ACTIONS_JS, WEBSOCKET_JS, UTILS_JS],
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
        assert result["r4"] == PLACEHOLDER
        assert result["r5"] == PLACEHOLDER
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
        # Residency under 15 min highlighted red; longer ones muted.
        # tabular-nums keeps the fixed-width durations from shifting layout.
        assert 'text-red-400 whitespace-nowrap tabular-nums">4m 32s' in html
        assert 'text-slate-400 whitespace-nowrap tabular-nums">1h 2m' in html
        assert 'text-slate-400 whitespace-nowrap tabular-nums">20m 0s' in html
        # Null residency renders an em-dash placeholder
        assert f'text-slate-400 whitespace-nowrap tabular-nums">{PLACEHOLDER}' in html
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
        assert "Route work there now." in result["text"]
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


# The toggle controls only the 5-hour rules. The weekly and model walls and
# the tier departures (a better 7-day position) run whatever it says, so the
# help text never says that jacked leaves an account "only" at a wall.
FIVE_H_RULES_HELP = (
    "Off: jacked does not leave an account for its 5-hour window. It still "
    "leaves an account whose weekly limit or model limit reaches the critical "
    "level of the plan (98% by default). It still moves to an account with a "
    "better 7-day position. Each swap makes the new account read the whole "
    "session again, and a 5-hour limit resets within hours. On: the two "
    "5-hour thresholds below also start a swap."
)
DIM = "opacity-60"
FIVE_H_ROWS = (("rng-warn-5h", "row-warn-5h"), ("rng-crit-5h", "row-crit-5h"))


def _tag(html, el_id, tag="input"):
    """The match of the opening tag of the element with this id."""
    m = re.search(rf'<{tag}\b[^>]*\bid="{re.escape(el_id)}"[^>]*>', html)
    assert m, f"no <{tag} id={el_id!r}> in the panel"
    return m


def _has_attr(tag_html, attr):
    """A real attribute, not a word in the class list: Tailwind variants
    such as ``disabled:cursor-not-allowed`` are classes."""
    bare = re.sub(r'\sclass="[^"]*"', "", tag_html)
    return re.search(rf"\s{attr}(?=[\s>=/])", bare) is not None


def _classes(tag_html):
    m = re.search(r'\sclass="([^"]*)"', tag_html)
    return set(m.group(1).split()) if m else set()


class TestFiveHourRulesToggle:
    """#224i: the 5-hour rules (5h critical, burn rate) are off by default,
    behind the "Swap on the 5-hour window" toggle. The toggle sits directly
    above the two 5-hour sliders. While it is off, those two sliders are
    disabled and their rows are dimmed. The usage check interval below the
    group is not a 5-hour rule: a divider separates it from the group, and
    the toggle never changes it."""

    def _panel(self, tmp_path, settings):
        return _run_js(tmp_path, """
window.jackedState.swapSettings = __SETTINGS__;
out({ html: renderAutoSwapPanel() });
""".replace("__SETTINGS__", json.dumps(settings)))["html"]

    def test_renders_off_by_default_with_the_label_and_the_help(self, tmp_path):
        html = self._panel(tmp_path, {})
        chk = _tag(html, "chk-swap-5h-rules").group(0)
        assert 'type="checkbox"' in chk
        assert not _has_attr(chk, "checked")
        assert '<label for="chk-swap-5h-rules" class="text-slate-400 text-sm">Swap on the 5-hour window</label>' in html
        assert f'id="help-swap-5h-rules" class="text-xs text-slate-400 mt-1 text-pretty">{FIVE_H_RULES_HELP}</p>' in html
        assert 'aria-describedby="help-swap-5h-rules"' in chk
        assert "—" not in html and "–" not in html

    def test_off_disables_the_two_5h_sliders_and_never_the_check_interval(self, tmp_path):
        html = self._panel(tmp_path, {"auto_swap_5h_rules_enabled": False})
        for slider, row in FIVE_H_ROWS:
            assert _has_attr(_tag(html, slider).group(0), "disabled"), slider
            assert DIM in _classes(_tag(html, row, "div").group(0)), row
        assert not _has_attr(_tag(html, "sel-check-interval", "select").group(0), "disabled")
        assert DIM not in _classes(_tag(html, "row-check-interval", "div").group(0))

    def test_on_renders_checked_with_the_5h_sliders_enabled(self, tmp_path):
        html = self._panel(tmp_path, {"auto_swap_5h_rules_enabled": True})
        assert _has_attr(_tag(html, "chk-swap-5h-rules").group(0), "checked")
        for slider, row in FIVE_H_ROWS:
            assert not _has_attr(_tag(html, slider).group(0), "disabled"), slider
            assert DIM not in _classes(_tag(html, row, "div").group(0)), row

    def test_the_toggle_sits_directly_above_the_5h_sliders_and_a_divider_closes_the_group(self, tmp_path):
        html = self._panel(tmp_path, {})
        chk = _tag(html, "chk-swap-5h-rules")
        warn = _tag(html, "rng-warn-5h")
        crit = _tag(html, "rng-crit-5h")
        interval_row = _tag(html, "row-check-interval", "div")
        assert chk.end() < warn.start() < crit.start() < interval_row.start()
        between = html[chk.end():warn.start()]
        assert "<input" not in between and "<select" not in between
        # The next control after the 5-hour group is the check interval.
        after = html[crit.end():interval_row.start()]
        assert "<input" not in after and "<select" not in after
        # A divider separates the 5-hour group from the check interval.
        assert "border-t" in _classes(interval_row.group(0))

    def test_flipping_the_toggle_enables_and_disables_the_5h_sliders_live(self, tmp_path):
        result = _run_js(tmp_path, """
const mk = (id, props) => {
    const el = Object.assign(__makeEl('div'), props || {});
    el.id = id;
    __body.appendChild(el);
    return el;
};
const chk = mk('chk-swap-5h-rules', { checked: true });
const sliders = {
    warn: mk('rng-warn-5h', { value: '80', disabled: false }),
    crit: mk('rng-crit-5h', { value: '90', disabled: false }),
    interval: mk('sel-check-interval', { value: '300', disabled: false }),
};
const rows = { warn: mk('row-warn-5h'), crit: mk('row-crit-5h'), interval: mk('row-check-interval') };
window.jackedState.swapSettings = {};
bindAutoSwapEvents();
const snap = () => Object.fromEntries(Object.keys(sliders).map(k => [k, {
    disabled: sliders[k].disabled, dim: rows[k].classList.contains('opacity-60'),
}]));
chk.checked = false; __fire(chk, 'change');
const off = snap();
chk.checked = true; __fire(chk, 'change');
const on = snap();
out({ off, on });
process.exit(0);
""")
        off, on = result["off"], result["on"]
        assert off["warn"] == {"disabled": True, "dim": True}
        assert off["crit"] == {"disabled": True, "dim": True}
        assert off["interval"] == {"disabled": False, "dim": False}
        for k in ("warn", "crit", "interval"):
            assert on[k] == {"disabled": False, "dim": False}, k

    def test_flipping_the_toggle_autosaves_it(self, tmp_path):
        result = _run_js(tmp_path, """
(async () => {
    const puts = [];
    global.api.put = async (path, body) => { puts.push({ path, body }); return body; };
    const chk = Object.assign(__makeEl('input'), { checked: false });
    chk.id = 'chk-swap-5h-rules';
    __body.appendChild(chk);
    window.jackedState.swapSettings = {};
    bindAutoSwapEvents();
    chk.checked = true;
    __fire(chk, 'change');
    await new Promise(r => setTimeout(r, 600));  // the save is debounced 400 ms
    out({ puts });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert [p["path"] for p in result["puts"]] == ["/api/settings/swap-settings"]
        assert result["puts"][0]["body"]["auto_swap_5h_rules_enabled"] is True

    def test_save_sends_the_field_and_keeps_the_pause_in_state(self, tmp_path):
        result = _run_js(tmp_path, """
(async () => {
    const puts = [];
    global.api.put = async (path, body) => { puts.push(body); return body; };
    window.jackedState.swapSettings = { auto_swap_paused_until: '2999-01-01T00:00:00+00:00' };
    await _saveSwapSettings();                    // no toggle in the DOM
    const chk = Object.assign(__makeEl('input'), { checked: true });
    chk.id = 'chk-swap-5h-rules';
    __body.appendChild(chk);
    await _saveSwapSettings();
    chk.checked = false;
    await _saveSwapSettings();
    out({ puts, state: window.jackedState.swapSettings });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert [p["auto_swap_5h_rules_enabled"] for p in result["puts"]] == [False, True, False]
        # The pause has its own endpoints. The form never sends it, so a save
        # never clears it on the server, and the panel keeps showing it.
        assert all("auto_swap_paused_until" not in p for p in result["puts"])
        assert result["state"]["auto_swap_paused_until"] == "2999-01-01T00:00:00+00:00"
        assert result["state"]["auto_swap_5h_rules_enabled"] is False


# The block of the panel that shows the pause: the notice, the badge, and the
# pause menu or the Resume button. When the pause changes, the page renders
# this block again in place, so an open panel stays open.
PAUSE_SLOT = "swap-pause-state"
PAUSE_NOTICE = "Auto-swap paused until"
MANUAL_SUFFIX = "(manual switch)"

_PAUSE_SETUP = r"""
const slot = __makeEl('div');
slot.id = 'swap-pause-state';
__body.appendChild(slot);
const PAUSE = new Date(Date.now() + 30 * 60000).toISOString();
const settle = () => new Promise(r => setTimeout(r, 0));
// The pause control of the page: the pause menu, or the Resume button.
const pauseControl = (action) => {
    const el = __makeEl(action === 'pause' ? 'select' : 'button');
    el.id = action === 'pause' ? 'sel-swap-pause' : 'btn-swap-resume';
    el.value = '30';
    __body.appendChild(el);
    return el;
};
const useControl = (action, el) => __fire(el, action === 'pause' ? 'change' : 'click');
global.api.post = async (path) => (
    path.includes('swap-pause') ? { paused_until: PAUSE, minutes: 30 } : { resumed: true }
);
"""


def _inner_div(html, el_id):
    """The inner HTML of the ``<div>`` with this id (nested divs counted)."""
    start = _tag(html, el_id, "div").end()
    depth = 1
    for m in re.finditer(r"<div\b|</div>", html[start:]):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            return html[start:start + m.start()]
    raise AssertionError(f"no closing tag for <div id={el_id!r}>")


class TestThePauseStateFollowsTheServer:
    """U2: the PUT response is the stored state. The server keeps a pause
    that a save does not send and reads it back, so the panel merges the
    response into ``window.jackedState.swapSettings`` and renders its pause
    block again. A pause that another client set (a manual switch, the menu
    bar) then shows after a save. A pause or a resume made on this page
    while a save is in flight wins over the response of that save: the
    server read that response before the change."""

    def test_the_panel_renders_the_pause_inside_its_slot(self, tmp_path):
        html = _run_js(tmp_path, """
window.jackedState.swapSettings = {
    auto_swap_paused_until: new Date(Date.now() + 30 * 60000).toISOString(),
};
out({ html: renderAutoSwapPanel() });
""")["html"]
        inner = _inner_div(html, PAUSE_SLOT)
        assert PAUSE_NOTICE in inner
        assert 'id="btn-swap-resume"' in inner
        assert 'id="chk-auto-swap"' not in inner
        assert 'id="chk-swap-5h-rules"' not in inner

    def test_save_shows_a_pause_that_the_server_kept(self, tmp_path):
        result = _run_js(tmp_path, _PAUSE_SETUP + """
(async () => {
    global.api.put = async (path, body) => ({ ...body, auto_swap_paused_until: PAUSE });
    window.jackedState.swapSettings = {};
    await _saveSwapSettings();
    out({ pause: window.jackedState.swapSettings.auto_swap_paused_until ?? null, expected: PAUSE, html: slot.innerHTML || '' });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["pause"] == result["expected"]
        # This page did not make the pause, so it reads as the manual-switch hold.
        assert PAUSE_NOTICE in result["html"] and MANUAL_SUFFIX in result["html"]
        assert 'id="btn-swap-resume"' in result["html"]

    def test_save_clears_a_pause_that_the_server_no_longer_has(self, tmp_path):
        result = _run_js(tmp_path, _PAUSE_SETUP + """
(async () => {
    global.api.put = async (path, body) => ({ ...body, auto_swap_paused_until: null });
    window.jackedState.swapSettings = { auto_swap_paused_until: PAUSE };
    await _saveSwapSettings();
    out({ pause: window.jackedState.swapSettings.auto_swap_paused_until ?? null, html: slot.innerHTML || '' });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["pause"] is None
        assert PAUSE_NOTICE not in result["html"]
        assert 'id="sel-swap-pause"' in result["html"]

    @pytest.mark.parametrize("action", ["pause", "resume"])
    def test_a_pause_change_during_a_save_wins_over_its_response(self, tmp_path, action):
        result = _run_js(tmp_path, _PAUSE_SETUP + """
(async () => {
    const ACTION = __ACTION__;
    const control = pauseControl(ACTION);
    window.jackedState.swapSettings = { auto_swap_paused_until: ACTION === 'pause' ? null : PAUSE };
    // The server reads the pause for this save before the change below.
    const readBefore = window.jackedState.swapSettings.auto_swap_paused_until;
    let answer;
    global.api.put = (path, body) => new Promise(r => {
        answer = () => r({ ...body, auto_swap_paused_until: readBefore });
    });
    bindAutoSwapEvents();
    const saving = _saveSwapSettings();
    useControl(ACTION, control);
    await settle();
    answer();
    await saving;
    out({
        pause: window.jackedState.swapSettings.auto_swap_paused_until ?? null,
        expected: ACTION === 'pause' ? PAUSE : null,
        html: slot.innerHTML || '',
    });
})().catch(e => { console.error(e); process.exit(1); });
""".replace("__ACTION__", json.dumps(action)))
        assert result["pause"] == result["expected"]
        assert (PAUSE_NOTICE in result["html"]) is (action == "pause")

    @pytest.mark.parametrize("action", ["pause", "resume"])
    def test_pause_and_resume_render_the_pause_block_again(self, tmp_path, action):
        html = _run_js(tmp_path, _PAUSE_SETUP + """
(async () => {
    const ACTION = __ACTION__;
    const control = pauseControl(ACTION);
    window.jackedState.swapSettings = { auto_swap_paused_until: ACTION === 'pause' ? null : PAUSE };
    bindAutoSwapEvents();
    useControl(ACTION, control);
    await settle();
    out({ html: slot.innerHTML || '' });
})().catch(e => { console.error(e); process.exit(1); });
""".replace("__ACTION__", json.dumps(action)))["html"]
        if action == "pause":
            # This page made the pause, so it is not the manual-switch hold.
            assert PAUSE_NOTICE in html and MANUAL_SUFFIX not in html
            assert 'id="btn-swap-resume"' in html
        else:
            assert PAUSE_NOTICE not in html
            assert 'id="sel-swap-pause"' in html


class TestRetiredSevenDayThreshold:
    """The "7-Day Threshold" slider (``auto_swap_7d_threshold``) changed
    nothing: no decision code read it. The weekly wall is the plan catalog
    field ``weekly_critical_pct``, and the dashboard has no control for it.
    So the panel shows no 7-day slider, and a save never sends the field."""

    RETIRED_IDS = ("row-thresh-7d", "rng-thresh-7d", "lbl-thresh-7d")

    def test_the_panel_has_no_7_day_threshold_control(self, tmp_path):
        # An older server still returns the field. The panel ignores it.
        html = _run_js(tmp_path, """
window.jackedState.swapSettings = { auto_swap_7d_threshold: 70 };
out({ html: renderAutoSwapPanel() });
""")["html"]
        for retired_id in self.RETIRED_IDS:
            assert f'id="{retired_id}"' not in html, retired_id
        assert "7-Day Threshold" not in html

    def test_a_stale_7_day_slider_is_neither_bound_nor_saved(self, tmp_path):
        """A stale page can still hold the old slider. bindAutoSwapEvents
        attaches no handler to it, and a save does not send its value."""
        result = _run_js(tmp_path, """
(async () => {
    const puts = [];
    global.api.put = async (path, body) => { puts.push(body); return body; };
    const slider = Object.assign(__makeEl('input'), { value: '70' });
    slider.id = 'rng-thresh-7d';
    __body.appendChild(slider);
    const label = __makeEl('span');
    label.id = 'lbl-thresh-7d';
    __body.appendChild(label);
    window.jackedState.swapSettings = {};
    bindAutoSwapEvents();
    await _saveSwapSettings();
    out({ listeners: Object.keys(slider._listeners), puts });
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["listeners"] == []
        assert len(result["puts"]) == 1
        assert "auto_swap_7d_threshold" not in result["puts"][0]


class TestDecisionLogWallColumn:
    """The candidate table of a decision-log row ends with a "Wall" column
    (#224i). It shows why the wall filter rejects a candidate: the first word
    of its walled departure reason (``wall``: "weekly critical: ..." gives
    "weekly", "model weekly critical: ..." gives "model"), "model" when it
    cannot serve the model of the active account (``serves_model`` false),
    else a plain "-". A row logged before these fields shows "-"."""

    WEEKLY = "weekly critical: 7d usage 99.0% >= 98%"
    MODEL = "model weekly critical: Fable 100.0% >= 98%"

    def _table(self, tmp_path, candidates):
        html = _run_js(tmp_path, """
out({ html: buildDecisionDetailHtml(
    { candidates: __CANDIDATES__, should_swap: false, cooldown_active: false }, 'decision-1') });
""".replace("__CANDIDATES__", json.dumps(candidates)))["html"]
        table = html[html.index("<table"):html.index("</table>")]
        head, body = table.split("<tbody>")
        heads = re.findall(r"<th\b[^>]*>([^<]*)</th>", head)
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", body)
        return heads, [re.findall(r"<td\b[^>]*>(.*?)</td>", row)[-1] for row in rows]

    def test_wall_is_the_last_column(self, tmp_path):
        heads, _ = self._table(tmp_path, [{"label": "a"}])
        assert heads[-2:] == ["Best", "Wall"]

    def test_the_three_states_and_an_old_row(self, tmp_path):
        heads, cells = self._table(tmp_path, [
            {"label": "weekly", "wall": self.WEEKLY, "serves_model": True},
            {"label": "model wall", "wall": self.MODEL, "serves_model": False},
            {"label": "other model", "wall": None, "serves_model": False},
            {"label": "free", "wall": None, "serves_model": True},
            {"label": "logged before the fields"},
        ])
        texts = [re.sub(r"<[^>]+>", "", cell) for cell in cells]
        assert texts == ["weekly", "model", "model", "-", "-"]
        assert all("—" not in cell and "–" not in cell for cell in cells)

    def test_the_full_reason_is_an_escaped_title(self, tmp_path):
        _, cells = self._table(tmp_path, [
            {"label": "a", "wall": self.WEEKLY, "serves_model": True},
            {"label": "b", "wall": '<img src=x onerror="alert(1)">: x', "serves_model": True},
        ])
        assert 'title="weekly critical: 7d usage 99.0% &gt;= 98%"' in cells[0]
        assert "<img" not in cells[1]
        assert "&lt;img" in cells[1]


class TestExhaustionBanner:
    """The all-accounts-exhausted banner names the window whose reset frees
    an account (next_recovery_window), and never promises usage in general:
    a model limit (Fable) can still be full after that reset. Without a
    window kind (an older server) it keeps the generic text. When a model
    limit walls the account (window "model"), the banner names that limit
    (next_recovery_label) and gives its reset time in the format of the
    account cards (formatResetTime, utils.js)."""

    def _banner(self, tmp_path, minutes, window=None, label=None):
        # utils.js loads before auto-swap.js on the page. Only its reset
        # formatter is taken here: its escapeHtml needs a real DOM. The eval
        # runs first-party repo source only, as the harness eval does.
        return _run_js(tmp_path, """
global.formatResetTime = (function () {
    eval(fs.readFileSync(__UTILS__, 'utf8'));
    return formatResetTime;
})();
const el = __makeEl('div');
el.id = 'swap-exhaustion-banner';
__body.appendChild(el);
const at = __MINUTES__ === null ? null : new Date(Date.now() + __MINUTES__ * 60000).toISOString();
window.jackedState._exhaustionData = {
    next_recovery_at: at, next_recovery_window: __WINDOW__, next_recovery_label: __LABEL__,
};
renderExhaustionBanner();
out({
    html: el.innerHTML,
    hidden: el.classList.contains('hidden'),
    reset: at === null ? null : formatResetTime(at),
});
""".replace("__UTILS__", json.dumps(str(UTILS_JS)))
           .replace("__MINUTES__", json.dumps(minutes))
           .replace("__WINDOW__", json.dumps(window))
           .replace("__LABEL__", json.dumps(label)))

    @pytest.mark.parametrize(
        "minutes, label, name",
        [
            (3 * 1440 + 125, "Fable", "The Fable limit"),
            (11.5, "Fable", "The Fable limit"),
            (199.5, None, "The model limit"),
            (199.5, "", "The model limit"),
        ],
    )
    def test_model_window_names_the_limit_and_its_reset_time(self, tmp_path, minutes, label, name):
        result = self._banner(tmp_path, minutes, "model", label)
        assert result["hidden"] is False
        assert result["reset"].startswith("resets ")
        assert f">{name} {result['reset']}</div>" in result["html"]
        assert "available again" not in result["html"]
        assert "—" not in result["html"] and "–" not in result["html"]

    def test_model_window_with_a_passed_reset_says_soon(self, tmp_path):
        result = self._banner(tmp_path, -1, "model", "Fable")
        assert ">The Fable limit should reset soon</div>" in result["html"]

    def test_model_label_is_escaped(self, tmp_path):
        """The label comes from the provider's usage payload: it is text,
        never markup, in the banner's innerHTML."""
        result = self._banner(tmp_path, 30, "model", '<img src=x onerror="alert(1)">')
        assert "<img" not in result["html"]
        assert "The &lt;img src=x onerror=&quot;alert(1)&quot;&gt; limit resets" in result["html"]

    @pytest.mark.parametrize(
        "minutes, window, text",
        [
            (199.5, "7d", "The weekly limit resets in 3h 20m"),
            (11.5, "5h", "The 5-hour limit resets in 12m"),
            (-1, "7d", "The weekly limit should reset soon"),
            (199.5, None, "Usage is available again in 3h 20m"),
            (11.5, None, "Usage is available again in 12m"),
            (-1, None, "Usage should be available again soon"),
        ],
    )
    def test_recovery_text(self, tmp_path, minutes, window, text):
        result = self._banner(tmp_path, minutes, window)
        assert result["hidden"] is False
        assert f">{text}</div>" in result["html"]
        assert "All accounts at capacity" in result["html"]
        assert "\u2014" not in result["html"] and "\u2013" not in result["html"]

    def test_unknown_window_kind_keeps_the_generic_text(self, tmp_path):
        result = self._banner(tmp_path, 30, "monthly")
        assert ">Usage is available again in 30m</div>" in result["html"]

    def test_no_recovery_time_shows_no_text(self, tmp_path):
        result = self._banner(tmp_path, None)
        assert "available again" not in result["html"]
        assert "resets in" not in result["html"]


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
