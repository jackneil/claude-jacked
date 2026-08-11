"""The account-card Re-auth button is an always-available escape hatch.

Regression for: the Re-auth button only rendered when the DB said the token
was invalid/expired. Accounts revalidate at most hourly, so a token that died
inside that window left the dashboard showing 'valid' with no re-auth
affordance — the escape hatch was hidden exactly when state detection was
wrong. The button now renders unconditionally for Claude accounts (subtle
when healthy, alert-blue when jacked knows re-auth is needed) and never for
Codex accounts (their re-auth is `codex login`, not browser OAuth).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ACCOUNTS_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "components" / "accounts.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ALERT_CLASS = "bg-blue-600/20"


def _render_actions(tmp_path, acct):
    # Sloppy-mode eval of the component source so its function declarations
    # leak into module scope where the test snippet can call them. eval is
    # safe here: it only executes first-party component source checked into
    # this repo (the file under test), never external or user-supplied input,
    # and it is the only way to load non-module browser scripts for unit
    # testing (same pattern as test_web_js_swap_ui.py).
    program = (
        "global.escapeHtml=(s)=>String(s==null?'':s)"
        ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')"
        ".replace(/\"/g,'&quot;');\n"
        "global.TOKEN_EXPIRY_WARN_SECS=3600;\n"
        "global.window={jackedState:{activeCredentialAccountId:null}};\n"
        f"eval(require('fs').readFileSync({json.dumps(str(ACCOUNTS_JS))},'utf8'));\n"
        f"process.stdout.write('\\n'+JSON.stringify(renderActionButtons({json.dumps(acct)}))+'\\n');\n"
    )
    script = tmp_path / "h.js"
    script.write_text(program)
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def _reauth_button(html):
    """Extract the btn-reauth button tag from rendered HTML, or None."""
    marker = "btn-reauth"
    if marker not in html:
        return None
    start = html.rindex("<button", 0, html.index(marker))
    end = html.index("</button>", start)
    return html[start:end]


def test_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(ACCOUNTS_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_valid_claude_account_still_gets_reauth_button(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 1, "email": "a@x.com", "provider": "claude", "is_active": True,
        "validation_status": "valid", "is_expired": False,
        "has_refresh_token": True, "expires_in_seconds": 999999,
    })
    btn = _reauth_button(html)
    assert btn is not None, "Re-auth must render even when jacked thinks the token is valid"
    assert ALERT_CLASS not in btn, "healthy account gets subtle styling, not alert blue"
    assert 'data-id="1"' in btn and 'data-email="a@x.com"' in btn


def test_invalid_account_gets_alert_styled_reauth(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 2, "email": "b@x.com", "provider": "claude", "is_active": True,
        "validation_status": "invalid",
    })
    btn = _reauth_button(html)
    assert btn is not None
    assert ALERT_CLASS in btn, "invalid account keeps the prominent alert styling"


def test_expired_api_key_account_gets_alert_styled_reauth(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 3, "email": "c@x.com", "provider": "claude", "is_active": True,
        "validation_status": "valid", "is_expired": True, "has_refresh_token": False,
    })
    btn = _reauth_button(html)
    assert btn is not None
    assert ALERT_CLASS in btn


def test_disabled_account_still_gets_reauth_button(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 4, "email": "d@x.com", "provider": "claude", "is_active": False,
        "validation_status": "valid",
    })
    assert _reauth_button(html) is not None, "escape hatch stays available on disabled accounts"


def test_codex_account_never_gets_reauth_button(tmp_path):
    for status in ("valid", "invalid"):
        html = _render_actions(tmp_path, {
            "id": 5, "email": "e@x.com", "provider": "codex", "is_active": True,
            "validation_status": status,
        })
        assert _reauth_button(html) is None, (
            f"Codex ({status}) must not render browser-OAuth Re-auth; its re-auth is `codex login`"
        )
