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
eval(fs.readFileSync(__USAGE__, 'utf8'));
eval(fs.readFileSync(__GROUPING__, 'utf8'));
eval(fs.readFileSync(__PANEL__, 'utf8'));
"""


def _run(tmp_path, snippet):
    program = (
        _HARNESS
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


@pytest.mark.parametrize("js_file", [USAGE_JS, GROUPING_JS, PANEL_JS], ids=lambda p: p.name)
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
