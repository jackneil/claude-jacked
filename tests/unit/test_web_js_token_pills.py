"""Codex accounts render a single signed-in pill — not the Claude Usage/CC pills.

Regression for: a Codex card showed "Usage Token · invalid" + a "CC Token · add"
prompt, both of which are Claude-specific and meaningless for Codex.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TOKEN_PILLS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "components" / "token-pills.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _render(tmp_path, acct):
    program = (
        "global.escapeHtml=(s)=>String(s==null?'':s)"
        ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
        ".replace(/\"/g,'&quot;');\n"
        f"eval(require('fs').readFileSync({json.dumps(str(TOKEN_PILLS))},'utf8'));\n"
        f"process.stdout.write('\\n'+JSON.stringify(renderTokenPills({json.dumps(acct)}))+'\\n');\n"
    )
    script = tmp_path / "h.js"
    script.write_text(program)
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def test_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(TOKEN_PILLS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_codex_valid_renders_signed_in_only(tmp_path):
    html = _render(tmp_path, {"provider": "codex", "validation_status": "valid",
                              "id": 2, "email": "x@x.com"})
    assert "signed in" in html
    assert "Usage Token" not in html  # no Anthropic token pill
    assert "CC Token" not in html     # no Claude Code token pill


def test_codex_invalid_renders_relogin(tmp_path):
    html = _render(tmp_path, {"provider": "codex", "validation_status": "invalid",
                              "id": 2, "email": "x@x.com"})
    assert "re-login" in html
    assert "Usage Token" not in html and "CC Token" not in html


def test_claude_still_renders_usage_and_cc_pills(tmp_path):
    html = _render(tmp_path, {
        "provider": "claude", "validation_status": "valid", "id": 1,
        "email": "c@x.com", "expires_in_seconds": 999999, "is_expired": False,
        "has_refresh_token": True, "has_cc_token": True, "cc_expires_at": 9999999999,
        "has_cc_refresh_token": True, "cc_needs_auth": False,
    })
    assert "Usage Token" in html and "CC Token" in html
