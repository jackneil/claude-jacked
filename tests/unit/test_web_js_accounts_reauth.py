"""Re-auth is an always-available escape hatch — now inside the kebab menu.

Regression for: the Re-auth button only rendered when the DB said the token
was invalid/expired. Accounts revalidate at most hourly, so a token that died
inside that window left the dashboard showing 'valid' with no re-auth
affordance — the escape hatch was hidden exactly when state detection was
wrong.

The affordance later moved off the card face (a permanently-visible Re-auth
button was noise) into the account overflow menu. The always-available
property is unchanged: the Re-auth row renders for every Claude account
regardless of validation_status, and never for Codex accounts (their re-auth
is `codex login`, not browser OAuth). When jacked DOES know re-auth is needed,
the kebab carries an attention dot so the hidden row stays discoverable.
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

ATTENTION_DOT = '<span class="account-menu-dot"'


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


def _menu_panel(html):
    """Everything from the <div class="account-menu ..."> panel onward."""
    marker = '<div class="account-menu '
    assert marker in html, "every card renders an overflow menu panel"
    return html[html.index(marker):]


def _reauth_item(html):
    """Extract the btn-reauth menu item from rendered HTML, or None."""
    marker = "btn-reauth"
    if marker not in html:
        return None
    start = html.rindex("<button", 0, html.index(marker))
    end = html.index("</button>", start)
    return html[start:end]


def test_node_syntax_check():
    proc = subprocess.run(["node", "--check", str(ACCOUNTS_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_valid_claude_account_still_gets_reauth(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 1, "email": "a@x.com", "provider": "claude", "is_active": True,
        "validation_status": "valid", "is_expired": False,
        "has_refresh_token": True, "expires_in_seconds": 999999,
    })
    item = _reauth_item(html)
    assert item is not None, "Re-auth must render even when jacked thinks the token is valid"
    assert 'data-id="1"' in item and 'data-email="a@x.com"' in item
    # It lives in the menu, not on the card face
    assert "btn-reauth" in _menu_panel(html)
    assert 'role="menuitem"' in item
    # A healthy account gets no attention dot on the kebab
    assert ATTENTION_DOT not in html


def test_invalid_account_gets_attention_dot(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 2, "email": "b@x.com", "provider": "claude", "is_active": True,
        "validation_status": "invalid",
    })
    assert _reauth_item(html) is not None
    assert ATTENTION_DOT in html, "invalid account flags the kebab so Re-auth stays discoverable"


def test_expired_api_key_account_gets_attention_dot(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 3, "email": "c@x.com", "provider": "claude", "is_active": True,
        "validation_status": "valid", "is_expired": True, "has_refresh_token": False,
    })
    assert _reauth_item(html) is not None
    assert ATTENTION_DOT in html


def test_disabled_account_still_gets_reauth(tmp_path):
    html = _render_actions(tmp_path, {
        "id": 4, "email": "d@x.com", "provider": "claude", "is_active": False,
        "validation_status": "valid",
    })
    assert _reauth_item(html) is not None, "escape hatch stays available on disabled accounts"


def test_codex_account_never_gets_reauth(tmp_path):
    for status in ("valid", "invalid"):
        html = _render_actions(tmp_path, {
            "id": 5, "email": "e@x.com", "provider": "codex", "is_active": True,
            "validation_status": status,
        })
        assert _reauth_item(html) is None, (
            f"Codex ({status}) must not render browser-OAuth Re-auth; its re-auth is `codex login`"
        )


def test_reauth_is_not_a_standalone_card_button(tmp_path):
    """The old always-visible Re-auth button on the card face is gone."""
    html = _render_actions(tmp_path, {
        "id": 6, "email": "f@x.com", "provider": "claude", "is_active": True,
        "validation_status": "valid",
    })
    before_menu = html[:html.index('<div class="account-menu-wrap')]
    assert "btn-reauth" not in before_menu
