"""CLI tests for `jacked skills listing`.

``JACKED_HOME`` redirects the CLI to a throwaway home under ``tmp_path``, so
the real ``~/.claude`` is never read or written.
"""
import json

import pytest
from click.testing import CliRunner

from jacked import skill_listing as sl
from jacked.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    monkeypatch.delenv(sl.ENV_BUDGET_VAR, raising=False)
    return tmp_path


def _skill(home, name, description):
    d = home / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\nBody\n",
        encoding="utf-8",
    )


def _over_budget(home):
    for i in range(80):
        _skill(home, f"skill-{i:03d}", "d" * 600)


def _settings(home):
    path = home / ".claude" / "settings.json"
    return json.loads(path.read_text()) if path.exists() else None


def _run(*args):
    return CliRunner().invoke(main, ["skills", "listing", *args])


def test_json_shape(home):
    _over_budget(home)
    result = _run("--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["context_window"] == 1_000_000
    assert body["dropped_count"] > 0
    assert set(body["windows"]) == {"200000", "1000000"}
    assert body["recommendation"]["summary"].startswith("Cap descriptions at")
    assert isinstance(body["dropped_groups"], list)


def test_window_option(home):
    _skill(home, "a", "Short.")
    assert json.loads(_run("--json", "--window", "200k").output)["context_window"] == 200_000
    assert json.loads(_run("--json", "--window", "1m").output)["context_window"] == 1_000_000
    assert _run("--window", "5k").exit_code != 0


def test_human_output(home):
    _over_budget(home)
    (home / ".claude" / "skills" / "broken").mkdir(parents=True)
    (home / ".claude" / "skills" / "broken" / "SKILL.md").write_text(
        "---\nname: broken\ndescription: one shot: two\n---\n", encoding="utf-8")
    result = _run()
    assert result.exit_code == 0, result.output
    assert "Skill listing:" in result.output
    assert "without a description" in result.output
    assert "broken" in result.output and "strict YAML" in result.output
    assert "jacked skills listing --apply" in result.output
    assert _settings(home) is None


def test_apply_writes_recommendation(home):
    _over_budget(home)
    rec = json.loads(_run("--json").output)["recommendation"]
    result = _run("--apply")
    assert result.exit_code == 0, result.output
    assert _settings(home) == {
        "skillListingMaxDescChars": rec["max_desc_chars"],
        "skillListingBudgetFraction": rec["budget_fraction"],
    }
    assert "Saved." in result.output


def test_apply_json_reports_applied(home):
    _over_budget(home)
    body = json.loads(_run("--apply", "--json").output)
    assert body["applied"]["skillListingMaxDescChars"] >= 150
    assert body["fits"] is True


def test_apply_when_it_fits_changes_nothing(home):
    _skill(home, "a", "Short.")
    result = _run("--apply")
    assert result.exit_code == 0
    assert "Nothing changed" in result.output
    assert _settings(home) is None


def test_reset(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"skillListingMaxDescChars": 250,
                                "skillListingBudgetFraction": 0.02, "model": "opus"}))
    result = _run("--reset")
    assert result.exit_code == 0, result.output
    assert _settings(home) == {"model": "opus"}


def test_apply_and_reset_together_is_an_error(home):
    result = _run("--apply", "--reset")
    assert result.exit_code != 0
    assert _settings(home) is None


def test_unreadable_settings_refused(home):
    _over_budget(home)
    path = home / ".claude" / "settings.json"
    path.write_text("{corrupt", encoding="utf-8")
    for flag in ("--apply", "--reset"):
        result = _run(flag)
        assert result.exit_code == 1
        assert "Nothing was written" in result.output
    assert path.read_text() == "{corrupt"


def test_window_defaults_to_the_configured_model(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": "claude-sonnet-4-6"}))
    body = json.loads(_run("--json").output)
    assert body["context_window"] == 200_000
    result = _run()
    assert "claude-sonnet-4-6" in result.output and "200,000-token window" in result.output
