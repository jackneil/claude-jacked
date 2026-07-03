"""Tests for the compact usage panel renderer (panel.js).

panel.js reuses renderUsageBar() (usage.js) and groupAccountsByLogin()
(account-grouping.js), so the harness evals all three under node in one scope
(the same sloppy-eval technique as test_web_js_swap_ui.py) and asserts on the
rendered HTML: the reused bars + white .elapsed-marker, multi-org grouping with
the "N orgs" chip + connecting rail, the active-org marker, tabular-nums on the
percentage, and the empty/error states. Skipped when node is not on PATH.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
USAGE_JS = WEB_JS / "components" / "usage.js"
GROUPING_JS = WEB_JS / "util" / "account-grouping.js"
PANEL_JS = WEB_JS / "components" / "panel.js"
PROVIDER_JS = WEB_JS / "util" / "provider.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

_HARNESS = r"""
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.formatResetTime = (iso) => iso ? 'resets soon' : '';
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
eval(fs.readFileSync(__PROVIDER__, 'utf8'));
eval(fs.readFileSync(__USAGE__, 'utf8'));
eval(fs.readFileSync(__GROUPING__, 'utf8'));
eval(fs.readFileSync(__PANEL__, 'utf8'));
"""


def _run(tmp_path, snippet):
    program = (
        _HARNESS
        .replace("__PROVIDER__", json.dumps(str(PROVIDER_JS)))
        .replace("__USAGE__", json.dumps(str(USAGE_JS)))
        .replace("__GROUPING__", json.dumps(str(GROUPING_JS)))
        .replace("__PANEL__", json.dumps(str(PANEL_JS)))
        + "\n" + snippet
    )
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
    "js_file", [PROVIDER_JS, USAGE_JS, GROUPING_JS, PANEL_JS], ids=lambda p: p.name
)
def test_node_syntax_check(js_file):
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_multi_org_panel_renders_bars_markers_and_grouping(tmp_path):
    result = _run(tmp_path, """
// reset 2h in the future → ~60% of the 5h window elapsed → marker rendered
const reset5h = new Date(Date.now() + 2 * 3600 * 1000).toISOString();
const reset7d = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
const accts = [
    { id: 1, email: 'jack@x.com', organization_uuid: '', priority: 1,
      cached_usage_5h: 40, cached_usage_7d: 30,
      cached_5h_resets_at: reset5h, cached_7d_resets_at: reset7d,
      subscription_type: 'pro', rate_limit_tier: '' },
    { id: 2, email: 'jack@x.com', organization_uuid: 'org-123', organization_name: 'Acme', priority: 0,
      cached_usage_5h: 96, cached_usage_7d: 78,
      cached_5h_resets_at: reset5h, cached_7d_resets_at: reset7d,
      subscription_type: 'max', rate_limit_tier: 'default_claude_max_20x' },
];
const html = buildPanelHtml(groupAccountsByLogin(accts, 2));
out({ html });
""")
    html = result["html"]
    # Reused bar component + white time marker
    assert "usage-bar" in html
    assert "elapsed-marker" in html, "white time-marker must render"
    assert "tabular-nums" in html, "percentage must use tabular-nums"
    # Color classes flow from the shared usageColorClass (96→red, 78→yellow)
    assert "fill red" in html
    assert "fill yellow" in html
    # Both windows present
    assert ">5h<" in html and ">7d<" in html
    # Multi-org grouping
    assert "2 orgs" in html, "org-chip must show org count"
    assert "has-rail" in html, "multi-org group shows the connecting rail"
    assert "Acme" in html and "Personal" in html
    # Active org marked (account id 2 is active)
    assert "active-badge" in html
    assert 'data-account-id="2"' in html
    # Plan badge derived from subscription + tier
    assert "Max 20x" in html


def test_binding_model_adds_third_compact_bar(tmp_path):
    # An account whose usage carries a binding_model gets a third compact bar
    # (the per-model cap, e.g. Fable) under the 5h/7d bars.
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 3, email: 'jack@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 10, cached_usage_7d: 66,
      usage: { binding_model: { label: 'Fable', utilization: 92,
                                resets_at: null, severity: 'critical' } } },
], null));
out({ html });
""")
    html = result["html"]
    assert ">5h<" in html and ">7d<" in html
    assert "Fable" in html, "the binding model label must render as a third bar"
    assert "92%" in html
    assert "fill red" in html, "92% Fable → red via shared usageColorClass"


def test_no_binding_model_keeps_two_bars(tmp_path):
    # No binding_model → only the 5h and 7d bars, panel density preserved.
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 4, email: 'idle@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 5, cached_usage_7d: 3, usage: {} },
], null));
out({ html });
""")
    html = result["html"]
    assert ">5h<" in html and ">7d<" in html
    # exactly the two window labels — no extra model bar
    assert html.count("usage-bar") == 2


def test_single_org_has_no_chip_or_rail(tmp_path):
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 7, email: 'solo@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 10, cached_usage_7d: 5 },
], null));
out({ html });
""")
    html = result["html"]
    assert "orgs</span>" not in html and "org-chip" not in html
    assert "has-rail" not in html
    assert "active-badge" not in html


def test_empty_and_error_states(tmp_path):
    result = _run(tmp_path, """
out({ empty: buildPanelHtml([]), err: panelErrorHtml('boom & <x>') });
""")
    assert "No accounts connected" in result["empty"]
    assert "Can't reach jacked" in result["err"]
    assert "boom &amp; &lt;x&gt;" in result["err"], "error message must be escaped"


def test_marker_absent_when_no_reset_time(tmp_path):
    """No reset timestamp → no elapsed fraction → no white marker drawn."""
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'a@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 50, cached_usage_7d: 50 },
], null));
out({ hasMarker: html.includes('elapsed-marker') });
""")
    assert result["hasMarker"] is False


def test_panel_marks_provider_per_account(tmp_path):
    """Each panel row carries a provider BADGE (glyph + CODEX/CLAUDE label) +
    provider-* class; Codex and Claude get distinct colors so you can tell
    accounts apart at a glance, not just on hover."""
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'claudey@x.com', organization_uuid: '', priority: 0,
      provider: 'claude', cached_usage_5h: 10, cached_usage_7d: 5 },
    { id: 2, email: 'codey@x.com', organization_uuid: '', priority: 1,
      provider: 'codex', cached_usage_5h: 20, cached_usage_7d: 15 },
], null));
out({ html });
""")
    html = result["html"]
    assert "provider-badge" in html            # labeled badge, not the bare glyph
    assert "provider-glyph" in html
    assert "provider-label" in html
    assert ">Claude<" in html and ">Codex<" in html  # visible text labels
    assert "provider-claude" in html and "provider-codex" in html
    assert "#a78bfa" in html  # Claude violet
    assert "#60a5fa" in html  # Codex blue
    assert 'title="Claude account"' in html and 'title="Codex account"' in html


def test_panel_provider_defaults_to_claude_when_missing(tmp_path):
    """An account row with no provider field renders as Claude (back-compat)."""
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'legacy@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 10, cached_usage_7d: 5 },
], null));
out({ html });
""")
    html = result["html"]
    assert "provider-claude" in html
    assert "provider-codex" not in html


def test_compact_bar_shows_reset_time_inline(tmp_path):
    """The dropdown panel must show WHEN each window resets, inline — not only on
    hover. Regression for the "dropdown doesn't show 5h/7d reset time" report."""
    result = _run(tmp_path, """
formatResetTime = (iso) => iso ? 'resets 3:45 PM' : '';
const reset = new Date(Date.now() + 2 * 3600 * 1000).toISOString();
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'a@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 40, cached_usage_7d: 30,
      cached_5h_resets_at: reset, cached_7d_resets_at: reset },
], null));
out({ html });
""")
    html = result["html"]
    assert html.count("reset-caption") == 2, "both 5h and 7d show an inline reset"
    assert ">3:45 PM<" in html, "the stripped reset time renders as visible text"
    # The 'resets ' prefix is dropped in the visible caption (kept in the title)
    assert ">resets 3:45 PM<" not in html
    assert "tabular-nums" in html


def test_compact_bar_no_reset_caption_without_reset_time(tmp_path):
    """No reset timestamp → no caption span (don't render an empty element)."""
    result = _run(tmp_path, """
formatResetTime = (iso) => iso ? 'resets 3:45 PM' : '';
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'a@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 50, cached_usage_7d: 50 },
], null));
out({ hasCaption: html.includes('reset-caption') });
""")
    assert result["hasCaption"] is False


def test_single_account_is_email_primary_and_strips_org_noise(tmp_path):
    """Single-org account collapses to one email-primary line; the noisy
    "<email>'s Organization" label is gone; freshness age is shown; no chip/rail."""
    result = _run(tmp_path, """
const now = Math.floor(Date.now() / 1000);
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 9, email: 'solo@example.com', organization_uuid: 'o1',
      organization_name: "solo@example.com's Organization", priority: 0,
      cached_usage_5h: 40, cached_usage_7d: 30, usage_cached_at: now - 300 },
], null));
out({ html });
""")
    html = result["html"]
    assert "acct-email" in html and "solo@example.com" in html
    assert "'s Organization" not in html, "noisy auto org name must be stripped"
    assert "Personal" not in html, "a lone personal account shows no redundant 'Personal'"
    assert "org-chip" not in html and "has-rail" not in html
    assert "acct-age" in html and ">5m<" in html, "freshness age must render"


def test_compact_bars_drop_the_reset_time_column(tmp_path):
    """The wide dashboard reset column (w-28) is what squeezed the bar — the
    panel must use compact bars without it (reset moves to the row title)."""
    result = _run(tmp_path, """
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'a@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 50, cached_usage_7d: 50,
      cached_5h_resets_at: '2099-01-01T00:00:00Z' },
], null));
out({ html });
""")
    html = result["html"]
    assert "w-28" not in html, "compact bars must not carry the fixed reset-time column"
    assert "usage-bar" in html and "tabular-nums" in html


def test_menu_button_gated_on_native_bridge(tmp_path):
    """The in-panel ⋯ button stays hidden in a plain browser (no bridge) and is
    revealed + posts 'show-menu' only when the WKWebView→native bridge exists."""
    result = _run(tmp_path, """
function fakeBtn() { return { hidden: true, _listeners: {}, addEventListener(ev, fn){ this._listeners[ev]=fn; } }; }

// 1) No bridge → stays hidden, no click wiring.
let btn = fakeBtn();
global.document = { getElementById: () => btn };
global.window = {};
setupMenuButton();
const noBridge = { hidden: btn.hidden, wired: !!btn._listeners.click };

// 2) Bridge present → revealed + click posts 'show-menu'.
btn = fakeBtn();
let posted = null;
global.document = { getElementById: () => btn };
global.window = { webkit: { messageHandlers: { jacked: { postMessage: (m) => { posted = m; } } } } };
setupMenuButton();
if (btn._listeners.click) btn._listeners.click();
const withBridge = { hidden: btn.hidden, posted };

out({ noBridge, withBridge });
""")
    assert result["noBridge"] == {"hidden": True, "wired": False}
    assert result["withBridge"]["hidden"] is False
    assert result["withBridge"]["posted"] == "show-menu"


def test_next_refresh_countdown_only_on_active_account(tmp_path):
    """The active account shows a live 'next refresh' countdown; others don't.
    formatCountdown rolls minutes/seconds and flips to 'refreshing…' when due."""
    result = _run(tmp_path, """
const nextAt = Math.floor(Date.now() / 1000) + 185;  // ~3 min out
const html = buildPanelHtml(groupAccountsByLogin([
    { id: 1, email: 'active@x.com', organization_uuid: '', priority: 0,
      cached_usage_5h: 50, cached_usage_7d: 50 },
    { id: 2, email: 'other@x.com', organization_uuid: '', priority: 1,
      cached_usage_5h: 10, cached_usage_7d: 10 },
], 1), nextAt);
out({
    html,
    nextCount: (html.match(/data-next-at=/g) || []).length,
    cd: formatCountdown(nextAt),
    soon: formatCountdown(Math.floor(Date.now()/1000) + 30),
    due: formatCountdown(Math.floor(Date.now()/1000) - 5),
    none: formatCountdown(null),
});
""")
    assert "next-refresh" in result["html"]
    assert result["nextCount"] == 1, "only the active account gets the countdown"
    assert result["cd"].startswith("next ") and result["cd"].endswith("m")
    assert result["soon"].endswith("s") and result["soon"].startswith("next ")
    assert result["due"] == "refreshing…"
    assert result["none"] == ""


def test_manual_refresh_button_endpoint_and_persisted_cooldown(tmp_path):
    """The refresh button POSTs the user-initiated bulk refresh, records the
    timestamp, and the 60s cooldown survives a popover reopen (localStorage)."""
    result = _run(tmp_path, """
(async () => {
function fakeBtn() {
    return { disabled: false, title: '',
             classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);} },
             _l: {}, addEventListener(e, fn){ this._l[e] = fn; } };
}
// No-op setTimeout so the cooldown ticker doesn't keep node alive past exit.
global.setTimeout = function () { return 0; };
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null),
                        setItem: (k, v) => { store[k] = String(v); } };
let posted = null;
global.fetch = async (url, opts) => { posted = { url, method: opts && opts.method }; return { ok: true, json: async () => ({}) }; };

let btn = fakeBtn();
global.document = { getElementById: (id) => (id === 'panel-refresh-btn' ? btn : null) };
setupRefreshButton();
const enabledFresh = btn.disabled;          // no prior refresh → enabled

await btn._l.click();                         // click → POST + stamp + cooldown
const postedAfter = posted;
const stamped = !!store['jacked_panel_last_refresh'];
const disabledAfterClick = btn.disabled;

// Reopen: a brand-new button with the recent timestamp must start cooled-down.
let btn2 = fakeBtn();
global.document = { getElementById: (id) => (id === 'panel-refresh-btn' ? btn2 : null) };
setupRefreshButton();
const cooledOnReopen = btn2.disabled;

out({ enabledFresh, postedAfter, stamped, disabledAfterClick, cooledOnReopen });
})();
""")
    assert result["enabledFresh"] is False
    assert result["postedAfter"]["url"] == "/api/auth/accounts/refresh-all-usage?user_initiated=true"
    assert result["postedAfter"]["method"] == "POST"
    assert result["stamped"] is True
    assert result["disabledAfterClick"] is True
    assert result["cooledOnReopen"] is True, "60s cooldown must survive a popover reopen"
