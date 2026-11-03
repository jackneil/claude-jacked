"""The read-only hook and settings integrity panel in ``settings.js`` (#99i).

Drives ``node`` with the same single-file harness as
``test_web_js_features_split``. Skipped when node is not on PATH.
"""
import json
import shutil

import pytest

from tests.unit.test_web_js_features_split import SETTINGS_JS, _eval_in_settings

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _panel(tmp_path, integ):
    return _eval_in_settings(tmp_path, f"_integrityPanel({json.dumps(integ)})")


def test_no_state_renders_nothing(tmp_path):
    assert _panel(tmp_path, None) == ""


def test_ok_names_the_baseline_time(tmp_path):
    html = _panel(tmp_path, {"state": "ok", "baseline_time": "2026-11-03T12:00:00Z", "lines": []})

    assert "Hook and settings integrity: OK (baseline 2026-11-03T12:00:00Z)" in html
    assert "data-integrity-panel" in html


def test_drift_lists_each_line_escaped(tmp_path):
    line = "jacked integrity: settings.json changed outside jacked; Stop hook added: <img src=x onerror=alert(1)>"
    html = _panel(tmp_path, {"state": "drift", "baseline_time": "t", "lines": [line]})

    assert "1 change outside jacked" in html
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "jacked integrity accept" in html


def test_the_panel_has_no_control_that_can_accept(tmp_path):
    html = _panel(tmp_path, {"state": "drift", "baseline_time": "t", "lines": ["x"]})

    assert "<button" not in html and "onclick" not in html and "<input" not in html


def test_no_baseline_and_unknown_states(tmp_path):
    assert "no baseline yet" in _panel(tmp_path, {"state": "no_baseline", "lines": []})
    assert "not checked" in _panel(tmp_path, {"state": "error", "lines": []})


def test_the_hooks_section_renders_the_panel():
    source = SETTINGS_JS.read_text(encoding="utf-8")

    assert "${_integrityPanel(features.integrity)}" in source
