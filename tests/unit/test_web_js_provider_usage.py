"""Usage text names the account's provider, and Grok shows no usage windows.

usage.js used to call every non-Codex provider "Anthropic" (an unreported
window) or "The provider" (a usage block), so a Grok row named the wrong
company. jacked has no Grok usage reader, so a Grok card showed two empty
bars that read as 0% used. Evals provider.js, usage.js and panel.js under
node. Skipped when node is not on PATH.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
PROVIDER_JS = WEB_JS / "util" / "provider.js"
USAGE_JS = WEB_JS / "components" / "usage.js"
GROUPING_JS = WEB_JS / "util" / "account-grouping.js"
PANEL_JS = WEB_JS / "components" / "panel.js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"
WEBSOCKET_JS = WEB_JS / "websocket.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
global.window = { jackedState: { settings: {} } };
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.formatResetTime = (iso) => iso ? 'resets in 1h' : '';
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
for (const f of __FILES__) eval(fs.readFileSync(f, 'utf8'));
"""


def _run(tmp_path, files, snippet):
    program = _HARNESS.replace("__FILES__", json.dumps([str(f) for f in files])) + "\n" + snippet
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _acct(provider, **extra):
    acct = {
        "id": 9, "email": "g@x.com", "provider": provider, "cached_usage_5h": None,
        "cached_usage_7d": None, "usage_cached_at": 1_700_000_000,
        "windows": {"five_hour": {"state": "unknown"}, "seven_day": {"state": "unknown"}},
    }
    acct.update(extra)
    return acct


@pytest.mark.parametrize("js_file", [USAGE_JS, PANEL_JS, ACCOUNTS_JS, WEBSOCKET_JS],
                         ids=lambda p: p.name)
def test_node_syntax_check(js_file):
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("provider, name", [
    ("claude", "Anthropic"), ("codex", "Codex"), ("grok", "Grok"),
])
def test_the_hover_text_names_the_reporting_provider(tmp_path, provider, name):
    acct = _acct(provider, usage_block={"reason": "limit_reached"})
    r = _run(tmp_path, [PROVIDER_JS, USAGE_JS], f"""
const a = {json.dumps(acct)};
out({{reason: windowReason(a, 'five_hour'), block: usageBlockText(a), has: providerHasUsage(a)}});
""")
    assert r["block"]["title"].startswith(f"Limit reached. {name} blocks new requests")
    assert r["has"] is (provider != "grok")
    if provider != "grok":
        assert r["reason"]["hint"] == f"{name} does not report this limit for this account."


def test_the_name_has_a_fallback_without_provider_js(tmp_path):
    r = _run(tmp_path, [USAGE_JS], "out(usageReporterName({provider: 'grok'}));")
    assert r == "Grok"


@pytest.mark.parametrize("provider, has", [
    (None, True), ("claude", True), ("codex", True), ("grok", False), ("GROK", False),
])
def test_only_providers_with_a_usage_reader_have_usage(tmp_path, provider, has):
    r = _run(tmp_path, [USAGE_JS], f"out(providerHasUsage({json.dumps({'provider': provider})}));")
    assert r is has


def test_a_grok_panel_row_has_no_usage_bars(tmp_path):
    html = _run(tmp_path, [PROVIDER_JS, USAGE_JS, GROUPING_JS, PANEL_JS], f"""
out(orgBarsHtml({json.dumps(_acct('grok'))}));
""")
    assert "data-no-usage" in html and "No usage data" in html
    assert "usage-bar" not in html and "%" not in html


def test_a_claude_panel_row_keeps_its_bars(tmp_path):
    html = _run(tmp_path, [PROVIDER_JS, USAGE_JS, GROUPING_JS, PANEL_JS], f"""
out(orgBarsHtml({json.dumps(_acct('claude', cached_usage_5h=40, cached_usage_7d=20))}));
""")
    assert "data-no-usage" not in html and "40%" in html


def test_the_card_note_names_grok_and_has_no_em_dash(tmp_path):
    html = _run(tmp_path, [PROVIDER_JS, USAGE_JS], f"""
out(renderNoUsageNote({json.dumps(_acct('grok'))}) + '|' + renderNoUsageNote({json.dumps(_acct('codex'))}));
""")
    note, codex = html.split("|")
    assert "jacked cannot read the usage limits of Grok accounts." in note
    assert codex == ""
    assert "—" not in note


def test_the_account_card_hides_bars_and_refresh_for_grok():
    """accounts.js reads providerHasUsage before it renders the bars, the
    usage age and the refresh control (a source check: the card renderer
    needs a full DOM harness)."""
    src = ACCOUNTS_JS.read_text(encoding="utf-8")
    assert "|| providerHasUsage(acct);" in src
    assert ": renderNoUsageNote(acct);" in src
    assert "${hasUsage ? `<span class=\"text-slate-600\">|</span>" in src
    ws = WEBSOCKET_JS.read_text(encoding="utf-8")
    assert "if (usageContainer && !noUsage &&" in ws
