"""Tests for the Advanced-tab raw settings table's key filter in
``settings.js``.

Plain browser JS, no bundler, so this drives ``node`` from pytest like the
other ``test_web_js_*`` suites: it evals the component source in a minimal stub
and asserts on ``settingsToEntries``. The remote-access keys are managed by the
dedicated card on the same tab and are protected server-side, so they must not
appear as editable rows in the raw table (editing there only 422s). Skipped when
node is not on PATH.
"""
import json
import shutil
import subprocess

import pytest

from tests.unit.test_web_js_swap_ui import WEB_JS

SETTINGS_JS = WEB_JS / "components" / "settings.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _run_settings_to_entries(tmp_path, settings_literal):
    # settings.js references browser globals at module scope only inside
    # functions, so a bare eval of the source makes settingsToEntries callable.
    # Stub the handful of globals it closes over so the eval itself is clean.
    # eval() here is safe and intentional: it executes ONLY this repo's own
    # first-party settings.js (never external or user input) inside a throwaway
    # node process, the same non-module-browser-JS loading pattern the other
    # test_web_js_* suites use (see test_web_js_swap_ui.py). settings_literal is
    # a json.dumps() of test-controlled data, so it is valid JSON, not code.
    program = f"""
const fs = require('fs');
global.window = {{ jackedState: {{}} }};
global.document = {{ getElementById: () => null, querySelectorAll: () => [], querySelector: () => null }};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
const SRC = fs.readFileSync({json.dumps(str(SETTINGS_JS))}, 'utf8');
eval(SRC);
const out = (o) => process.stdout.write('\\n' + JSON.stringify(o) + '\\n');
const entries = settingsToEntries({settings_literal});
out({{ keys: entries.map(e => e[0]), entries }});
"""
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_node_syntax_check():
    proc = subprocess.run(
        ["node", "--check", str(SETTINGS_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_remote_access_keys_hidden_from_raw_table_array_form(tmp_path):
    settings = json.dumps([
        {"key": "remote_access_enabled", "value": "true"},
        {"key": "remote_access_scope", "value": "tailscale"},
        {"key": "theme", "value": '"dark"'},
        {"key": "usage_check_interval", "value": "120"},
    ])
    result = _run_settings_to_entries(tmp_path, settings)
    assert "remote_access_enabled" not in result["keys"]
    assert "remote_access_scope" not in result["keys"]
    # Other settings still shown (the filter is surgical, not a blanket hide).
    assert "theme" in result["keys"]
    assert "usage_check_interval" in result["keys"]


def test_remote_access_keys_hidden_from_raw_table_object_form(tmp_path):
    settings = json.dumps({
        "remote_access_scope": "all",
        "remote_access_enabled": "true",
        "theme": '"dark"',
    })
    result = _run_settings_to_entries(tmp_path, settings)
    assert result["keys"] == ["theme"]


def test_non_managed_settings_unaffected(tmp_path):
    settings = json.dumps([
        {"key": "b_key", "value": "2"},
        {"key": "a_key", "value": "1"},
    ])
    result = _run_settings_to_entries(tmp_path, settings)
    # Still sorted, still all present.
    assert result["keys"] == ["a_key", "b_key"]
