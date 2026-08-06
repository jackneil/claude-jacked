"""Tests for the DCR Review Engine card in ``settings.js``.

Plain browser JS, no bundler, so this drives ``node`` from pytest exactly like
the sibling ``test_web_js_*`` suites (see ``test_web_js_packs_render.py`` for the
harness this copies): it evals the component source in a minimal stub and
asserts on the HTML that ``_renderDcrEngineSection`` returns, on the request the
save path actually issues, and on the Features-tab wiring around the card.

Three wrinkles, all inherited from the packs suite:

1. The renderer calls ``escapeHtml`` (defined in ``utils.js``), so ``utils.js``
   is eval'd first. The ``document.createElement`` stub mirrors a real browser's
   textContent->innerHTML (escapes ``&``, ``<``, ``>``; leaves quotes) so the
   REAL ``escapeHtml`` runs and only the DOM node is faked. ``reason`` is a
   server-provided string rendered into the card, so its escaping is pinned
   below with a hostile value.

2. ``_dcrEngineSaving`` / ``_dcrEngineSaveError`` are module-level ``let``
   bindings. A ``let`` in an ``eval`` is scoped to that eval, so any driver that
   seeds them is concatenated INTO the eval'd source rather than run after it.

3. Nothing here touches a real server: ``api`` is stubbed, so the endpoint paths
   and request bodies are observed rather than sent.

Skipped when node is not on PATH.
"""
import json
import re
import shutil
import subprocess

import pytest

from tests.unit.test_web_js_swap_ui import WEB_JS

SETTINGS_JS = WEB_JS / "components" / "settings.js"
UTILS_JS = WEB_JS / "utils.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

# Every effort level the card must offer, weakest to strongest.
EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _program(driver_source, api_stub="global.api = {};"):
    """Build a node program that evals utils.js + settings.js + the driver in a
    SINGLE eval, so the driver shares scope with settings.js's module-level
    bindings.

    eval() here is safe and intentional: it executes ONLY this repo's own
    first-party JS (never external or user input) in a throwaway node process,
    the same non-module-browser-JS loading pattern the other test_web_js_*
    suites use. All injected values go through json.dumps, so they are data.
    """
    return f"""
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
  // The card re-render is a no-op in the harness: there is no live section node.
  getElementById: () => null,
  querySelectorAll: () => [],
  querySelector: () => null,
}};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
global.showToast = () => {{}};
global.renderSettingsTab = async () => {{}};
{api_stub}
const UTILS = fs.readFileSync({json.dumps(str(UTILS_JS))}, 'utf8');
const SETTINGS = fs.readFileSync({json.dumps(str(SETTINGS_JS))}, 'utf8');
const DRIVER = {json.dumps(driver_source)};
eval(UTILS + '\\n' + SETTINGS + '\\n' + DRIVER);
"""


def _run_node(tmp_path, program, name="dcr_harness.js"):
    script = tmp_path / name
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, (
        f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    )
    return proc.stdout


def _capture(out, tag="OUT"):
    start = out.index(f"<<<{tag}_START>>>") + len(f"<<<{tag}_START>>>")
    end = out.index(f"<<<{tag}_END>>>")
    return out[start:end].strip("\n")


def _engine(
    engine="claude",
    model="gpt-5.6-luna",
    effort="xhigh",
    keep_on_claude=("Security", "Frontend Design"),
    usable=True,
    reason=None,
    codex_installed=True,
    codex_logged_in=True,
    codex_path="/usr/local/bin/codex",
    schema_path="/abs/path",
):
    """A GET /api/dcr-engine payload, shaped exactly like the API contract."""
    return {
        "engine": engine,
        "model": model,
        "effort": effort,
        "keep_on_claude": list(keep_on_claude),
        "usable": usable,
        "reason": reason,
        "codex_installed": codex_installed,
        "codex_logged_in": codex_logged_in,
        "codex_path": codex_path,
        "schema_path": schema_path,
    }


def _render_section(tmp_path, data, saving=False, save_error=None, retryable=True):
    """Return the HTML that ``_renderDcrEngineSection(data)`` produces.

    ``saving`` / ``save_error`` / ``retryable`` seed the module-level state that
    the mid-save and failed-save branches read.
    """
    seed = ""
    if saving:
        seed += "_dcrEngineSaving = true;\n"
    if save_error is not None:
        seed += f"_dcrEngineSaveError = {json.dumps(save_error)};\n"
        seed += f"_dcrEngineSaveRetryable = {json.dumps(bool(retryable))};\n"
        seed += "_dcrEngineLastPayload = { engine: 'codex' };\n"
    driver = (
        seed
        + f"const __html = _renderDcrEngineSection({json.dumps(data)});\n"
        + "process.stdout.write('\\n<<<OUT_START>>>\\n');\n"
        + "process.stdout.write(__html);\n"
        + "process.stdout.write('\\n<<<OUT_END>>>\\n');\n"
    )
    return _capture(_run_node(tmp_path, _program(driver)))


def _render_error(tmp_path, message):
    driver = (
        f"const __html = _renderDcrEngineError({json.dumps(message)});\n"
        + "process.stdout.write('\\n<<<OUT_START>>>\\n');\n"
        + "process.stdout.write(__html);\n"
        + "process.stdout.write('\\n<<<OUT_END>>>\\n');\n"
    )
    return _capture(_run_node(tmp_path, _program(driver)))


# --- Source-level pins -------------------------------------------------------


def test_node_syntax_check():
    for path in (UTILS_JS, SETTINGS_JS):
        proc = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr


def test_get_endpoint_path_is_pinned():
    """The card reads its state from GET /api/dcr-engine and publishes the
    response to jackedState so the rest of the card reads one latest copy."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    assert re.search(r"api\.get\(\s*'/api/dcr-engine'\s*\)", src)
    assert "window.jackedState.dcrEngine" in src
    assert "async function loadDcrEngine()" in src


def test_load_dcr_engine_never_caches():
    """Codex readiness is LIVE external state (CLI installed? signed in?), so the
    loader must not short-circuit on a cached value: a user who runs `codex login`
    in a terminal and returns to the tab has to see the new state.

    Source-level because the bug IS the cache guard, so its absence is the pin."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = src[src.index("async function loadDcrEngine()"):]
    body = body[: body.index("\n}\n") + 2]
    assert "if (!window.jackedState.dcrEngine)" not in body
    assert "await api.get('/api/dcr-engine')" in body
    # The invalidate-on-write helper existed only to defeat that cache. With the
    # cache gone it has no callers, so it must not linger as dead code.
    assert "refreshDcrEngine" not in src


def test_load_dcr_engine_refetches_on_every_call(tmp_path):
    """Behavioral half of the pin: two loads, two GETs, and jackedState carries
    the newest response rather than the first."""
    api_stub = """
global.__gets = 0;
global.api = {
  get: async () => { global.__gets += 1; return { engine: 'codex', usable: global.__gets > 1 }; },
};
"""
    driver = """
(async () => {
  const first = await loadDcrEngine();
  const second = await loadDcrEngine();
  process.stdout.write('\\n<<<OUT_START>>>\\n');
  process.stdout.write(JSON.stringify({
    gets: global.__gets,
    first: first,
    second: second,
    cached: window.jackedState.dcrEngine,
  }));
  process.stdout.write('\\n<<<OUT_END>>>\\n');
})();
"""
    state = json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))
    assert state["gets"] == 2
    assert state["first"]["usable"] is False
    assert state["second"]["usable"] is True
    # The fetched value is still published for the rest of the card to read.
    assert state["cached"] == state["second"]


def test_features_tab_renders_card_between_hooks_and_knowledge():
    """Section order is part of the spec: Hooks, then Review Engine, then
    Knowledge."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    hooks = src.index(">Hooks</h3>")
    engine = src.index("${dcrEngineSection}")
    knowledge = src.index(">Knowledge</h3>")
    assert hooks < engine < knowledge


# --- Rendered card -----------------------------------------------------------


def test_section_header_and_subtitle(tmp_path):
    """Header text and the plain-language subtitle, verbatim."""
    html = _render_section(tmp_path, _engine())
    assert "Review Engine" in html
    assert (
        "Choose which AI runs your /dcr code reviews. Claude is the default and "
        "uses your Anthropic plan. Codex sends the review work to OpenAI instead, "
        "which saves your Anthropic usage and costs less." in html
    )
    # Matches the sibling section headers exactly.
    assert (
        '<h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Review Engine</h3>'
        in html
    )


def test_engine_selector_offers_both_engines_with_claude_selected(tmp_path):
    html = _render_section(tmp_path, _engine(engine="claude"))
    assert '<option value="claude" selected>Claude (default)</option>' in html
    assert '<option value="codex" >Codex (OpenAI)</option>' in html


def test_claude_engine_hides_model_effort_and_status(tmp_path):
    """On Claude there is no Codex model, effort, or readiness to show — the
    fields are absent, not dead controls."""
    html = _render_section(tmp_path, _engine(engine="claude"))
    assert 'id="dcr-engine-model"' not in html
    assert 'id="dcr-engine-effort"' not in html
    assert "Codex is ready" not in html
    assert "Reviews fall back to Claude until this is fixed." not in html
    assert 'id="dcr-engine-recheck"' not in html
    # The carve-out note is rendered from the server's list, on either engine.
    assert (
        "Security and Frontend Design reviews always stay on Claude for the "
        "highest quality judgment." in html
    )


def test_codex_engine_shows_model_field_with_placeholder_and_value(tmp_path):
    html = _render_section(tmp_path, _engine(engine="codex", model="gpt-5.6-terra"))
    assert 'id="dcr-engine-model"' in html
    assert 'placeholder="gpt-5.6-luna"' in html
    assert 'value="gpt-5.6-terra"' in html
    assert (
        "Any Codex model name works. gpt-5.6-luna is fast and cheap; "
        "gpt-5.6-terra is stronger." in html
    )


def test_codex_engine_offers_all_seven_effort_levels(tmp_path):
    """All seven levels, with the server's current one selected."""
    html = _render_section(tmp_path, _engine(engine="codex", effort="xhigh"))
    for level in EFFORT_LEVELS:
        assert f'<option value="{level}"' in html, level
    assert '<option value="xhigh" selected>xhigh</option>' in html
    assert "How hard the model thinks. xhigh is the sweet spot for reviews." in html


def test_codex_effort_falls_back_to_a_known_level_when_value_is_unknown(tmp_path):
    """The API sanitizes stored values, so an unknown effort should never reach
    the card. Defensive pin anyway: a select with nothing selected would silently
    PUT its first option ('none') on the next unrelated change."""
    html = _render_section(tmp_path, _engine(engine="codex", effort="turbo"))
    assert '<option value="turbo"' not in html
    assert '<option value="xhigh" selected>xhigh</option>' in html
    # Exactly one effort option is selected, and it is not the leading 'none'.
    assert '<option value="none" selected>' not in html
    assert sum(f'<option value="{lvl}" selected>' in html for lvl in EFFORT_LEVELS) == 1


def test_codex_usable_shows_green_ready_status(tmp_path):
    html = _render_section(tmp_path, _engine(engine="codex", usable=True))
    assert "bg-green-400" in html
    assert "Codex is ready" in html
    assert "bg-amber-400" not in html
    assert "Reviews fall back to Claude until this is fixed." not in html
    # Nothing to re-check when it already works.
    assert 'id="dcr-engine-recheck"' not in html


def test_codex_unusable_shows_reason_verbatim_and_fallback_sentence(tmp_path):
    """Amber dot, the server's reason verbatim, then the muted fallback line —
    otherwise a broken Codex reads as a silently broken /dcr."""
    reason = "Codex CLI is not signed in. Run: codex login"
    html = _render_section(
        tmp_path,
        _engine(engine="codex", usable=False, reason=reason, codex_logged_in=False),
    )
    assert "bg-amber-400" in html
    assert reason in html
    assert "Reviews fall back to Claude until this is fixed." in html
    assert "Codex is ready" not in html


def test_unusable_codex_offers_an_explicit_recheck_link(tmp_path):
    """Readiness is live external state: the user fixes it in a terminal
    (`codex login`) and needs a way to re-ask without reloading the dashboard.
    The link sits right after the reason it contradicts."""
    reason = "Codex CLI is not signed in. Run: codex login"
    html = _render_section(
        tmp_path, _engine(engine="codex", usable=False, reason=reason)
    )
    assert 'id="dcr-engine-recheck"' in html
    assert ">Check again</a>" in html
    assert html.index(reason) < html.index('id="dcr-engine-recheck"')


def test_reason_is_escaped(tmp_path):
    """`reason` is server-provided text dropped into the card, so it goes through
    escapeHtml: no live markup survives."""
    html = _render_section(
        tmp_path,
        _engine(
            engine="codex",
            usable=False,
            reason='<script>alert(1)</script> & "quoted"',
        ),
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&amp;" in html


def test_model_value_is_escaped(tmp_path):
    """The model string is server-provided too and lands in an attribute value,
    so a quote must not break out of it."""
    html = _render_section(tmp_path, _engine(engine="codex", model='evil" onx="1'))
    assert 'value="evil" onx="1"' not in html
    assert "&quot;" in html


def test_keep_on_claude_note_names_the_servers_actual_lenses(tmp_path):
    """The note is rendered FROM the payload, not hardcoded. A hardcoded sentence
    would keep promising that Security is protected after the CLI changed the
    list."""
    for engine in ("claude", "codex"):
        html = _render_section(tmp_path, _engine(engine=engine))
        assert (
            "Security and Frontend Design reviews always stay on Claude for the "
            "highest quality judgment." in html
        ), engine

    html = _render_section(
        tmp_path, _engine(engine="codex", keep_on_claude=("Security",))
    )
    assert (
        "Security reviews always stay on Claude for the highest quality judgment."
        in html
    )
    assert "Frontend Design" not in html


def test_keep_on_claude_names_are_escaped(tmp_path):
    """Lens names come from a hand-editable settings file, so they are untrusted
    text in the DOM."""
    html = _render_section(
        tmp_path, _engine(engine="codex", keep_on_claude=("<img src=x>",))
    )
    assert "<img src=x>" not in html
    assert "&lt;img src=x&gt;" in html


def test_empty_keep_on_claude_warns_instead_of_lying(tmp_path):
    """With the carve-out list emptied and Codex selected, EVERY lens including
    Security runs on Codex. The card says so, in amber, with the exact command
    that restores the default."""
    html = _render_section(
        tmp_path, _engine(engine="codex", keep_on_claude=())
    )
    assert "always stay on Claude" not in html
    assert (
        "The keep-on-Claude list is empty: every review lens, including Security, "
        "runs on Codex." in html
    )
    assert (
        'Restore it with: jacked dcr engine set codex --keep-on-claude '
        '"Security,Frontend Design"' in html
    )
    assert 'id="dcr-engine-carveout" class="text-xs text-amber-400"' in html


def test_empty_keep_on_claude_is_silent_on_claude_engine(tmp_path):
    """Nothing is at risk when every review already runs on Claude, so the
    warning would be noise."""
    html = _render_section(tmp_path, _engine(engine="claude", keep_on_claude=()))
    assert "The keep-on-Claude list is empty" not in html
    assert "always stay on Claude" not in html


def test_saving_disables_every_control_and_shows_progress(tmp_path):
    """Mid-PUT the controls are inert. The flag is module-level, so a re-render
    landing during the save cannot resurrect live-looking controls. The PUT
    re-runs the Codex preflight and can take seconds, so inert controls need a
    progress line or the card reads as broken."""
    html = _render_section(tmp_path, _engine(engine="codex"), saving=True)
    assert html.count(" disabled>") == 3  # engine select, model input, effort select
    assert 'aria-label="Review engine" disabled>' in html
    assert 'aria-label="Codex model" disabled>' in html
    assert 'aria-label="Codex effort" disabled>' in html
    assert 'id="dcr-engine-saving"' in html
    assert ">Saving...</div>" in html


def test_not_saving_has_no_progress_line(tmp_path):
    html = _render_section(tmp_path, _engine(engine="codex"))
    assert 'id="dcr-engine-saving"' not in html
    assert "Saving..." not in html


def test_failed_save_renders_inline_red_error_with_retry(tmp_path):
    html = _render_section(
        tmp_path, _engine(engine="codex"), save_error="Request timed out"
    )
    assert "text-red-400" in html
    assert "Request timed out" in html
    assert 'id="dcr-engine-retry"' in html
    assert ">Retry</a>" in html


def test_validation_failure_renders_the_error_without_retry(tmp_path):
    """Retry re-fires the IDENTICAL body. After a 4xx rejection of that body it
    can only fail identically, so offering it is a trap: the message already
    names the bad value and the user's next edit re-fires the change handler."""
    html = _render_section(
        tmp_path,
        _engine(engine="codex"),
        save_error="Invalid effort 'turbo'. Valid values: none, minimal, low",
        retryable=False,
    )
    assert "text-red-400" in html
    assert "Invalid effort 'turbo'" in html
    assert 'id="dcr-engine-retry"' not in html
    assert ">Retry</a>" not in html


def test_no_ai_slop_design_tells(tmp_path):
    """Flat dark-slate only: no gradients, no glows, no colored edge stripes, no
    emoji — the card must not look like it was decorated by a bot."""
    html = _render_section(tmp_path, _engine(engine="codex", usable=False, reason="x"))
    assert "gradient" not in html
    assert "shadow" not in html
    assert "backdrop-blur" not in html
    assert "border-l-" not in html and "border-t-" not in html
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", html)
    # Row chrome matches the neighboring hook/knowledge rows exactly.
    assert 'bg-slate-900/50 rounded border border-slate-700/50' in html


# --- Load failure isolation --------------------------------------------------


def test_load_error_section_has_its_own_retry(tmp_path):
    html = _render_error(tmp_path, "Request timed out")
    assert "Review Engine" in html
    assert "Failed to load review engine: Request timed out" in html
    assert "renderSettingsTab('features')" in html
    assert ">Retry</button>" in html


def test_dcr_fetch_failure_does_not_blank_hooks_or_knowledge(tmp_path):
    """A dead /api/dcr-engine must cost the user the card, not the whole tab.
    Mirrors how the packs section isolates its own failure."""
    api_stub = """
global.api = {
  get: async (p) => {
    if (p === '/api/features') {
      return {
        hooks: [{name: 'sessionstart', display_name: 'Session Start', description: 'd', installed: true, source_available: true}],
        knowledge: [{name: 'rules', display_name: 'Rules', description: 'd', installed: true, source_available: true}],
      };
    }
    if (p === '/api/packs') return { npx_available: true, packs: [] };
    if (p === '/api/dcr-engine') throw new Error('connection refused');
    throw new Error('unexpected path ' + p);
  },
};
"""
    driver = """
const container = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
renderFeaturesTab(container).then(() => {
  process.stdout.write('\\n<<<OUT_START>>>\\n');
  process.stdout.write(container.innerHTML);
  process.stdout.write('\\n<<<OUT_END>>>\\n');
});
"""
    html = _capture(_run_node(tmp_path, _program(driver, api_stub=api_stub)))
    # Sibling sections survived.
    assert "Session Start" in html
    assert ">Hooks</h3>" in html
    assert ">Knowledge</h3>" in html
    assert "Failed to load features" not in html
    # The card reports its own failure, with its own retry.
    assert "Failed to load review engine: connection refused" in html
    assert "renderSettingsTab('features')" in html


# --- Save path ---------------------------------------------------------------


def _drive_save(tmp_path, payload, put_result=None, put_error=None, put_status=0):
    """Call ``_saveDcrEngine(payload)`` against a stubbed api and report the
    request it issued plus the resulting module state.

    ``put_status`` mirrors ``ApiError.status`` from ``app.js`` (0 for a timeout or
    network failure), which is how the save path tells a validation rejection
    apart from a transient one.
    """
    if put_error is not None:
        put_impl = (
            f"const __e = new Error({json.dumps(put_error)});"
            f" __e.name = 'ApiError'; __e.status = {int(put_status)}; throw __e;"
        )
    else:
        put_impl = f"return {json.dumps(put_result or _engine(engine='codex'))};"
    api_stub = f"""
global.__put = [];
global.api = {{
  put: async (path, body) => {{
    global.__put.push({{ path, body }});
    {put_impl}
  }},
}};
"""
    driver = f"""
_saveDcrEngine({json.dumps(payload)}).then(() => {{
  const state = {{
    puts: global.__put,
    cached: window.jackedState.dcrEngine || null,
    error: _dcrEngineSaveError,
    retryable: _dcrEngineSaveRetryable,
    saving: _dcrEngineSaving,
    lastPayload: _dcrEngineLastPayload,
  }};
  process.stdout.write('\\n<<<OUT_START>>>\\n');
  process.stdout.write(JSON.stringify(state));
  process.stdout.write('\\n<<<OUT_END>>>\\n');
}});
"""
    return json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))


def test_save_issues_put_to_the_contract_endpoint(tmp_path):
    """PUT /api/dcr-engine with the {engine, model?, effort?} body, and the
    response becomes the cached state the card re-renders from."""
    fresh = _engine(engine="codex", model="gpt-5.6-terra", effort="high")
    state = _drive_save(
        tmp_path, {"engine": "codex", "model": "gpt-5.6-terra"}, put_result=fresh
    )
    assert len(state["puts"]) == 1
    assert state["puts"][0]["path"] == "/api/dcr-engine"
    assert state["puts"][0]["body"] == {"engine": "codex", "model": "gpt-5.6-terra"}
    # Re-render reads the PUT response, not a locally patched guess.
    assert state["cached"] == fresh
    assert state["error"] is None
    assert state["saving"] is False


def test_transient_save_failure_keeps_error_and_payload_for_retry(tmp_path):
    """A timeout or 5xx leaves the message on screen and the exact failed body
    queued, so Retry re-fires that request rather than a generic refetch."""
    state = _drive_save(
        tmp_path,
        {"engine": "codex", "effort": "max"},
        put_error="Request timed out",
        put_status=0,
    )
    assert state["error"] == "Request timed out"
    assert state["retryable"] is True
    assert state["lastPayload"] == {"engine": "codex", "effort": "max"}
    assert state["saving"] is False
    # A failed write must not poison the cache with a value the server rejected.
    assert state["cached"] is None

    server_error = _drive_save(
        tmp_path, {"engine": "codex"}, put_error="Internal Server Error", put_status=500
    )
    assert server_error["retryable"] is True


def test_validation_save_failure_is_not_retryable(tmp_path):
    """A 4xx means the server judged THIS body invalid. Re-sending it unchanged
    can only fail the same way, so the card must not offer that button."""
    for status in (400, 422):
        state = _drive_save(
            tmp_path,
            {"engine": "codex", "effort": "turbo"},
            put_error="Invalid effort 'turbo'.",
            put_status=status,
        )
        assert state["error"] == "Invalid effort 'turbo'.", status
        assert state["retryable"] is False, status
        assert state["cached"] is None, status


def test_save_start_disables_controls_in_place_without_rerendering(tmp_path):
    """The model input's `change` fires on BLUR. A user who edits the model and
    then clicks the effort select triggers the save while their pointer is down
    on that select: re-rendering the card there destroys the element mid-gesture
    and the click is swallowed. So the save start mutates the live nodes instead,
    and the full re-render waits until the PUT settles."""
    api_stub = """
global.__put = [];
global.api = {
  put: async (path, body) => {
    global.__put.push({ path, body });
    if (global.__probe) global.__probe();
    return { engine: 'codex', model: 'gpt-5.6-terra', effort: 'xhigh', keep_on_claude: ['Security'], usable: true };
  },
};
"""
    # A minimal live card: only what _markDcrEngineSaving and
    # _rerenderDcrEngineCard actually touch.
    driver = """
const inserted = [];
let rerenders = 0;
const select = { id: 'dcr-engine-select', disabled: false };
const input = { id: 'dcr-engine-model', disabled: false };
const carveout = { id: 'dcr-engine-carveout' };
carveout.parentNode = { insertBefore: (node, ref) => { inserted.push({ node, before: ref && ref.id }); } };
const card = {
  id: 'dcr-engine-section',
  set outerHTML(v) { rerenders += 1; },
  querySelectorAll: (sel) => (sel === 'select, input' ? [select, input] : []),
  querySelector: (sel) => {
    if (sel === '#dcr-engine-carveout') return carveout;
    if (sel === '#dcr-engine-saving') return inserted.length ? inserted[0].node : null;
    return null;
  },
  appendChild: (node) => { inserted.push({ node, before: null }); },
};
global.document = {
  createElement: () => {
    let _t = '';
    return {
      id: '', className: '',
      set textContent(v) { _t = (v == null) ? '' : String(v); },
      get textContent() { return _t; },
      get innerHTML() { return _t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    };
  },
  getElementById: (id) => (id === 'dcr-engine-section' ? card : null),
  querySelectorAll: () => [],
  querySelector: () => null,
};
let midSave = null;
global.__probe = () => {
  midSave = {
    rerenders: rerenders,
    selectDisabled: select.disabled,
    inputDisabled: input.disabled,
    inserted: inserted.map(i => ({ id: i.node.id, className: i.node.className, text: i.node.textContent, before: i.before })),
  };
};
_saveDcrEngine({ engine: 'codex', model: 'gpt-5.6-terra' }).then(() => {
  process.stdout.write('\\n<<<OUT_START>>>\\n');
  process.stdout.write(JSON.stringify({ midSave: midSave, finalRerenders: rerenders }));
  process.stdout.write('\\n<<<OUT_END>>>\\n');
});
"""
    state = json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))
    mid = state["midSave"]
    # No rebuild happened before the request went out.
    assert mid["rerenders"] == 0
    # The live controls went inert where they stand.
    assert mid["selectDisabled"] is True
    assert mid["inputDisabled"] is True
    # The progress line was inserted into the same slot the renderer uses.
    assert len(mid["inserted"]) == 1
    assert mid["inserted"][0]["id"] == "dcr-engine-saving"
    assert mid["inserted"][0]["text"] == "Saving..."
    assert mid["inserted"][0]["className"] == "text-xs text-slate-400"
    assert mid["inserted"][0]["before"] == "dcr-engine-carveout"
    # The rebuild-from-state happens exactly once, after the PUT settles.
    assert state["finalRerenders"] == 1


def test_save_start_marks_in_place_rather_than_rerendering():
    """Structural pin so the race cannot be reintroduced: between setting the
    saving flag and the try block, the save path marks the card in place and
    never calls the full re-render."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    body = src[src.index("async function _saveDcrEngine(payload)"):]
    prelude = body[: body.index("    try {")]
    assert "_markDcrEngineSaving();" in prelude
    assert "_rerenderDcrEngineCard();" not in prelude
    # The rebuild still happens, once, when the request settles.
    settled = body[body.index("    } finally {"):]
    assert "_rerenderDcrEngineCard();" in settled


def test_concurrent_save_is_blocked(tmp_path):
    """A second save (a stale Retry link, a fast double-change) while the first
    PUT is pending must not fire a second concurrent write."""
    api_stub = """
global.__put = [];
global.api = {
  put: (path, body) => { global.__put.push({ path, body }); return new Promise(() => {}); },
};
"""
    driver = """
_saveDcrEngine({ engine: 'codex' });
_saveDcrEngine({ engine: 'claude' });
process.stdout.write('\\n<<<OUT_START>>>\\n');
process.stdout.write(JSON.stringify({ puts: global.__put }));
process.stdout.write('\\n<<<OUT_END>>>\\n');
"""
    state = json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))
    assert len(state["puts"]) == 1
    assert state["puts"][0]["body"] == {"engine": "codex"}


# --- Re-check affordance -----------------------------------------------------


def _drive_recheck(tmp_path, seed=""):
    """Bind the card's events against a fake container, fire the Check-again
    click, and report the requests it issued plus the resulting state."""
    api_stub = """
global.__gets = [];
global.api = {
  get: async (path) => {
    global.__gets.push(path);
    return { engine: 'codex', usable: true, keep_on_claude: ['Security'], effort: 'xhigh' };
  },
};
"""
    driver = (
        seed
        + """
const handlers = {};
const recheck = {
  id: 'dcr-engine-recheck',
  addEventListener: (ev, fn) => { handlers[ev] = fn; },
};
const container = { querySelector: (sel) => (sel === '#dcr-engine-recheck' ? recheck : null) };
_bindDcrEngineEvents(container);
let prevented = false;
Promise.resolve(handlers.click ? handlers.click({ preventDefault: () => { prevented = true; } }) : null)
  .then(() => {
    process.stdout.write('\\n<<<OUT_START>>>\\n');
    process.stdout.write(JSON.stringify({
      bound: !!handlers.click,
      prevented: prevented,
      gets: global.__gets,
      cached: window.jackedState.dcrEngine || null,
    }));
    process.stdout.write('\\n<<<OUT_END>>>\\n');
  });
"""
    )
    return json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))


def test_recheck_link_refetches_and_updates_state(tmp_path):
    """Click re-asks the server, publishes the answer, and redraws the card in
    place. No full tab reload, and no navigation from the href="#"."""
    state = _drive_recheck(tmp_path)
    assert state["bound"] is True
    assert state["prevented"] is True
    assert state["gets"] == ["/api/dcr-engine"]
    assert state["cached"]["usable"] is True


def test_recheck_is_a_noop_while_a_save_is_in_flight(tmp_path):
    """A running PUT owns the card. Racing a GET behind it would paint state the
    save is about to replace."""
    state = _drive_recheck(tmp_path, seed="_dcrEngineSaving = true;\n")
    assert state["bound"] is True
    assert state["gets"] == []
    assert state["cached"] is None


def test_recheck_renders_in_place_rather_than_reloading_the_tab():
    """Structural pin: the handler uses the scoped card swap, not the whole-tab
    render that would blow away hooks, knowledge, and packs."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    handler = src[src.index("#dcr-engine-recheck'"):]
    handler = handler[: handler.index("#dcr-engine-retry'")]
    assert "api.get(DCR_ENGINE_URL)" in handler
    assert "window.jackedState.dcrEngine =" in handler
    assert "_rerenderDcrEngineCard();" in handler
    assert "renderSettingsTab" not in handler


# --- Stale state across tab navigations --------------------------------------


def test_features_tab_entry_clears_a_stale_save_failure(tmp_path):
    """_dcrEngineSaveError/_dcrEngineLastPayload are module-level, so without an
    explicit reset a rejected PUT would resurface, error and all, every later
    time the user opens the Features tab."""
    api_stub = """
global.api = {
  get: async (p) => {
    if (p === '/api/features') return { hooks: [], knowledge: [] };
    if (p === '/api/packs') return { npx_available: true, packs: [] };
    if (p === '/api/dcr-engine') {
      return { engine: 'codex', model: 'gpt-5.6-luna', effort: 'xhigh',
               keep_on_claude: ['Security'], usable: true, codex_path: '/bin/codex' };
    }
    throw new Error('unexpected path ' + p);
  },
};
"""
    driver = """
_dcrEngineSaveError = 'stale failure from a previous visit';
_dcrEngineSaveRetryable = false;
_dcrEngineLastPayload = { engine: 'codex', effort: 'turbo' };
const container = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
renderFeaturesTab(container).then(() => {
  process.stdout.write('\\n<<<OUT_START>>>\\n');
  process.stdout.write(JSON.stringify({
    html: container.innerHTML,
    error: _dcrEngineSaveError,
    retryable: _dcrEngineSaveRetryable,
    lastPayload: _dcrEngineLastPayload,
  }));
  process.stdout.write('\\n<<<OUT_END>>>\\n');
});
"""
    state = json.loads(_capture(_run_node(tmp_path, _program(driver, api_stub=api_stub))))
    assert state["error"] is None
    assert state["retryable"] is True
    assert state["lastPayload"] is None
    assert "stale failure from a previous visit" not in state["html"]
    assert 'id="dcr-engine-retry"' not in state["html"]
    # The card itself still rendered.
    assert 'id="dcr-engine-section"' in state["html"]


def test_features_tab_entry_leaves_an_in_flight_save_alone():
    """The reset is guarded: a save genuinely still running keeps its state, so a
    re-render landing mid-PUT does not resurrect live-looking controls."""
    src = SETTINGS_JS.read_text(encoding="utf-8")
    guard = src.index("if (!_dcrEngineSaving) {")
    block = src[guard: src.index("let dcrEngineData = null;", guard)]
    assert "_dcrEngineSaveError = null;" in block
    assert "_dcrEngineSaveRetryable = true;" in block
    assert "_dcrEngineLastPayload = null;" in block
    # And the reset runs before the fetch that feeds the render.
    assert guard < src.index("dcrEngineData = await loadDcrEngine();")
