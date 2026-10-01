"""Tests for the Skill listing panel renderer in ``settings.js``.

Plain browser JS, no bundler: like ``test_web_js_packs_render.py`` this evals
``utils.js`` + ``settings.js`` under node with a minimal DOM stub (the REAL
``escapeHtml`` runs; only the DOM node primitive is faked) and asserts on the
HTML that ``_renderSkillListingPanel`` returns, plus the pure toast helper
``_skillListingPackWarning``.

eval() here only executes this repo's own first-party JS in a throwaway node
process; every injected value goes through json.dumps, so it is data.

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

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _eval(tmp_path, expr: str, prelude: str = "") -> str:
    driver = (
        prelude
        + f"Promise.resolve({expr}).then((__out) => {{\n"
        + "process.stdout.write('\\n<<<OUT_START>>>\\n');\n"
        + "process.stdout.write(typeof __out === 'string' ? __out : JSON.stringify(__out));\n"
        + "process.stdout.write('\\n<<<OUT_END>>>\\n');\n"
        + "});\n"
    )
    program = f"""
const fs = require('fs');
global.window = {{ jackedState: {{}} }};
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
global.showToast = () => {{}};
const UTILS = fs.readFileSync({json.dumps(str(UTILS_JS))}, 'utf8');
const SETTINGS = fs.readFileSync({json.dumps(str(SETTINGS_JS))}, 'utf8');
const DRIVER = {json.dumps(driver)};
eval(UTILS + '\\n' + SETTINGS + '\\n' + DRIVER);
"""
    script = tmp_path / "skill_listing_harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    out = proc.stdout
    start = out.index("<<<OUT_START>>>") + len("<<<OUT_START>>>")
    return out[start:out.index("<<<OUT_END>>>")].strip("\n")


def _render(tmp_path, data, prelude="") -> str:
    return _eval(tmp_path, f"_renderSkillListingPanel({json.dumps(data)})", prelude)


def _report(**over):
    base = {
        "summary": "Skill listing: 112k of 40k characters. 170 skills show without a description, so Claude rarely picks them.",
        "fits": False,
        "full_chars": 112000,
        "budget_chars": 40000,
        "dropped_count": 3,
        "dropped_groups": [
            {"key": "pack:marketing", "label": "Marketing Skills (skill pack)", "source": "user",
             "names": ["ads", "seo"]},
            {"key": "command", "label": "Your commands", "source": "command", "names": ["retro"]},
        ],
        "recommendation": {
            "max_desc_chars": 250, "budget_fraction": 0.02, "fits": True, "extra_tokens": 8000,
            "summary": "Cap descriptions at 250 characters and use a 2% budget (about 8k more tokens per session).",
        },
        "strict_yaml_warnings": [],
        "settings": {"max_desc_chars": 1536, "max_desc_chars_set": False,
                     "budget_fraction": 0.01, "budget_fraction_set": False,
                     "env_budget": None, "settings_unreadable": False},
        "windows": {"200000": {"dropped_count": 200, "fits": False},
                    "1000000": {"dropped_count": 3, "fits": False}},
        "context_window": 1000000,
        "window_source": "model",
        "window_known": True,
        "summary_short": "With a 1M-token context window, 3 skills show without a description, so Claude rarely picks them (112k of 40k characters). With a 200k-token window, 200 skills show without a description.",
        "version_note": None,
        "not_counted": [],
    }
    base.update(over)
    return base


def test_over_budget_panel(tmp_path):
    html = _render(tmp_path, _report())
    assert 'id="skill-listing-panel"' in html
    assert "With a 1M-token context window, 3 skills show without a description" in html
    assert "Skill listing:" not in html  # the heading already says it
    assert "<details" in html
    assert "Marketing Skills (skill pack)" in html and "ads" in html and "retro" in html
    assert 'id="skill-listing-apply"' in html
    assert "Apply recommended" in html
    assert "Cap descriptions at 250 characters and use a 2% budget (about 8k more tokens per session)." in html
    assert 'id="skill-listing-reset"' not in html  # nothing set, nothing to reset


def test_reset_button_when_a_key_is_set(tmp_path):
    settings = dict(_report()["settings"], max_desc_chars_set=True, max_desc_chars=250)
    html = _render(tmp_path, _report(settings=settings))
    assert 'id="skill-listing-reset"' in html
    assert "Reset to defaults" in html


def test_fits_panel_has_no_apply(tmp_path):
    html = _render(tmp_path, _report(
        fits=True, dropped_count=0, dropped_groups=[], recommendation=None,
        summary="Skill listing: With a 1M-token context window, every skill shows its description (30k of 60k characters).",
        summary_short="With a 1M-token context window, every skill shows its description (30k of 60k characters).",
        windows={"200000": {"dropped_count": 0, "fits": True},
                 "1000000": {"dropped_count": 0, "fits": True}},
    ))
    assert "every skill shows its description" in html
    assert 'id="skill-listing-apply"' not in html
    assert "<details" not in html


def test_small_window_note(tmp_path):
    html = _render(tmp_path, _report())
    assert "With a 200k-token window, 200 skills show without a description." in html


def test_window_select_defaults_to_report_window(tmp_path):
    html = _render(tmp_path, _report())
    m = re.search(r'<select id="skill-listing-window"[^>]*>(.*?)</select>', html, re.S)
    assert m, html
    assert re.search(r'<option value="1000000"[^>]*selected', m.group(1))
    assert not re.search(r'<option value="200000"[^>]*selected', m.group(1))
    html = _render(tmp_path, _report(context_window=200000))
    m = re.search(r'<select id="skill-listing-window"[^>]*>(.*?)</select>', html, re.S)
    assert re.search(r'<option value="200000"[^>]*selected', m.group(1))


def test_window_source_is_explained(tmp_path):
    assert "from your model" in _render(tmp_path, _report())
    html = _render(tmp_path, _report(window_known=False, window_source="unknown"))
    assert "model is unknown" in html


def test_version_note_and_not_counted_and_override(tmp_path):
    settings = dict(_report()["settings"], listing_keys_overridden_by=["/etc/claude-code/managed-settings.json"])
    html = _render(tmp_path, _report(
        version_note="jacked calibrated this estimate for Claude Code 2.1.287. You run 2.2.0.",
        not_counted=[{"path": "~/.claude/commands/ns", "owner": "Your commands",
                      "reason": "2 command file(s) in a sub-folder"}],
        settings=settings,
    ))
    assert "You run 2.2.0" in html
    assert "~/.claude/commands/ns" in html and "not counted" in html.lower()
    assert "/etc/claude-code/managed-settings.json" in html


def test_stale_get_never_overwrites_a_newer_one(tmp_path):
    prelude = """
const __res = [];
global.api = { get: () => new Promise(r => __res.push(r)) };
async function __run() {
  const p1 = loadSkillListing();
  const p2 = loadSkillListing();
  __res[1]({ summary_short: 'new' }); await p2;
  __res[0]({ summary_short: 'old' }); await p1;
  return window.jackedState.skillListing.summary_short;
}
"""
    assert _eval(tmp_path, "__run()", prelude) == "new"


def test_stale_get_never_overwrites_a_put(tmp_path):
    prelude = """
const __res = [];
global.api = {
  get: () => new Promise(r => __res.push(r)),
  put: () => Promise.resolve({ summary_short: 'after-put', message: 'Saved.' }),
};
async function __run() {
  const pGet = loadSkillListing();
  await _saveSkillListing('recommended');
  __res[0]({ summary_short: 'stale-get' }); await pGet;
  return window.jackedState.skillListing.summary_short;
}
"""
    assert _eval(tmp_path, "__run()", prelude) == "after-put"


def test_put_sends_the_selected_window(tmp_path):
    prelude = """
let __body = null;
global.api = { put: (u, b) => { __body = b; return Promise.resolve({}); } };
async function __run() {
  _skillListingWindow = 200000;
  await _saveSkillListing('reset');
  return __body;
}
"""
    out = json.loads(_eval(tmp_path, "__run()", prelude))
    assert out == {"action": "reset", "context_window": 200000}


def test_hostile_names_and_paths_are_escaped(tmp_path):
    evil = '<img src=x onerror="alert(1)">'
    html = _render(tmp_path, _report(
        dropped_groups=[{"key": "user", "label": evil, "source": "user", "names": [evil]}],
        strict_yaml_warnings=[{"name": evil, "path": "/tmp/" + evil, "error": evil}],
    ))
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html


def test_strict_yaml_warnings_are_called_out(tmp_path):
    html = _render(tmp_path, _report(strict_yaml_warnings=[
        {"name": "onboard", "path": "/home/u/.claude/skills/onboard/SKILL.md",
         "error": "mapping values are not allowed here (line 3)"},
    ]))
    assert "not strict YAML" in html
    assert "onboard" in html and "/home/u/.claude/skills/onboard/SKILL.md" in html
    assert "Codex" in html


def test_unreadable_settings_disable_buttons(tmp_path):
    settings = dict(_report()["settings"], settings_unreadable=True, max_desc_chars_set=True)
    html = _render(tmp_path, _report(settings=settings))
    assert "unreadable" in html
    assert re.search(r'id="skill-listing-apply"[^>]*disabled', html)
    assert re.search(r'id="skill-listing-reset"[^>]*disabled', html)


def test_env_budget_note(tmp_path):
    settings = dict(_report()["settings"], env_budget=8000)
    html = _render(tmp_path, _report(settings=settings))
    assert "SLASH_COMMAND_TOOL_CHAR_BUDGET" in html


def test_saving_state_disables_buttons(tmp_path):
    settings = dict(_report()["settings"], max_desc_chars_set=True)
    html = _render(tmp_path, _report(settings=settings), prelude="_skillListingSaving = true;\n")
    assert re.search(r'id="skill-listing-apply"[^>]*disabled', html)
    assert "Saving..." in html


def test_copy_follows_the_design_rules(tmp_path):
    html = _render(tmp_path, _report(strict_yaml_warnings=[
        {"name": "x", "path": "/p", "error": "e"}]))
    assert "—" not in html  # no em-dash in user-facing copy
    assert not re.search(r"[\U0001F300-\U0001FAFF]", html)  # no emoji
    assert not re.search(r"border-[lt]-", html)  # no colored edge stripes
    assert "gradient" not in html
    heading = re.search(r"<h4[^>]*>(.*?)</h4>", html, re.S)
    assert heading and heading.group(1).strip() == "Skill listing"
    assert "uppercase" not in heading.group(0)


def test_error_panel(tmp_path):
    html = _eval(tmp_path, "_renderSkillListingError('boom <b>')")
    assert 'id="skill-listing-panel"' in html
    assert "boom &lt;b&gt;" in html


@pytest.mark.parametrize("before,after,expected", [
    ({"fits": True, "dropped_count": 0}, {"fits": False, "dropped_count": 28, "context_window": 1000000},
     "Marketing Skills pushed the skill listing over budget for a 1M-token context window. 28 skills now show without a description."),
    ({"fits": False, "dropped_count": 10}, {"fits": False, "dropped_count": 38, "context_window": 200000},
     "Marketing Skills pushed the skill listing over budget for a 200k-token context window. 38 skills now show without a description."),
    ({"fits": True, "dropped_count": 0}, {"fits": True, "dropped_count": 0}, None),
    ({"fits": False, "dropped_count": 38}, {"fits": False, "dropped_count": 10}, None),
    (None, {"fits": False, "dropped_count": 1, "context_window": 1000000},
     "Marketing Skills pushed the skill listing over budget for a 1M-token context window. 1 skill now shows without a description."),
])
def test_pack_warning_message(tmp_path, before, after, expected):
    out = _eval(tmp_path,
                f"_skillListingPackWarning({json.dumps(before)}, {json.dumps(after)}, 'Marketing Skills')")
    if expected is None:
        assert out in ("null", "")
    else:
        assert out.startswith(expected)
        assert "Apply recommended" in out


def test_failed_older_refresh_never_replaces_a_newer_panel(tmp_path):
    prelude = """
const __html = [];
const __el = { set outerHTML(v) { __html.push(v); }, querySelector: () => null };
global.document.getElementById = (id) => (id === 'skill-listing-panel' ? __el : null);
const __calls = [];
global.api = { get: () => new Promise((res, rej) => __calls.push({ res, rej })) };
async function __run() {
  const older = _refreshSkillListingPanel();
  const newer = _refreshSkillListingPanel();
  __calls[1].res({ summary_short: 'fresh report', settings: {}, fits: true });
  await newer;
  __calls[0].rej(new Error('older request failed'));
  await older;
  return __html[__html.length - 1];
}
"""
    out = _eval(tmp_path, "__run()", prelude)
    assert "fresh report" in out and "Could not check" not in out


def test_get_started_during_a_put_never_discards_the_put(tmp_path):
    prelude = """
let __putRes = null;
const __gets = [];
global.api = {
  get: () => new Promise(r => __gets.push(r)),
  put: () => new Promise(r => { __putRes = r; }),
};
async function __run() {
  const save = _saveSkillListing('recommended');
  const get = loadSkillListing();          // pack-toggle refresh races the Apply
  __putRes({ summary_short: 'after-put', message: 'Saved.' });
  await save;
  __gets[0]({ summary_short: 'stale-get' });
  await get;
  const after = window.jackedState.skillListing.summary_short;
  const later = loadSkillListing();        // a GET that starts after the PUT applies
  __gets[1]({ summary_short: 'later-get' });
  await later;
  return after + '|' + window.jackedState.skillListing.summary_short;
}
"""
    assert _eval(tmp_path, "__run()", prelude) == "after-put|later-get"
