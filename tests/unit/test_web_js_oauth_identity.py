"""The OAuth waiting banner has to name the account it is waiting on.

Re-auth now pre-fills the claude.ai email box and opens a browser profile
dedicated to that account, so the banner is the only place the user can see
which of their accounts is being authorized and where it opened. A banner that
just says "Waiting for authorization" leaves them guessing on the exact screen
where guessing wrong costs a logout.

Node runs the real component source (concatenated into a harness script, so no
eval is involved), which means the copy under test is the copy that ships.
Skipped when node is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

OAUTH_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "components" / "oauth-flows.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Minimal DOM: buildOAuthCodeEntry only creates elements and appends them, and
# textAll flattens the result so a test can assert on the rendered copy.
DOM_STUB = """
function makeEl(tag) {
    return {
        tagName: tag, className: '', textContent: '', type: '', name: '',
        placeholder: '', autocomplete: '', spellcheck: true, href: '',
        target: '', rel: '', hidden: false, value: '',
        children: [],
        appendChild(child) { this.children.push(child); return child; },
        addEventListener() {},
        setAttribute() {},
    };
}
global.document = { createElement: makeEl, getElementById: () => null };
global.window = { jackedState: {}, location: { hostname: 'localhost' } };
function textAll(el) {
    return (el.textContent || '') + el.children.map(textAll).join(' ');
}
"""

ACCENT = {"banner": "b", "subtitle": "s", "link": "l"}
AUTH_URL = "https://claude.com/cai/oauth/authorize?x=1"


def _build(tmp_path, manual, identity):
    identity_js = "undefined" if identity is None else json.dumps(identity)
    harness = (
        "const textDiv = makeEl('div');\n"
        f"buildOAuthCodeEntry(textDiv, {json.dumps(ACCENT)}, "
        f"{json.dumps(AUTH_URL)}, {'true' if manual else 'false'}, {identity_js});\n"
        "process.stdout.write('\\n'+JSON.stringify(textAll(textDiv))+'\\n');\n"
    )
    script = tmp_path / "h.js"
    script.write_text(
        DOM_STUB + OAUTH_JS.read_text(encoding="utf-8") + "\n" + harness,
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.strip()][-1])


def test_node_syntax_check():
    proc = subprocess.run(
        ["node", "--check", str(OAUTH_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_banner_names_the_account_org_and_browser_profile(tmp_path):
    text = _build(tmp_path, False, {
        "email": "a@b.com", "orgName": "Acme", "browserMode": "profile",
    })
    assert "Authorizing a@b.com" in text
    assert "Acme" in text
    assert "Open the authorization page for a@b.com" in text
    assert "dedicated browser profile" in text


def test_private_window_mode_says_so(tmp_path):
    text = _build(tmp_path, False, {"email": "a@b.com", "browserMode": "incognito"})
    assert "Authorizing a@b.com" in text
    assert "private browser window" in text
    assert "dedicated browser profile" not in text


def test_system_default_mode_makes_no_claim_about_the_window(tmp_path):
    text = _build(tmp_path, False, {"email": "a@b.com", "browserMode": "default"})
    assert "Authorizing a@b.com" in text
    assert "dedicated browser profile" not in text
    assert "private browser window" not in text


def test_org_is_optional(tmp_path):
    text = _build(tmp_path, False, {"email": "a@b.com", "browserMode": "profile"})
    assert "Authorizing a@b.com" in text
    assert "\u00b7" not in text


def test_no_identity_renders_the_original_copy(tmp_path):
    """Add Account from a remote dashboard has no identity to show, and must
    not grow an empty "Authorizing" line."""
    text = _build(tmp_path, True, None)
    assert "Authorizing" not in text
    assert "for a@b.com" not in text
    assert "dedicated browser profile" not in text
    assert "private browser window" not in text
    assert "Open the authorization page" in text
    # The manual-mode instructions still render.
    assert "paste it below" in text


def test_identity_without_an_email_is_ignored(tmp_path):
    text = _build(tmp_path, False, {"browserMode": "profile"})
    assert "Authorizing" not in text
    assert "Open the authorization page" in text
    # The browser note is still worth showing.
    assert "dedicated browser profile" in text


def test_no_em_dashes_in_the_banner_copy(tmp_path):
    """User-visible copy uses plain punctuation."""
    text = _build(tmp_path, False, {
        "email": "a@b.com", "orgName": "Acme", "browserMode": "profile",
    })
    assert "\u2014" not in text
