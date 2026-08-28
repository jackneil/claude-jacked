"""Tests for the Features-tab Skills/Knowledge split and the shared feature-row
filter in ``settings.js``.

Plain browser JS, no bundler, so this drives ``node`` from pytest like the
sibling ``test_web_js_*`` suites. It differs from them in one way worth copying:
instead of ``eval``-ing the component source, it concatenates the browser
globals stub, ``utils.js``, ``settings.js`` and a driver into a single throwaway
``.js`` file and runs ``node`` on that. The older suites reach for ``eval`` so a
driver can share scope with module-level ``const`` bindings; one file gives them
that same shared file scope with no dynamic evaluation at all.

What is covered behaviorally: ``_isSkillFeature`` (the one rule deciding which
half of the ``knowledge`` category a row belongs to) and the markup contract of
``_renderFeatureFilter``.

What is NOT, and why: ``_bindFeatureFilter`` attaches an ``input`` listener, and
the shared DOM stub makes element ``addEventListener`` a no-op, so the handler
can never be dispatched. Extending that stub would perturb every suite that
imports it. The wiring is pinned with source-level assertions instead, the same
compromise as the ``visibilitychange`` guard in
``test_web_js_remote_access_ui.py``.

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

# Browser globals settings.js/utils.js close over. document.createElement
# mirrors a real browser's textContent -> innerHTML so the REAL escapeHtml runs
# (escape &, < and >; escapeHtml adds the " -> &quot; pass itself). Only the DOM
# node primitive is faked.
_STUB_JS = """
global.window = { jackedState: {} };
global.document = {
  createElement: () => {
    let _t = '';
    return {
      set textContent(v) { _t = (v == null) ? '' : String(v); },
      get innerHTML() {
        return _t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
  getElementById: () => null,
  querySelectorAll: () => [],
  querySelector: () => null,
};
global.localStorage = { getItem: () => null, setItem: () => {} };
"""


def _eval_in_settings(tmp_path, expression_js):
    """Run ``expression_js`` with utils.js + settings.js loaded, and return its
    JSON-round-tripped value.

    Only this repo's own first-party JS is loaded, never external or user input,
    and every injected value goes through ``json.dumps`` so it arrives as data.
    """
    driver = (
        f"const __value = ({expression_js});\n"
        "process.stdout.write('<<<J>>>' + JSON.stringify(__value) + '<<<J>>>');\n"
    )
    program = "\n".join([
        _STUB_JS,
        UTILS_JS.read_text(encoding="utf-8"),
        SETTINGS_JS.read_text(encoding="utf-8"),
        driver,
    ])
    script = tmp_path / "features_split_harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}"
    out = proc.stdout
    start = out.index("<<<J>>>") + len("<<<J>>>")
    end = out.index("<<<J>>>", start)
    return json.loads(out[start:end])


# ---------------------------------------------------------------------------
# _isSkillFeature: the split rule
# ---------------------------------------------------------------------------


def test_is_skill_feature_classifies_the_knowledge_grab_bag(tmp_path):
    """Only `skill_`-prefixed entries are skills. `rules` and `reference` are
    genuine knowledge documents and must stay in the Knowledge section."""
    cases = [
        {"name": "skill_dcr"},
        {"name": "skill_qa"},
        {"name": "rules"},
        {"name": "reference"},
    ]
    got = _eval_in_settings(tmp_path, f"({json.dumps(cases)}).map(_isSkillFeature)")
    assert got == [True, True, False, False]


def test_is_skill_feature_is_defensive_about_shape(tmp_path):
    """This runs over whatever the API returned. A malformed entry must classify
    as a document, not throw and blank the entire tab."""
    cases = [None, {}, {"name": None}, {"name": 123}, {"name": ""}]
    got = _eval_in_settings(tmp_path, f"({json.dumps(cases)}).map(_isSkillFeature)")
    assert got == [False, False, False, False, False]


def test_split_partitions_a_realistic_knowledge_payload(tmp_path):
    """The halves must be disjoint and lose nothing: every row the API sends
    still renders somewhere. A silent drop is the failure this catches."""
    knowledge = [
        {"name": "rules"},
        {"name": "skill_dcr"},
        {"name": "skill_whats-next"},
        {"name": "reference"},
    ]
    got = _eval_in_settings(
        tmp_path,
        "(() => { const k = "
        + json.dumps(knowledge)
        + "; return {"
        " skills: k.filter(_isSkillFeature).map(x => x.name),"
        " docs: k.filter(x => !_isSkillFeature(x)).map(x => x.name) }; })()",
    )
    assert got["skills"] == ["skill_dcr", "skill_whats-next"]
    assert got["docs"] == ["rules", "reference"]
    assert sorted(got["skills"] + got["docs"]) == sorted(k["name"] for k in knowledge)


# ---------------------------------------------------------------------------
# _renderFeatureFilter: markup contract
# ---------------------------------------------------------------------------


def test_filter_markup_carries_the_hooks_bind_relies_on(tmp_path):
    """_bindFeatureFilter finds its input and empty-state node by these two
    attributes. Drop either and the filter silently does nothing."""
    html = _eval_in_settings(tmp_path, "_renderFeatureFilter('Filter agents...')")
    assert "data-feature-filter" in html
    assert "data-feature-filter-empty" in html
    assert 'placeholder="Filter agents..."' in html
    # The empty state starts hidden; an unfiltered tab must not say "No matches."
    assert "hidden" in html


def test_filter_placeholder_is_escaped(tmp_path):
    """The placeholder lands in an attribute value. It is a literal today, but
    escaping is the contract, not the caller's discipline."""
    html = _eval_in_settings(
        tmp_path, "_renderFeatureFilter('\\\"><script>alert(1)</script>')"
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Source-level wiring guards (see module docstring for why not behavioral)
# ---------------------------------------------------------------------------


def _settings_code():
    """settings.js with line comments stripped.

    The comments explaining this feature name the very identifiers asserted on
    below, so matching raw source would pass on prose alone.
    """
    src = SETTINGS_JS.read_text(encoding="utf-8")
    return "\n".join(ln for ln in src.split("\n") if not ln.strip().startswith("//"))


def test_every_row_bearing_tab_binds_the_filter():
    """Agents, Commands and Features each render a filter, so each must bind one.
    A tab that renders the input without binding it looks functional and is
    inert, which is worse than not having it at all.
    """
    code = _settings_code()
    # Match call sites only: the bare identifier also hits each function's own
    # declaration, which is how the first version of this guard miscounted.
    assert code.count("${_renderFeatureFilter(") == 3, (
        "expected exactly 3 tabs to render a feature filter (agents, commands, "
        "features); update this guard deliberately if a tab was added or removed"
    )
    assert code.count("_bindFeatureFilter(container);") == 3, (
        "a tab renders a filter input it never binds; it would sit there inert"
    )


def test_features_tab_renders_a_dedicated_skills_section():
    """Regression: the 28 bundled skills used to render inside Knowledge, under
    a description about 'documents and rules' that did not describe them."""
    code = _settings_code()
    assert "skillRows" in code, "Features tab lost its separate skills row set"
    assert ">Skills</h3>" in code, "Features tab lost its dedicated Skills heading"


def test_skill_rows_still_toggle_through_the_knowledge_category():
    """The split is presentation only. PUT /api/features/{category}/{name} takes
    Literal['agents','commands','hooks','knowledge'] and has no 'skills' member,
    so a skill row toggling as its own category would 422 on every click.
    """
    code = _settings_code()
    assert "renderToggle(k.name, 'knowledge'" in code, (
        "skill and knowledge rows must both toggle with category 'knowledge'"
    )


def test_collapsible_sections_are_marked_for_the_filter():
    """_bindFeatureFilter collapses a section whose rows are all filtered out.
    That only works on sections carrying the attribute."""
    code = _settings_code()
    assert code.count("data-feature-section") >= 3, (
        "Hooks, Knowledge and Skills each need data-feature-section or their "
        "headings hang over empty space when filtered"
    )
