"""Tests for the Skill Packs section renderer in ``settings.js``.

Plain browser JS, no bundler, so this drives ``node`` from pytest exactly like
the sibling ``test_web_js_*`` suites (see ``test_web_js_settings_filter.py`` for
the base harness): it evals the component source in a minimal stub and asserts
on the HTML that ``_renderPacksSection`` returns.

Two wrinkles beyond the filter suite's harness:

1. ``_renderPacksSection`` calls ``escapeHtml`` (defined in ``utils.js``), so we
   eval ``utils.js`` first. ``escapeHtml`` itself does
   ``div.textContent = str; return div.innerHTML.replace(/"/g, '&quot;')`` — it
   needs a DOM primitive. The ``document.createElement`` stub below mirrors a
   real browser's textContent->innerHTML (escapes ``&``, ``<``, ``>``; leaves
   quotes) so the REAL ``escapeHtml`` runs, only the DOM node is faked.

   What escapeHtml DOES cover: the HTML-injection metacharacters ``& < > "``.
   What it does NOT cover: dangerous URL schemes (``javascript:``, ``data:``).
   In a text or attribute-value context those render inert, so the description
   text is safe even when it literally contains ``javascript:...``. The homepage
   is emitted into an ``href`` — escaping neutralizes quote/bracket breakout of
   the attribute, but scheme safety there rides on the registry value being a
   real ``https://`` URL, not on escapeHtml. See the hostile test below.

2. ``_packsInFlight`` is a module-level ``const`` in settings.js. A ``const`` in
   an ``eval`` is scoped to that eval, so the driver that seeds an in-flight
   entry is concatenated INTO the eval'd source (not run after it) — that is the
   only way to reach the binding.

Skipped when node is not on PATH.
"""
import json
import shutil
import subprocess

import pytest

from tests.unit.test_web_js_swap_ui import WEB_JS

SETTINGS_JS = WEB_JS / "components" / "settings.js"
UTILS_JS = WEB_JS / "utils.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _render_packs_section(tmp_path, packs_data, inflight=None):
    """Eval utils.js + settings.js under node and return the HTML string that
    ``_renderPacksSection(packs_data)`` produces.

    ``inflight`` is an optional {pack_name: 'enable'|'disable'} map seeded into
    ``_packsInFlight`` before the render, to exercise the mid-op branch.

    eval() here is safe and intentional: it executes ONLY this repo's own
    first-party JS (never external or user input) in a throwaway node process,
    the same non-module-browser-JS loading pattern the other test_web_js_*
    suites use. All injected values go through json.dumps, so they are data.
    """
    inflight = inflight or {}
    inflight_js = "".join(
        f"_packsInFlight.set({json.dumps(name)}, {json.dumps(mode)});\n"
        for name, mode in inflight.items()
    )
    driver_source = (
        inflight_js
        + f"const __html = _renderPacksSection({json.dumps(packs_data)});\n"
        + "process.stdout.write('\\n<<<PACKS_START>>>\\n');\n"
        + "process.stdout.write(__html);\n"
        + "process.stdout.write('\\n<<<PACKS_END>>>\\n');\n"
    )

    program = f"""
const fs = require('fs');
global.window = {{ jackedState: {{}} }};
// Mirror a real browser's textContent->innerHTML so the REAL escapeHtml (from
// utils.js) runs: escape &, <, > and leave quotes; escapeHtml adds the
// " -> &quot; pass itself. Only the DOM node primitive is stubbed.
global.document = {{
  createElement: () => {{
    let _t = '';
    return {{
      set textContent(v) {{ _t = (v == null) ? '' : String(v); }},
      get innerHTML() {{
        return _t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      }},
    }};
  }},
  getElementById: () => null,
  querySelectorAll: () => [],
  querySelector: () => null,
}};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
const UTILS = fs.readFileSync({json.dumps(str(UTILS_JS))}, 'utf8');
const SETTINGS = fs.readFileSync({json.dumps(str(SETTINGS_JS))}, 'utf8');
const DRIVER = {json.dumps(driver_source)};
// Single eval so the driver shares scope with the `const _packsInFlight`.
eval(UTILS + '\\n' + SETTINGS + '\\n' + DRIVER);
"""
    script = tmp_path / "packs_harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    out = proc.stdout
    start = out.index("<<<PACKS_START>>>") + len("<<<PACKS_START>>>")
    end = out.index("<<<PACKS_END>>>")
    return out[start:end].strip("\n")


def _pack(
    name="marketing",
    display_name="Marketing Skills",
    description="Marketing bundle",
    homepage="https://github.com/coreyhaines31/marketingskills",
    source="coreyhaines31/marketingskills",
    enabled=False,
    installed_count=0,
    total=2,
):
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "homepage": homepage,
        "source": source,
        "enabled": enabled,
        "installed_count": installed_count,
        "total": total,
        "skills": [],
    }


def test_node_syntax_check():
    for path in (UTILS_JS, SETTINGS_JS):
        proc = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr


def test_empty_packs_returns_empty_string(tmp_path):
    """No registry packs -> the whole section collapses to '' (no empty header
    left dangling in the Features tab)."""
    html = _render_packs_section(tmp_path, {"npx_available": True, "packs": []})
    assert html == ""


def test_npx_unavailable_dims_label_and_wires_note(tmp_path):
    """npx missing: the label gets the `disabled` class (CSS dims it + drops the
    pointer, fixing the dead-looking toggle), the input is disabled and points
    at the note via aria-describedby, and the note carries the matching id."""
    data = {"npx_available": False, "packs": [_pack(enabled=False)]}
    html = _render_packs_section(tmp_path, data)

    assert 'class="toggle-switch pack-toggle flex-shrink-0 disabled"' in html
    assert 'id="packs-npx-note"' in html
    assert 'aria-describedby="packs-npx-note"' in html
    # Accessible name on the toggle input.
    assert 'aria-label="Marketing Skills skill pack"' in html
    # Input itself is disabled.
    assert "disabled aria-label=" in html


def test_partial_install_flags_amber_with_retry(tmp_path):
    """Enabled but short (3 of 9 landed): amber count line + a Retry-install
    repair link that re-fires the enable PUT."""
    data = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=3, total=9)],
    }
    html = _render_packs_section(tmp_path, data)

    assert "text-amber-400" in html
    assert "3 of 9 skills installed" in html
    assert "Retry install" in html
    assert 'class="pack-retry' in html
    assert 'data-pack-retry="marketing"' in html


def test_fully_installed_enabled_pack_is_not_amber(tmp_path):
    """Enabled and complete (9 of 9): plain slate count line, no amber, no retry
    link — the repair affordance only shows when something is actually missing."""
    data = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=9, total=9)],
    }
    html = _render_packs_section(tmp_path, data)

    assert "9 of 9 skills installed" in html
    assert "text-amber-400" not in html
    assert "Retry install" not in html


def test_inflight_enable_shows_pending_and_installing(tmp_path):
    """A pack whose enable PUT is running renders with the label `pending`
    class, a disabled input, and 'Installing skills...' in place of the count —
    and this survives a re-render because it reads the module-level map."""
    data = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=3, total=9)],
    }
    html = _render_packs_section(tmp_path, data, inflight={"marketing": "enable"})

    assert "toggle-switch pack-toggle flex-shrink-0 pending" in html
    assert "Installing skills..." in html
    # The stale count is replaced by the spinner text, not shown alongside it.
    assert "3 of 9 skills installed" not in html
    # Input is disabled during the op (checked because enabled=True).
    assert "checked disabled aria-label=" in html


def test_inflight_disable_shows_removing(tmp_path):
    """A disable in flight shows 'Removing skills...' rather than 'Installing'."""
    data = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=9, total=9)],
    }
    html = _render_packs_section(tmp_path, data, inflight={"marketing": "disable"})

    assert "Removing skills..." in html
    assert "Installing skills..." not in html


def test_hostile_description_and_homepage_are_escaped(tmp_path):
    """A malicious registry entry can't inject markup. escapeHtml neutralizes
    the HTML metacharacters (& < > ") so no live <script> or attribute breakout
    survives. It does NOT strip the javascript: scheme — that stays as literal
    text (inert in a text context), which this test documents explicitly."""
    data = {
        "npx_available": True,
        "packs": [
            _pack(
                description='<script>alert(1)</script> javascript:alert(2)',
                homepage='https://evil/"><script>evil()</script>',
            )
        ],
    }
    html = _render_packs_section(tmp_path, data)

    # No live script tag or quote/bracket breakout survives.
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '"><script>' not in html
    # The hostile homepage's double-quote is escaped inside the href attribute.
    assert "&quot;" in html
    # Documented gap: escapeHtml leaves the javascript: scheme intact. It is
    # harmless HERE only because it sits in a text node, not an href.
    assert "javascript:alert(2)" in html


def test_inflight_checkbox_reflects_intent_not_stale_cache(tmp_path):
    """Mid-disable, the cached enabled flag is still True; the checkbox must
    render UNCHECKED (the user's intent) so a re-render never snaps the track
    back to ON under a 'Removing skills...' label — and vice versa mid-enable."""
    disabling = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=9, total=9)],
    }
    html = _render_packs_section(tmp_path, disabling, inflight={"marketing": "disable"})
    assert "checked" not in html.split("aria-label")[0].split("input")[-1]
    assert 'aria-busy="true"' in html

    enabling = {
        "npx_available": True,
        "packs": [_pack(enabled=False, installed_count=0, total=9)],
    }
    html = _render_packs_section(tmp_path, enabling, inflight={"marketing": "enable"})
    assert "checked disabled" in html


def test_retry_link_suppressed_when_npx_missing(tmp_path):
    """A partial pack with no Node must not dangle an actionable Retry link
    next to a dead toggle: the link needs npx exactly like the toggle."""
    data = {
        "npx_available": False,
        "packs": [_pack(enabled=True, installed_count=3, total=9)],
    }
    html = _render_packs_section(tmp_path, data)
    assert "3 of 9 skills installed" in html
    assert "Retry install" not in html


def test_status_line_is_a_live_region(tmp_path):
    """The status div is the one signal for a minute-long operation; it must be
    announced to assistive tech."""
    data = {
        "npx_available": True,
        "packs": [_pack(enabled=True, installed_count=9, total=9)],
    }
    html = _render_packs_section(tmp_path, data)
    assert 'role="status"' in html and 'aria-live="polite"' in html
