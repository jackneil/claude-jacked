"""M5: the shared provider visual-identity util (js/util/provider.js).

Single source of truth for the Claude-vs-Codex mark used by both the dashboard
cards and the panel rows. Evals provider.js under node (same sloppy-eval
harness as the other web-js tests) and asserts brand color/label/glyph per
provider plus the Claude fallback. Skipped when node is not on PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROVIDER_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "util" / "provider.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path, snippet):
    program = f"eval(require('fs').readFileSync({json.dumps(str(PROVIDER_JS))},'utf8'));\n"
    program += "const out=(o)=>process.stdout.write('\\n'+JSON.stringify(o)+'\\n');\n"
    program += snippet
    script = tmp_path / "h.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def test_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(PROVIDER_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_provider_meta_claude_and_codex(tmp_path):
    r = _run(tmp_path, "out({claude: providerMeta('claude'), codex: providerMeta('codex')});")
    assert r["claude"]["label"] == "Claude" and r["claude"]["color"] == "#d97757"
    assert r["codex"]["label"] == "Codex" and r["codex"]["color"] == "#10a37f"
    assert "<svg" in r["claude"]["svg"] and "<svg" in r["codex"]["svg"]


def test_provider_meta_unknown_falls_back_to_claude(tmp_path):
    r = _run(tmp_path, "out({a: providerMeta(undefined), b: providerMeta('weird'), c: providerMeta(null)});")
    assert r["a"]["key"] == "claude"
    assert r["b"]["key"] == "claude"
    assert r["c"]["key"] == "claude"


def test_provider_badge_and_glyph_render(tmp_path):
    r = _run(tmp_path, "out({badge: providerBadge('codex'), glyph: providerGlyph('claude')});")
    assert "provider-badge" in r["badge"] and "provider-codex" in r["badge"]
    assert "Codex" in r["badge"] and "#10a37f" in r["badge"]
    assert "provider-glyph" in r["glyph"] and "provider-claude" in r["glyph"]
    assert 'title="Claude account"' in r["glyph"]
