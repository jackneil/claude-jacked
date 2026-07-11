"""Integrations tab (agent-reach card + relocated Chrome DevTools widget).

String-level assertions against the shipped web JS, per the repo's web-JS test
idiom. Assert on STABLE markers (element ids, data attributes, function names,
API paths), never on styling class strings — Tailwind polish must not break
these tests (see test_web_js_swap_ui.py history).
"""
import re
from pathlib import Path

WEB = Path(__file__).parent.parent.parent / "jacked" / "data" / "web"
INTEGRATIONS_JS = (WEB / "js" / "components" / "integrations.js").read_text(encoding="utf-8")
SETTINGS_JS = (WEB / "js" / "components" / "settings.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


# --- wiring -----------------------------------------------------------------

def test_index_html_loads_integrations_component():
    assert '/js/components/integrations.js' in INDEX_HTML


def test_settings_tab_bar_includes_integrations():
    assert 'data-tab="integrations"' in SETTINGS_JS


def test_settings_tab_switch_renders_integrations():
    assert "case 'integrations':" in SETTINGS_JS
    assert "renderIntegrationsTab(container)" in SETTINGS_JS


def test_component_defines_tab_entry_point():
    assert "async function renderIntegrationsTab(container)" in INTEGRATIONS_JS


# --- agent-reach card stable markers -----------------------------------------

def test_card_stable_markers_exist():
    for marker in (
        'id="reach-card"',
        'id="reach-unvetted-badge"',
        'id="reach-breakglass-ack"',
        'id="reach-breakglass-ref"',
        'id="reach-breakglass-apply"',
        'id="reach-doctor-table"',
        'id="reach-drift-warning"',
        'class="reach-channel-enable',
        'id="reach-op-error"',
    ):
        assert marker in INTEGRATIONS_JS, f"missing stable marker: {marker}"


def test_card_talks_to_the_reach_api():
    for path in (
        "/api/integrations/agent-reach",
        "/api/integrations/agent-reach/channels/",
        "/api/integrations/agent-reach/override",
    ):
        assert path in INTEGRATIONS_JS, f"missing API path: {path}"


def test_break_glass_requires_ack_and_sends_confirm_unvetted():
    # The apply button starts disabled and is gated on the ack checkbox.
    assert "disabled>Apply override" in INTEGRATIONS_JS
    assert "applyBtn.disabled = !ack.checked" in INTEGRATIONS_JS
    assert "confirm_unvetted: true" in INTEGRATIONS_JS


def test_burner_account_warning_on_login_channels():
    assert "REACH_LOGIN_CHANNELS" in INTEGRATIONS_JS
    assert "burner" in INTEGRATIONS_JS


def test_channels_field_absent_is_handled_not_crashed():
    assert "Channel list unavailable" in INTEGRATIONS_JS


# --- chrome-devtools widget relocation ---------------------------------------

def test_chrome_widget_lives_in_integrations_component():
    for marker in ('id="cdp-mcp-status"', "_refreshChromeDevToolsStatus", "_initChromeDevToolsMCP"):
        assert marker in INTEGRATIONS_JS, f"missing chrome widget marker: {marker}"


def test_chrome_widget_removed_from_settings_js():
    """The widget must exist in exactly one place; a leftover copy in
    settings.js would collide in global scope and double-bind events."""
    for marker in ("cdp-mcp-status", "_refreshChromeDevToolsStatus", "_initChromeDevToolsMCP"):
        assert marker not in SETTINGS_JS, f"stale chrome widget marker in settings.js: {marker}"


# --- copy quality -------------------------------------------------------------

def test_no_em_dash_outside_comments():
    """User-facing copy must not contain em-dashes; comments are exempt."""
    no_comments = re.sub(r"//[^\n]*", "", INTEGRATIONS_JS)
    no_comments = re.sub(r"/\*.*?\*/", "", no_comments, flags=re.S)
    assert "—" not in no_comments


def test_shas_render_with_tabular_nums():
    assert "tabular-nums" in INTEGRATIONS_JS
