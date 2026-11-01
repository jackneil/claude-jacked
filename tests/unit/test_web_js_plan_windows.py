"""Plan-aware usage windows in the dashboard and the menu-bar panel.

The server sends every account a ``plan`` object (the plan name, its former
name, catalog drift) and a ``windows`` object (the state and length of the
5-hour and 7-day windows), and every model-scoped limit a ``window_seconds``.
These tests eval the REAL usage.js helpers (no stubs for the bar code) next to
accounts.js, panel.js and websocket.js under node and assert on the HTML:

* every bar class (card binding row, per-model list, panel binding row and
  the WebSocket binding refresh) draws the white elapsed marker when its
  window has a length and a future reset, and none without a reset;
* a 5-hour window that the plan does not have reads "none" and
  "not on Ultra" with the catalog note as the hover; a window the provider
  did not report keeps "n/a" and "not reported"; a temporary window keeps a
  normal bar with its note in the hover;
* the plan label comes from ``plan.label`` (no tier multiplier regex), and
  a WebSocket event refreshes it only when the event carries a plan.

Skipped when node is not on PATH.
"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
UTILS_JS = WEB_JS / "utils.js"
PROVIDER_JS = WEB_JS / "util" / "provider.js"
GROUPING_JS = WEB_JS / "util" / "account-grouping.js"
USAGE_JS = WEB_JS / "components" / "usage.js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"
PANEL_JS = WEB_JS / "components" / "panel.js"
WEBSOCKET_JS = WEB_JS / "websocket.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

EM_DASH = "—"
EN_DASH = "–"
ULTRA_NOTE = "Verified Ultra accounts have no 5-hour limit. Weekly limits still apply."
TEMP_NOTE = (
    "This Ultra account has a temporary 5-hour limit. "
    "Anthropic removes it after it verifies the account."
)
DRIFT_HINT = "The plan catalog in this jacked release does not match this account. Update jacked."

# A minimal DOM: enough for websocket.js to load (it registers a
# visibilitychange listener at load time) and for _usageUpdateCardDOM to swap
# nodes through document.createElement('div').insertAdjacentHTML. eval is safe
# here: it only executes first-party component source checked into this repo.
_HARNESS = r"""
const fs = require('fs');
global.window = { jackedState: { settings: {}, swapSettings: {}, accounts: [] } };
function fakeNode(html) {
    const node = { outerHTML: html, parent: null };
    node.replaceWith = (other) => {
        const kids = node.parent.children;
        kids[kids.indexOf(node)] = other;
        other.parent = node.parent;
    };
    return node;
}
global.document = {
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    documentElement: { classList: { contains: () => false } },
    createElement: () => {
        const box = { html: '' };
        box.insertAdjacentHTML = (_pos, html) => { box.html = html + box.html; };
        Object.defineProperty(box, 'firstElementChild', {
            get: () => (box.html.trim() ? fakeNode(box.html.trim()) : null),
        });
        return box;
    },
};
global.TOKEN_EXPIRY_WARN_SECS = 3600;
const _files = __FILES__;
for (const f of _files) eval(fs.readFileSync(f, 'utf8'));
// utils.js escapeHtml needs a real DOM; the harness has none.
escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
// Sibling components the card calls into; not under test here.
global.renderTokenPills = () => '';
global.renderActiveSessions = () => '';
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
const DAY = 86400 * 1000;
const iso = (ms) => new Date(ms).toISOString();
// A card as accounts.js renders it, reduced to the nodes websocket.js patches.
function fakeCard(planHtml) {
    const bar5 = fakeNode('<div>old 5h</div>');
    const bar7 = fakeNode('<div>old 7d</div>');
    const binding = { innerHTML: '', parent: null };
    const usage = {
        children: [bar5, bar7, binding],
        querySelector: (s) => (s === '[data-binding-bar]' ? binding : null),
    };
    usage.children.forEach((n) => { n.parent = usage; });
    const info = { children: [fakeNode(planHtml)] };
    info.children[0].parent = info;
    return {
        usage, binding, info,
        querySelector: (s) => (s === '[data-usage-container]' ? usage
            : s === '[data-plan-label]' ? info.children[0] : null),
    };
}
"""

_PANEL_FILES = [UTILS_JS, PROVIDER_JS, USAGE_JS, GROUPING_JS, PANEL_JS]
_CARD_FILES = [UTILS_JS, PROVIDER_JS, USAGE_JS, ACCOUNTS_JS]
_WS_FILES = [UTILS_JS, PROVIDER_JS, USAGE_JS, ACCOUNTS_JS, WEBSOCKET_JS]


def _run(tmp_path, files, snippet):
    program = (
        _HARNESS.replace("__FILES__", json.dumps([str(f) for f in files]))
        + "\n" + snippet
    )
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _markers(html):
    """Left offsets (percent) of every elapsed marker in the HTML."""
    return [float(x) for x in re.findall(r'class="elapsed-marker" style="left: ([\d.]+)%"', html)]


def _bars(html):
    """The HTML of each bar row, split at the usage-bar track."""
    return html.split('class="usage-bar')[1:]


def _now():
    return int(time.time())


def _ultra(**kw):
    """A verified Ultra account as GET /api/auth/accounts sends it: no 5h window."""
    acct = {
        "id": 1, "provider": "claude", "email": "u@x.com", "is_active": True,
        "validation_status": "valid", "priority": 0,
        "subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
        "usage_cached_at": _now() - 60,
        "cached_usage_5h": None, "cached_5h_resets_at": None,
        "cached_usage_7d": 33.0, "cached_7d_resets_at": None,
        "plan": {"id": "ultra", "label": "Ultra", "formerly": "Max 20x",
                 "label_source": "provider", "drift": [], "catalog_verified_at": "2026-11-01"},
        "windows": {
            "five_hour": {"state": "absent", "temporary": False, "seconds": 18000, "note": ULTRA_NOTE},
            "seven_day": {"state": "present", "temporary": False, "seconds": 604800, "note": None},
        },
    }
    acct.update(kw)
    return acct


# A Fable limit two days into its week: 2/7 of the window has elapsed.
_FABLE_JS = (
    "const fable = { label: 'Fable', utilization: 92, severity: 'critical', is_active: true,\n"
    "    resets_at: iso(Date.now() + 5 * DAY), window_seconds: 604800 };\n"
)


# ---------------------------------------------------------------------------
# The elapsed marker on every model-scoped bar
# ---------------------------------------------------------------------------


class TestModelBarMarker:
    def test_card_binding_bar_draws_the_week_marker(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, _FABLE_JS + "out(renderBindingBar({usage: {binding_model: fable}}));")
        assert "data-binding-bar" in html
        assert _markers(html) == [pytest.approx(28.6, abs=0.15)]

    def test_per_model_list_draws_a_marker_per_limit_with_a_reset(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, _FABLE_JS + """
out(renderPerModelBars({per_model: {
    fable: fable,
    sonnet: { label: 'Sonnet', utilization: 4, resets_at: null, window_seconds: 604800 },
}}));
""")
        assert "Per-Model Breakdown" in html
        assert "7-Day" not in html, "a Codex named limit is not always weekly"
        fable_row, sonnet_row = _bars(html)
        assert _markers(fable_row) == [pytest.approx(28.6, abs=0.15)]
        assert _markers(sonnet_row) == [], "no reset, no marker"

    def test_per_model_list_keeps_the_static_label_fallback(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, """
out(renderPerModelBars({per_model: { opus: { utilization: 5 }, mystery: { utilization: 3 } }}));
""")
        assert ">Opus</span>" in html and ">mystery</span>" in html

    def test_codex_named_limit_uses_its_own_length(self, tmp_path):
        """A 5-hour named limit two hours from its reset is 60% elapsed."""
        html = _run(tmp_path, _CARD_FILES, """
out(renderModelBar({ label: 'GPT-5.3-Codex-Spark', utilization: 10,
    resets_at: iso(Date.now() + 2 * 3600 * 1000), window_seconds: 18000 }));
""")
        assert _markers(html) == [pytest.approx(60.0, abs=0.15)]

    def test_no_window_length_keeps_todays_markerless_bar(self, tmp_path):
        """A payload from an older server has no window_seconds."""
        html = _run(tmp_path, _CARD_FILES, """
out(renderModelBar({ label: 'Fable', utilization: 50, resets_at: iso(Date.now() + 5 * DAY) }));
""")
        assert _markers(html) == []
        assert ">50%</span>" in html

    def test_panel_binding_row_draws_the_week_marker(self, tmp_path):
        html = _run(tmp_path, _PANEL_FILES, _FABLE_JS + """
const org = { id: 3, email: 'p@x.com', provider: 'claude', usage_cached_at: Math.floor(Date.now() / 1000) - 60,
    cached_usage_5h: 10, cached_usage_7d: 66, usage: { binding_model: fable } };
out(orgBarsHtml(org));
""")
        five, seven, model = _bars(html)
        assert _markers(five) == [] and _markers(seven) == []
        assert _markers(model) == [pytest.approx(28.6, abs=0.15)]
        # A row's label precedes its track, so it ends the previous chunk.
        assert ">Fable</span>" in seven

    def test_panel_binding_row_without_a_reset_has_no_marker(self, tmp_path):
        html = _run(tmp_path, _PANEL_FILES, """
const org = { id: 3, email: 'p@x.com', cached_usage_5h: 10, cached_usage_7d: 66,
    usage: { binding_model: { label: 'Fable', utilization: 92, resets_at: null, window_seconds: 604800 } } };
out(orgBarsHtml(org));
""")
        assert _markers(html) == []

    def test_websocket_binding_refresh_draws_the_week_marker(self, tmp_path):
        r = _run(tmp_path, _WS_FILES, _FABLE_JS + """
const acct = { id: 1, provider: 'claude', cached_usage_5h: 10, cached_usage_7d: 40,
    usage_cached_at: Math.floor(Date.now() / 1000) - 60 };
window.jackedState.accounts = [acct];
const card = fakeCard('<span data-plan-label>Max subscription</span>');
// The active-poll payload: flat fields plus a compact _binding_model.
_usageUpdateCardDOM(card, { id: 1, cached_usage_5h: 12, cached_usage_7d: 41, _binding_model: fable });
const withReset = card.binding.innerHTML;
_usageUpdateCardDOM(card, { id: 1, _binding_model: Object.assign({}, fable, { resets_at: null }) });
out({ withReset, noReset: card.binding.innerHTML, stateBinding: acct.usage.binding_model.label });
""")
        assert _markers(r["withReset"]) == [pytest.approx(28.6, abs=0.15)]
        assert ">Fable</span>" in r["withReset"]
        assert _markers(r["noReset"]) == []
        assert r["stateBinding"] == "Fable"

    def test_card_window_bars_use_the_servers_window_length(self, tmp_path):
        """A 7d reset two days out on a 7-day window puts the marker at 5/7."""
        html = _run(tmp_path, _CARD_FILES, """
const a = { id: 1, provider: 'claude', is_active: true, validation_status: 'valid', priority: 0,
    cached_usage_5h: 10, cached_5h_resets_at: iso(Date.now() + 2 * 3600 * 1000),
    cached_usage_7d: 40, cached_7d_resets_at: iso(Date.now() + 2 * DAY),
    windows: { five_hour: { state: 'present', temporary: false, seconds: 18000, note: null },
               seven_day: { state: 'present', temporary: false, seconds: 604800, note: null } } };
out(renderAccountCard(a, 0, 1));
""")
        five, seven = _bars(html)[:2]
        assert _markers(five) == [pytest.approx(60.0, abs=0.15)]
        assert _markers(seven) == [pytest.approx(71.4, abs=0.15)]


class TestWindowElapsed:
    def test_reads_the_reset_and_the_window_length_of_the_key(self, tmp_path):
        r = _run(tmp_path, [USAGE_JS], """
const a = { cached_5h_resets_at: iso(Date.now() + 1 * 3600 * 1000),
            cached_7d_resets_at: iso(Date.now() + 1 * 3600 * 1000),
            windows: { five_hour: { seconds: 4 * 3600 }, seven_day: { seconds: 2 * 3600 } } };
out([windowElapsed(a, 'five_hour'), windowElapsed(a, 'seven_day')]);
""")
        assert r[0] == pytest.approx(0.75, abs=0.01)
        assert r[1] == pytest.approx(0.5, abs=0.01)

    def test_older_server_payload_uses_the_5h_and_7d_defaults(self, tmp_path):
        r = _run(tmp_path, [USAGE_JS], """
const reset = iso(Date.now() + 2.5 * 3600 * 1000);
const a = { cached_5h_resets_at: reset, cached_7d_resets_at: reset };
const bad = { cached_5h_resets_at: reset, windows: { five_hour: { seconds: 0 } } };
out([windowElapsed(a, 'five_hour'), windowElapsed(a, 'seven_day'), windowElapsed(bad, 'five_hour')]);
""")
        assert r[0] == pytest.approx(0.5, abs=0.01)
        assert r[1] == pytest.approx(1 - 2.5 / 168, abs=0.01)
        assert r[2] == pytest.approx(0.5, abs=0.01)

    def test_no_reset_or_an_unknown_key_has_no_marker(self, tmp_path):
        r = _run(tmp_path, [USAGE_JS], """
out([windowElapsed({ cached_5h_resets_at: null }, 'five_hour'),
     windowElapsed({ cached_7d_resets_at: '2020-01-01T00:00:00Z' }, 'seven_day'),
     windowElapsed({ cached_5h_resets_at: iso(Date.now() + 3600 * 1000) }, 'hourly'),
     windowElapsed(null, 'five_hour')]);
""")
        assert r == [None, None, None, None]


# ---------------------------------------------------------------------------
# Window states: absent, unknown, temporary
# ---------------------------------------------------------------------------


class TestAbsentWindow:
    def test_card_reads_none_and_not_on_ultra_with_the_note(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, f"out(renderAccountCard({json.dumps(_ultra())}, 0, 1));")
        five = _bars(html)[0]
        assert ">none</span>" in five
        assert ">not on Ultra</span>" in five
        assert f'title="{ULTRA_NOTE}"' in five
        assert 'class="fill' not in five, "no fill: an absent window is not free headroom"
        assert _markers(five) == []
        assert "0%" not in five and "n/a" not in five
        assert EM_DASH not in five and EN_DASH not in five

    def test_a_stale_5h_reset_never_shows_on_an_absent_window(self, tmp_path):
        """A window the plan does not have has no reset; the reason wins."""
        html = _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(_ultra())};
a.cached_5h_resets_at = iso(Date.now() + 2 * 3600 * 1000);
out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, windowElapsed(a, 'five_hour'), '5h limit',
    usageBarOpts(a, 'five_hour')));
""")
        assert ">not on Ultra</span>" in html
        assert "resets " not in html
        assert _markers(html) == []

    def test_panel_caption_reads_not_on_ultra(self, tmp_path):
        html = _run(tmp_path, _PANEL_FILES, f"out(orgBarsHtml({json.dumps(_ultra())}));")
        five = _bars(html)[0]
        assert ">none</span>" in five
        assert ">not on Ultra</span>" in five
        assert f"5h · {ULTRA_NOTE}" in html, "the row hover carries the note"

    def test_reason_shape(self, tmp_path):
        r = _run(tmp_path, [USAGE_JS], f"out(windowReason({json.dumps(_ultra())}, 'five_hour'));")
        assert r == {
            "text": "not on Ultra", "short": "not on Ultra", "tone": "muted", "muted": False,
            "absent": True, "none": True, "hint": ULTRA_NOTE,
        }

    def test_long_plan_label_gets_the_short_caption(self, tmp_path):
        acct = _ultra(plan={"label": "Enterprise Plus", "drift": []})
        r = _run(tmp_path, [USAGE_JS], f"out(windowReason({json.dumps(acct)}, 'five_hour'));")
        assert r["text"] == "not on Enterprise Plus"
        assert r["short"] == "no 5h limit"

    def test_no_plan_label_and_no_note(self, tmp_path):
        acct = _ultra(plan=None)
        acct["windows"]["five_hour"]["note"] = None
        r = _run(tmp_path, [USAGE_JS], f"out(windowReason({json.dumps(acct)}, 'five_hour'));")
        assert r["text"] == r["short"] == "no 5h limit"
        assert r["hint"] == "This plan has no 5-hour limit."

    def test_absent_7d_window(self, tmp_path):
        acct = _ultra(provider="codex", cached_usage_7d=None)
        acct["windows"]["seven_day"] = {"state": "absent", "temporary": False, "seconds": 604800, "note": None}
        r = _run(tmp_path, [USAGE_JS], f"""
const a = {json.dumps(acct)};
out({{ reason: windowReason(a, 'seven_day'),
       html: renderUsageBar(a.cached_usage_7d, a.cached_7d_resets_at, windowElapsed(a, 'seven_day'),
                            '7d limit', usageBarOpts(a, 'seven_day')) }});
""")
        assert r["reason"]["text"] == r["reason"]["short"] == "no 7d limit"
        assert r["reason"]["hint"] == "This plan has no 7-day limit."
        assert ">none</span>" in r["html"]

    def test_every_absent_short_form_fits_the_compact_caption(self, tmp_path):
        labels = ["Ultra", "Pro", "Max", "Team", "Enterprise", "A very long plan name", ""]
        snippet = "const res = [];\n"
        for label in labels:
            acct = _ultra(plan={"label": label, "drift": []})
            snippet += f"res.push(windowReason({json.dumps(acct)}, 'five_hour').short);\n"
        snippet += "out(res);"
        for short in _run(tmp_path, [USAGE_JS], snippet):
            assert len(short) <= 14, short


class TestUnknownWindow:
    def _codex(self, **kw):
        acct = {
            "id": 2, "provider": "codex", "usage_cached_at": _now() - 60,
            "cached_usage_5h": None, "cached_5h_resets_at": None,
            "cached_usage_7d": 45.0, "cached_7d_resets_at": "2099-01-01T00:00:00Z",
            "plan": {"id": None, "label": "Plus", "formerly": None, "drift": []},
            "windows": {
                "five_hour": {"state": "unknown", "temporary": False, "seconds": 18000, "note": None},
                "seven_day": {"state": "present", "temporary": False, "seconds": 604800, "note": None},
            },
        }
        acct.update(kw)
        return acct

    def test_codex_unknown_window_keeps_n_a_and_not_reported(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(self._codex())};
out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, windowElapsed(a, 'five_hour'), '5h limit',
    usageBarOpts(a, 'five_hour')));
""")
        assert ">n/a</span>" in html
        assert ">not reported</span>" in html
        assert 'title="Codex does not report this limit for this account."' in html
        assert ">none</span>" not in html
        assert 'class="fill' not in html

    def test_claude_unknown_window_names_anthropic(self, tmp_path):
        acct = _ultra()
        acct["windows"]["five_hour"] = {"state": "unknown", "temporary": False, "seconds": 18000, "note": None}
        r = _run(tmp_path, [USAGE_JS], f"out(windowReason({json.dumps(acct)}, 'five_hour'));")
        assert r["text"] == "not reported"
        assert r["absent"] is True and "none" not in r
        assert r["hint"] == "Anthropic does not report this limit for this account."

    def test_unknown_window_with_a_reading_keeps_its_bar(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(self._codex(cached_usage_5h=30.0))};
out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, windowElapsed(a, 'five_hour'), '5h limit',
    usageBarOpts(a, 'five_hour')));
""")
        assert ">30%</span>" in html
        assert "not reported" not in html

    def test_codex_present_window_without_a_reading_is_never_zero(self, tmp_path):
        """Codex never turns a missing reading into 0%, whatever the state."""
        acct = self._codex()
        acct["windows"]["five_hour"]["state"] = "present"
        r = _run(tmp_path, [USAGE_JS], f"out(windowReason({json.dumps(acct)}, 'five_hour'));")
        assert r["text"] == "not reported"

    def test_claude_present_window_without_a_reading_reads_zero(self, tmp_path):
        """An idle Claude 5-hour window is 0% used: the truth, not a gap."""
        acct = _ultra(plan={"label": "Max", "drift": []})
        acct["windows"]["five_hour"] = {"state": "present", "temporary": False, "seconds": 18000, "note": None}
        html = _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(acct)};
out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, windowElapsed(a, 'five_hour'), '5h limit',
    usageBarOpts(a, 'five_hour')));
""")
        assert ">0%</span>" in html
        assert "not reported" not in html and ">none</span>" not in html


class TestTemporaryWindow:
    def _temp(self):
        acct = _ultra(cached_usage_5h=40.0)
        acct["windows"]["five_hour"] = {
            "state": "present", "temporary": True, "seconds": 18000, "note": TEMP_NOTE,
        }
        return acct

    def test_card_keeps_a_normal_bar_with_the_note_in_the_hover(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(self._temp())};
a.cached_5h_resets_at = iso(Date.now() + 2 * 3600 * 1000);
out(renderAccountCard(a, 0, 1));
""")
        five = _bars(html)[0]
        assert 'class="fill green" style="width: 40.0%"' in five
        assert ">40%</span>" in five
        assert _markers(five) == [pytest.approx(60.0, abs=0.15)]
        row_title = re.search(r'<div class="flex items-center gap-3 mb-1" title="([^"]*)"', html)
        assert row_title, "a row with a window note carries a hover title"
        assert row_title.group(1).startswith("5h limit · resets ")
        assert row_title.group(1).endswith(f" · {TEMP_NOTE}")
        reset_title = re.search(r'class="reset-text[^"]*" title="([^"]*)"', five).group(1)
        assert reset_title.endswith(f" · {TEMP_NOTE}"), "the reset column hover carries it too"

    def test_compact_row_title_carries_the_note(self, tmp_path):
        html = _run(tmp_path, _PANEL_FILES, f"""
const a = {json.dumps(self._temp())};
a.cached_5h_resets_at = iso(Date.now() + 2 * 3600 * 1000);
out(orgBarsHtml(a));
""")
        five = html.split('class="usage-bar')[0]
        assert f"· {TEMP_NOTE}" in five
        assert ">40%</span>" in _bars(html)[0]

    def test_options_carry_the_hint_only_for_a_temporary_window(self, tmp_path):
        r = _run(tmp_path, [USAGE_JS], f"""
const t = {json.dumps(self._temp())};
const u = {json.dumps(_ultra())};
out({{ temp: usageBarOpts(t, 'five_hour', {{ compact: true }}), absent: usageBarOpts(u, 'five_hour'),
       weekly: usageBarOpts(t, 'seven_day') }});
""")
        assert r["temp"]["hint"] == TEMP_NOTE
        assert r["temp"]["compact"] is True
        assert "hint" not in r["absent"]
        assert "hint" not in r["weekly"]


# ---------------------------------------------------------------------------
# Plan labels
# ---------------------------------------------------------------------------


class TestPlanLabel:
    def _label(self, tmp_path, acct):
        return _run(tmp_path, _CARD_FILES, f"""
const a = {json.dumps(acct)};
out({{ text: getSubDisplay(a), html: renderPlanLabel(a) }});
""")

    def test_ultra_reads_ultra_with_the_former_name_in_the_hover(self, tmp_path):
        r = self._label(tmp_path, _ultra())
        assert r["text"] == "Ultra subscription"
        assert "data-plan-label" in r["html"]
        assert 'title="Formerly Max 20x"' in r["html"]
        assert ">Ultra subscription</span>" in r["html"]
        assert "20x tier" not in r["html"]
        assert "decoration-dotted" not in r["html"]

    def test_max_and_team(self, tmp_path):
        max_ = self._label(tmp_path, {"subscription_type": "max", "rate_limit_tier": "default_claude_max_5x",
                                      "plan": {"label": "Max", "formerly": "Max 5x", "drift": []}})
        team = self._label(tmp_path, {"subscription_type": "team",
                                      "plan": {"label": "Team", "formerly": None, "drift": []}})
        assert max_["text"] == "Max subscription"
        assert 'title="Formerly Max 5x"' in max_["html"]
        assert team["text"] == "Team subscription"
        assert "title=" not in team["html"]

    def test_older_server_falls_back_without_a_tier_multiplier(self, tmp_path):
        r = self._label(tmp_path, {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"})
        assert r["text"] == "Max subscription"
        assert "20x" not in r["html"]
        assert self._label(tmp_path, {})["text"] == "Unknown subscription"

    def test_drift_adds_the_update_hint_and_a_dotted_underline(self, tmp_path):
        acct = _ultra()
        acct["plan"]["drift"] = ["renamed"]
        r = self._label(tmp_path, acct)
        assert f'title="Formerly Max 20x. {DRIFT_HINT}"' in r["html"]
        assert "decoration-dotted" in r["html"]
        assert EM_DASH not in r["html"] and EN_DASH not in r["html"]

    def test_label_is_escaped(self, tmp_path):
        r = self._label(tmp_path, {"plan": {"label": "<b>x</b>", "formerly": '"q"', "drift": []}})
        assert "<b>" not in r["html"]
        assert "&lt;b&gt;x&lt;/b&gt; subscription" in r["html"]
        assert 'title="Formerly &quot;q&quot;"' in r["html"]

    def test_card_renders_the_stable_plan_label_target(self, tmp_path):
        html = _run(tmp_path, _CARD_FILES, f"out(renderAccountCard({json.dumps(_ultra())}, 0, 1));")
        assert html.count("data-plan-label") == 1
        assert ">Ultra subscription</span>" in html

    def test_panel_badge(self, tmp_path):
        r = _run(tmp_path, _PANEL_FILES, f"""
out({{ ultra: orgMetaHtml({json.dumps(_ultra())}, null),
       label: planLabel({json.dumps(_ultra())}),
       old: planLabel({{ subscription_type: 'max', rate_limit_tier: 'default_claude_max_20x' }}),
       pro: orgMetaHtml({{ id: 4, subscription_type: 'pro' }}, null),
       none: planLabel({{}}) }});
""")
        assert r["label"] == "Ultra"
        assert '<span class="plan-badge" title="Formerly Max 20x">Ultra</span>' in r["ultra"]
        assert r["old"] == "Max"
        assert '<span class="plan-badge">Pro</span>' in r["pro"]
        assert r["none"] == ""


# ---------------------------------------------------------------------------
# WebSocket card refresh
# ---------------------------------------------------------------------------


class TestWebSocketRefresh:
    def test_partial_poll_event_keeps_the_absent_window(self, tmp_path):
        """The active-poll payload carries no windows: the state account does."""
        r = _run(tmp_path, _WS_FILES, f"""
window.jackedState.accounts = [{json.dumps(_ultra())}];
const card = fakeCard(renderPlanLabel(window.jackedState.accounts[0]));
_usageUpdateCardDOM(card, {{ id: 1, cached_usage_5h: null, cached_usage_7d: 35,
    cached_5h_resets_at: null, cached_7d_resets_at: iso(Date.now() + 2 * DAY), _binding_model: null }});
out({{ five: card.usage.children[0].outerHTML, seven: card.usage.children[1].outerHTML,
       plan: card.info.children[0].outerHTML }});
""")
        assert ">none</span>" in r["five"]
        assert ">not on Ultra</span>" in r["five"]
        assert ">35%</span>" in r["seven"]
        assert _markers(r["seven"]) == [pytest.approx(71.4, abs=0.15)]
        assert ">Ultra subscription</span>" in r["plan"]

    def test_event_with_a_plan_updates_the_label_and_one_without_keeps_it(self, tmp_path):
        r = _run(tmp_path, _WS_FILES, """
const acct = { id: 1, provider: 'claude', subscription_type: 'max', cached_usage_5h: 10, cached_usage_7d: 20,
    plan: { label: 'Max', formerly: null, drift: [] } };
window.jackedState.accounts = [acct];
const card = fakeCard(renderPlanLabel(acct));
const before = card.info.children[0].outerHTML;
_usageUpdateCardDOM(card, { id: 1, cached_usage_7d: 21,
    plan: { label: 'Ultra', formerly: 'Max 20x', drift: [] } });
const changed = card.info.children[0].outerHTML;
_usageUpdateCardDOM(card, { id: 1, cached_usage_7d: 22 });
const kept = card.info.children[0].outerHTML;
out({ before, changed, kept });
""")
        assert ">Max subscription</span>" in r["before"]
        assert ">Ultra subscription</span>" in r["changed"]
        assert 'title="Formerly Max 20x"' in r["changed"]
        assert r["kept"] == r["changed"]

    def test_event_for_an_account_not_in_state_without_a_plan_keeps_the_label(self, tmp_path):
        r = _run(tmp_path, _WS_FILES, """
window.jackedState.accounts = [];
const card = fakeCard('<span data-plan-label>Team subscription</span>');
_usageUpdateCardDOM(card, { id: 9, cached_usage_5h: 5, cached_usage_7d: 6 });
out(card.info.children[0].outerHTML);
""")
        assert r == "<span data-plan-label>Team subscription</span>"

    def test_full_refresh_payload_with_windows_renders_the_absent_window(self, tmp_path):
        """usage_refresh_progress sends the full account response."""
        r = _run(tmp_path, _WS_FILES, f"""
window.jackedState.accounts = [{{ id: 1, provider: 'claude', cached_usage_5h: 50, cached_usage_7d: 20 }}];
const card = fakeCard('<span data-plan-label>Max subscription</span>');
_usageUpdateCardDOM(card, {json.dumps(_ultra())});
out({{ five: card.usage.children[0].outerHTML, plan: card.info.children[0].outerHTML }});
""")
        assert ">not on Ultra</span>" in r["five"]
        assert ">Ultra subscription</span>" in r["plan"]


# ---------------------------------------------------------------------------
# Next-check countdown before the first active-poll broadcast
# ---------------------------------------------------------------------------


class TestCacheAgeFallback:
    def _age(self, tmp_path, acct):
        return _run(tmp_path, _CARD_FILES, f"""
window.jackedState.swapSettings = {{ auto_swap_enabled: true }};
window.jackedState.activeCredentialAccountId = 1;
const a = {json.dumps(acct)};
window.jackedState.accounts = [a];
out(renderCacheAge(a.usage_cached_at, 1));
""")

    def test_no_broadcast_yet_counts_down_from_the_default_without_a_tier(self, tmp_path):
        """A 5h value of 95 used to read "(critical)" on a guess."""
        html = self._age(tmp_path, {"id": 1, "usage_cached_at": _now() - 60, "cached_usage_5h": 95})
        m = re.search(r"data-next-check[^>]*>(\d+)s([^<]*)<", html)
        assert m, html
        assert 230 <= int(m.group(1)) <= 240
        assert m.group(2) == ""
        assert "critical" not in html and "idle" not in html

    def test_broadcast_interval_and_tier_win(self, tmp_path):
        html = self._age(tmp_path, {"id": 1, "usage_cached_at": _now() - 60, "cached_usage_5h": 5,
                                    "_poll_interval": 180, "_poll_tier": "critical"})
        m = re.search(r"data-next-check[^>]*>(\d+)s \(critical\)<", html)
        assert m, html
        assert 110 <= int(m.group(1)) <= 120
