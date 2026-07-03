"""Tests for per-model usage rendering in accounts.js.

renderBindingBar() (the inline binding cap under the 5h/7d bars) and
renderPerModelBars() (the full list in the details expander) reuse
renderUsageBar() from usage.js, so the harness evals both under node and asserts
on the rendered HTML: the binding wrapper is always emitted (so websocket.js can
patch it in place), the provider's model label is shown, and the empty states
behave. Skipped when node is not on PATH.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
USAGE_JS = WEB_JS / "components" / "usage.js"
ACCOUNTS_JS = WEB_JS / "components" / "accounts.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

# accounts.js only declares functions/consts at the top level (no top-level
# execution), so eval'ing it just defines renderBindingBar/renderPerModelBars.
# The only deps those two exercise are renderUsageBar (usage.js) + escapeHtml +
# formatResetTime, all provided here.
_HARNESS = r"""
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.formatResetTime = (iso) => iso ? 'resets soon' : '';
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
eval(fs.readFileSync(__USAGE__, 'utf8'));
eval(fs.readFileSync(__ACCOUNTS__, 'utf8'));
"""


def _run(tmp_path, snippet):
    program = (
        _HARNESS
        .replace("__USAGE__", json.dumps(str(USAGE_JS)))
        .replace("__ACCOUNTS__", json.dumps(str(ACCOUNTS_JS)))
        + "\n" + snippet
    )
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_binding_bar_renders_label_and_pct(tmp_path):
    out = _run(tmp_path, """
        const html = renderBindingBar({usage: {binding_model:
            {label: 'Fable', utilization: 92, resets_at: '2026-07-06T05:00:00Z'}}});
        out({html});
    """)
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "data-binding-bar" in html
    assert "Fable" in html
    assert "92%" in html


def test_binding_bar_empty_wrapper_when_no_binding(tmp_path):
    # The wrapper must ALWAYS be present (stable target for the WS patch), but
    # empty when there's no binding model.
    out = _run(tmp_path, """
        out({none: renderBindingBar({usage: {}}),
             noUsage: renderBindingBar({})});
    """)
    res = json.loads(out.strip().splitlines()[-1])
    for html in (res["none"], res["noUsage"]):
        assert "data-binding-bar" in html
        assert "usage-bar" not in html  # no actual bar rendered inside


def test_per_model_bars_use_provider_label(tmp_path):
    out = _run(tmp_path, """
        const usage = {per_model: {
            fable: {label: 'Fable', utilization: 92, is_active: true},
            sonnet: {label: 'Sonnet', utilization: 4, is_active: false},
        }};
        out({html: renderPerModelBars(usage)});
    """)
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "Per-Model" in html
    assert "Fable" in html and "Sonnet" in html
    assert "92%" in html and "4%" in html


def test_per_model_falls_back_to_static_map_then_key(tmp_path):
    # No label from the API → legacy MODEL_DISPLAY_NAMES map, then raw key.
    out = _run(tmp_path, """
        out({html: renderPerModelBars({per_model: {
            opus: {utilization: 5},
            mystery: {utilization: 3},
        }})});
    """)
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "Opus" in html      # mapped from MODEL_DISPLAY_NAMES
    assert "mystery" in html    # raw key fallback


def test_per_model_empty_state(tmp_path):
    out = _run(tmp_path, """
        out({empty: renderPerModelBars({per_model: {}}),
             none: renderPerModelBars(null)});
    """)
    res = json.loads(out.strip().splitlines()[-1])
    assert "No per-model breakdown" in res["empty"]
    assert "No per-model breakdown" in res["none"]
