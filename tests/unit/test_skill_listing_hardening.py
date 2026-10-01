"""Regression tests for the skill-listing review findings (security + logic).

Every test builds a throwaway home under ``tmp_path``. The managed-settings
paths and the ``claude --version`` probe are patched for the whole module so
nothing reads the real machine.
"""
import json
import re
import time
from pathlib import Path

import pytest

from jacked import skill_listing as sl

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skill_listing"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(sl, "managed_settings_paths", lambda: [])
    monkeypatch.setattr(sl, "claude_code_version", lambda: sl.CALIBRATED_CLAUDE_VERSION)
    for var in (sl.ENV_BUDGET_VAR, "ANTHROPIC_MODEL", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
                "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"):
        monkeypatch.delenv(var, raising=False)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill_md(home: Path, name: str, frontmatter: str) -> Path:
    return _write(home / ".claude" / "skills" / name / "SKILL.md", f"---\n{frontmatter}\n---\nBody\n")


def _settings(home: Path, data: dict) -> Path:
    return _write(home / ".claude" / "settings.json", json.dumps(data))


def _entry(name, desc_len, usage=0.0):
    return sl.ListingEntry(name=name, source="user", group="user", group_label="Your skills",
                           desc_len=desc_len, usage=usage)


# --------------------------------------------------------------------------- #
# 1. YAML alias expansion
# --------------------------------------------------------------------------- #

def _alias_bomb() -> str:
    lines = ["name: bomb", 'a: &a ["xxxxxxxxxx"]']
    prev = "a"
    for lvl in range(9):
        lines.append(f"l{lvl}: &l{lvl} [{', '.join(['*' + prev] * 10)}]")
        prev = f"l{lvl}"
    lines.append(f"description: *{prev}")
    return "\n".join(lines)


def test_alias_bomb_is_bounded_and_warned(tmp_path):
    _skill_md(tmp_path, "bomb", _alias_bomb())
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    start = time.monotonic()
    inv = sl.enumerate_entries(tmp_path, sl.read_listing_settings(tmp_path, environ={}),
                               include_builtins=False)
    assert time.monotonic() - start < 1.0
    by = {e.name: e for e in inv.entries}
    assert by["good"].desc_len == len("Fine.")
    assert "bomb" in {w.name for w in inv.yaml_warnings}
    if "bomb" in by:
        assert by["bomb"].desc_len < 10_000


def test_non_text_description_is_a_warning_not_a_join(tmp_path):
    _skill_md(tmp_path, "nested", "name: nested\ndescription:\n  - [a, b]\n  - {k: v}")
    inv = sl.enumerate_entries(tmp_path, sl.read_listing_settings(tmp_path, environ={}),
                               include_builtins=False)
    assert "nested" in {w.name for w in inv.yaml_warnings}


def test_flat_list_description_is_joined():
    fm, err = sl.parse_frontmatter("---\nname: x\ndescription: [one, two, 3]\n---\n")
    assert err is None
    assert sl._as_text(fm["description"]) == "one two 3"


# --------------------------------------------------------------------------- #
# 2. Deep nesting / other parser crashes
# --------------------------------------------------------------------------- #

def test_deep_nesting_does_not_crash_the_report(tmp_path):
    _skill_md(tmp_path, "deep", "name: deep\ndescription: ok\nx: " + "[" * 5000 + "]" * 5000)
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    rep = sl.build_report(tmp_path, environ={})
    names = {d["name"] for d in rep["strict_yaml_warnings"]}
    assert "deep" in names
    assert rep["counts_by_source"]["user"] == 2


def test_deep_nesting_via_api_is_200(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from jacked.api.routes.skill_listing import router

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    _skill_md(tmp_path, "deep", "name: deep\ndescription: ok\nx: " + "[" * 5000 + "]" * 5000)
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    resp = TestClient(app).get("/api/skill-listing")
    assert resp.status_code == 200
    assert "deep" in {w["name"] for w in resp.json()["strict_yaml_warnings"]}


# --------------------------------------------------------------------------- #
# 3. Workflow meta scan is linear
# --------------------------------------------------------------------------- #

def test_workflow_meta_scan_is_fast_on_large_input():
    filler = ", ".join(f"k{i}: 'v{i}'" for i in range(150_000))
    text = "export const meta = {\n  name: 'big',\n  " + filler + ",\n  description: 'End.',\n}\n"
    assert len(text) > 2_000_000
    start = time.monotonic()
    meta = sl.parse_workflow_meta(text)
    assert time.monotonic() - start < 1.0
    assert meta == {"name": "big", "description": "End."}


# --------------------------------------------------------------------------- #
# 4. Bounded reads
# --------------------------------------------------------------------------- #

def test_frontmatter_read_stops_at_the_fence(tmp_path):
    path = _write(tmp_path / "SKILL.md",
                  "---\nname: a\ndescription: b\n---\n" + ("body line\n" * 300_000))
    text, note = sl.read_frontmatter_text(path)
    assert note is None
    assert text.startswith("---") and len(text) < 1000


def test_oversized_frontmatter_is_skipped_loudly(tmp_path, caplog):
    _skill_md(tmp_path, "huge", "name: huge\ndescription: " + "x" * (sl.MAX_FRONTMATTER_BYTES + 10))
    inv = sl.enumerate_entries(tmp_path, sl.read_listing_settings(tmp_path, environ={}),
                               include_builtins=False)
    assert inv.entries == []
    assert any(x["name"] == "huge" and "1 MB" in x["reason"] for x in inv.excluded)
    assert "huge" in caplog.text or any("huge" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# 5. Fixtures carry no private data
# --------------------------------------------------------------------------- #

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z.]{2,}")


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda p: p.name)
def test_fixtures_contain_no_private_data(path):
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for word in ("hank", "paperclip", "/users/", "jack", "onboard"):
        assert word not in lowered, (path.name, word)
    assert not UUID_RE.search(text), path.name
    assert not EMAIL_RE.search(text), path.name


# --------------------------------------------------------------------------- #
# 6. Synced account ids never leave the module
# --------------------------------------------------------------------------- #

def test_synced_account_dir_is_masked(tmp_path):
    org, acct = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"
    _write(tmp_path / ".claude.json",
           json.dumps({"oauthAccount": {"organizationUuid": org, "accountUuid": acct}}))
    synced = tmp_path / ".claude" / "skills" / "synced" / f"{org}_{acct}"
    _write(synced / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: bad: yaml: here\n---\n")
    for i in range(400):
        _write(synced / f"s{i:03d}" / "SKILL.md", f"---\nname: s{i:03d}\ndescription: {'d' * 900}\n---\n")
    rep = sl.build_report(tmp_path, environ={}, context_window=200_000)
    assert rep["dropped_count"] > 0 and rep["strict_yaml_warnings"]
    dumped = json.dumps(rep)
    assert org not in dumped and acct not in dumped
    # display paths use the platform separator (backslash on Windows, escaped in JSON)
    assert "synced/<account>/" in dumped or "synced\\\\<account>\\\\" in dumped


# --------------------------------------------------------------------------- #
# 7. Recommendation never makes things worse
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fraction", [0.1, 0.05])
def test_recommendation_never_lowers_fraction_or_drops_more(fraction):
    entries = [_entry(f"s{i:04d}", 100) for i in range(3000)]
    budget = sl.budget_chars(fraction, 1_000_000)
    current = sl.simulate(entries, 1536, budget)
    rec = sl.recommend(entries, fraction=fraction, max_desc=1536, context_window=1_000_000)
    if rec is None:
        return
    assert rec["budget_fraction"] >= fraction
    after = sl.simulate(entries, rec["max_desc_chars"],
                        sl.budget_chars(rec["budget_fraction"], 1_000_000))
    assert len(after.dropped) < len(current.dropped)


def test_high_current_fraction_is_never_lowered():
    entries = [_entry(f"s{i:04d}", 400) for i in range(1000)]  # cap 150 > 0.05 budget
    rec = sl.recommend(entries, fraction=0.05, max_desc=1536, context_window=1_000_000)
    assert rec is None or rec["budget_fraction"] >= 0.05


# --------------------------------------------------------------------------- #
# 8. Window derived from the model
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model,window", [
    ("claude-opus-5-5", 1_000_000),
    ("opus", 1_000_000),
    ("claude-sonnet-4-6", 200_000),
    ("claude-sonnet-4-6[1m]", 1_000_000),
    ("haiku", 200_000),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 200_000),
    ("claude-fable-5", 1_000_000),
])
def test_window_for_model(model, window):
    assert sl.context_window_for_model(model, environ={})[0] == window


def test_window_unknown_without_model():
    win, source = sl.context_window_for_model(None, environ={})
    assert win is None and source == "unknown"


def test_disable_1m_env_forces_200k():
    assert sl.context_window_for_model(
        "claude-opus-5-5", environ={"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"})[0] == 200_000


def test_report_derives_window_from_settings_model(tmp_path):
    _settings(tmp_path, {"model": "claude-sonnet-4-6"})
    rep = sl.build_report(tmp_path, environ={})
    assert rep["context_window"] == 200_000 and rep["window_source"] == "model"
    assert rep["window_known"] is True


def test_report_unknown_window_says_so(tmp_path):
    rep = sl.build_report(tmp_path, environ={})
    assert rep["window_known"] is False
    assert "unknown" in rep["summary_short"].lower()


def test_summary_names_the_window_and_the_other_windows_drops(tmp_path):
    for i in range(60):
        _skill_md(tmp_path, f"s{i:02d}", f"name: s{i:02d}\ndescription: {'d' * 300}")
    _settings(tmp_path, {"model": "claude-opus-5-5", "skillListingBudgetFraction": 0.02,
                         "skillListingMaxDescChars": 250})
    rep = sl.build_report(tmp_path, environ={})
    assert rep["fits"] is True
    assert "1M" in rep["summary_short"]
    assert "200k" in rep["summary_short"]  # the 200k window still drops


# --------------------------------------------------------------------------- #
# 9. Model resolution
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model,cpt", [
    ("haiku", 4),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 4),
    ("claude-sonnet-4-5@20250929", 4),
    ("claude-opus-4-20250514", 4),
    ("claude-opus-4-1-20250805", 4),
    ("claude-opus-5-5", 3),
    ("opus", 3),
    ("sonnet", 3),
    ("claude-sonnet-4-6[1m]", 4),
])
def test_chars_per_token_resolves_aliases_and_provider_ids(model, cpt):
    assert sl.chars_per_token_for_model(model) == cpt


def test_resolve_model_canonical():
    assert sl.resolve_model("claude-opus-4-20250514") == "claude-opus-4-0"
    assert sl.resolve_model("global.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"
    assert sl.resolve_model("not-a-model") is None


# --------------------------------------------------------------------------- #
# 10. Merged settings
# --------------------------------------------------------------------------- #

def test_project_and_local_settings_merge_over_user(tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    _settings(home, {"skillOverrides": {"a": "off"}, "skillListingMaxDescChars": 300})
    _write(proj / ".claude" / "settings.json",
           json.dumps({"skillOverrides": {"b": "off"}, "model": "claude-sonnet-4-6"}))
    _write(proj / ".claude" / "settings.local.json", json.dumps({"skillListingMaxDescChars": 400}))
    s = sl.read_listing_settings(home, environ={}, cwd=proj)
    assert s["skill_overrides"] == {"a": "off", "b": "off"}
    assert s["model"] == "claude-sonnet-4-6"
    assert s["max_desc_chars"] == 400
    assert len(s["settings_sources"]) == 3


def test_managed_settings_win(tmp_path, monkeypatch):
    managed = _write(tmp_path / "managed" / "managed-settings.json",
                     json.dumps({"skillListingBudgetFraction": 0.05}))
    monkeypatch.setattr(sl, "managed_settings_paths", lambda: [managed])
    _settings(tmp_path, {"skillListingBudgetFraction": 0.02})
    s = sl.read_listing_settings(tmp_path, environ={})
    assert s["budget_fraction"] == 0.05
    assert s["listing_keys_overridden_by"] == [str(managed)]


def test_disable_bundled_skills(tmp_path):
    _settings(tmp_path, {"disableBundledSkills": True})
    inv = sl.enumerate_entries(tmp_path, sl.read_listing_settings(tmp_path, environ={}))
    assert {e.name for e in inv.entries if e.source == "builtin"} == {"init", "security-review"}
    s = sl.read_listing_settings(tmp_path / "x", environ={"CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1"})
    assert s["disable_bundled_skills"] is True


def test_report_states_scope(tmp_path):
    rep = sl.build_report(tmp_path, environ={})
    assert rep["settings"]["settings_scope"]


# --------------------------------------------------------------------------- #
# 11. Env budget source
# --------------------------------------------------------------------------- #

def test_settings_env_budget_wins_and_is_labelled(tmp_path):
    _settings(tmp_path, {"env": {sl.ENV_BUDGET_VAR: "5000"}})
    s = sl.read_listing_settings(tmp_path, environ={sl.ENV_BUDGET_VAR: "7000"})
    assert s["env_budget"] == 5000 and s["env_budget_from"] == "settings.json env"


def test_process_env_budget_is_labelled_as_process_env(tmp_path):
    s = sl.read_listing_settings(tmp_path, environ={sl.ENV_BUDGET_VAR: "7000"})
    assert s["env_budget"] == 7000
    assert "jacked" in s["env_budget_from"] and "shell" in s["env_budget_from"]


# --------------------------------------------------------------------------- #
# 12. No recommendation the writer would reject
# --------------------------------------------------------------------------- #

def test_env_budget_with_large_fraction_recommendation_is_writable(tmp_path):
    for i in range(80):
        _skill_md(tmp_path, f"s{i:02d}", f"name: s{i:02d}\ndescription: {'d' * 600}")
    _settings(tmp_path, {"skillListingBudgetFraction": 0.5,
                         "env": {sl.ENV_BUDGET_VAR: "3000"}})
    rep = sl.build_report(tmp_path, environ={}, context_window=1_000_000)
    rec = rep["recommendation"]
    if rec is not None:
        sl.apply_listing_settings(tmp_path, rec["max_desc_chars"], rec["budget_fraction"])


def test_writer_accepts_fraction_up_to_one(tmp_path):
    sl.apply_listing_settings(tmp_path, 250, 1.0)
    with pytest.raises(sl.ListingSettingsError):
        sl.apply_listing_settings(tmp_path, 250, 1.01)


# --------------------------------------------------------------------------- #
# 13. Enumeration with known lengths
# --------------------------------------------------------------------------- #

def test_enumeration_over_known_tree_is_exact(tmp_path):
    home = tmp_path
    _skill_md(home, "alpha", "name: alpha\ndescription: " + "a" * 40)
    _skill_md(home, "beta", "name: beta\ndescription: >\n  " + "b" * 30 + "\n  " + "c" * 9
              + "\nwhen_to_use: now")
    _write(home / ".claude" / "commands" / "go.md", "---\ndescription: " + "g" * 12 + "\n---\n")
    _write(home / ".claude" / "commands" / "skip.md",
           "---\ndescription: x\ndisable-model-invocation: true\n---\n")
    inv = sl.enumerate_entries(home, sl.read_listing_settings(home, environ={}),
                               include_builtins=False)
    assert [(e.name, e.desc_len) for e in inv.entries] == [
        ("alpha", 40), ("beta", 30 + 1 + 9 + len(" - now")), ("go", 12)]
    expected = (5 + 4 + 40) + 1 + (4 + 4 + 46) + 1 + (2 + 4 + 12)
    assert sl.listing_chars(inv.entries, 1536) == expected


# --------------------------------------------------------------------------- #
# 14. Unsupported layouts are reported
# --------------------------------------------------------------------------- #

def test_nested_commands_reported_not_counted(tmp_path):
    _write(tmp_path / ".claude" / "commands" / "ns" / "deep.md", "---\ndescription: d\n---\n")
    rep = sl.build_report(tmp_path, environ={})
    assert any("ns" in item["path"] for item in rep["not_counted"])


def test_plugin_custom_paths_reported_not_counted(tmp_path):
    root = tmp_path / "plug"
    _write(root / ".claude-plugin" / "plugin.json",
           json.dumps({"name": "p", "commands": ["./extra/cmds"], "skills": "./more"}))
    _write(tmp_path / ".claude" / "plugins" / "installed_plugins.json", json.dumps(
        {"plugins": {"p@m": [{"installPath": str(root)}]}}))
    _settings(tmp_path, {"enabledPlugins": {"p@m": True}})
    rep = sl.build_report(tmp_path, environ={})
    reasons = " ".join(i["reason"] for i in rep["not_counted"])
    assert "plugin.json" in reasons


# --------------------------------------------------------------------------- #
# 16. Version note
# --------------------------------------------------------------------------- #

def test_version_mismatch_note(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "claude_code_version", lambda: "9.9.9")
    rep = sl.build_report(tmp_path, environ={})
    assert rep["claude_code_version"] == "9.9.9"
    assert sl.CALIBRATED_CLAUDE_VERSION in rep["version_note"] and "9.9.9" in rep["version_note"]


def test_version_match_has_no_note(tmp_path):
    assert sl.build_report(tmp_path, environ={})["version_note"] is None


def test_version_probe_is_cached_and_bounded(monkeypatch):
    """The real probe, with only ``which`` and ``subprocess.run`` faked. The
    conftest guard replaces ``claude_code_version`` for every test, so this one
    reaches the original function through the module's saved reference."""
    calls = []

    class Done:
        stdout = "2.1.300 (Claude Code)\n"
        returncode = 0

    def fake_run(argv, **kw):
        calls.append(kw)
        return Done()

    monkeypatch.setattr(sl.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    monkeypatch.setattr(sl, "_VERSION_CACHE", {})
    probe = sl._probe_claude_code_version
    assert probe() == "2.1.300"
    assert probe() == "2.1.300"
    assert len(calls) == 1 and calls[0]["timeout"] <= 2
    assert calls[0].get("errors") == "replace"
    assert sl.claude_code_version() == sl.CALIBRATED_CLAUDE_VERSION  # guard still active


def test_version_probe_survives_undecodable_output(monkeypatch):
    def fake_run(argv, **kw):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    monkeypatch.setattr(sl.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(sl.subprocess, "run", fake_run)
    monkeypatch.setattr(sl, "_VERSION_CACHE", {})
    assert sl._probe_claude_code_version() is None


def test_plugin_paths_inside_the_default_folder_are_counted_not_reported(tmp_path):
    root = tmp_path / "plug"
    _write(root / ".claude-plugin" / "plugin.json",
           json.dumps({"name": "p", "skills": ["./skills/a", "./skills/b/"], "commands": "./commands"}))
    _write(tmp_path / ".claude" / "plugins" / "installed_plugins.json", json.dumps(
        {"plugins": {"p@m": [{"installPath": str(root)}]}}))
    _settings(tmp_path, {"enabledPlugins": {"p@m": True}})
    assert sl.build_report(tmp_path, environ={})["not_counted"] == []



# --------------------------------------------------------------------------- #
# Wave 2: one file must never kill the report
# --------------------------------------------------------------------------- #

def _get(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from jacked.api.routes.skill_listing import router

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app).get("/api/skill-listing")


def test_bad_yaml_tag_falls_back(tmp_path, monkeypatch):
    _skill_md(tmp_path, "tagged", "name: tagged\ndescription: ok\nother: !!timestamp abc")
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    resp = _get(tmp_path, monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts_by_source"]["user"] == 2
    assert "tagged" in {w["name"] for w in body["strict_yaml_warnings"]}


def test_huge_yaml_int_description_falls_back(tmp_path, monkeypatch):
    _skill_md(tmp_path, "hexy", "name: hexy\ndescription: 0x" + "f" * 4000)
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    resp = _get(tmp_path, monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts_by_source"]["user"] == 2
    assert "hexy" in {w["name"] for w in body["strict_yaml_warnings"]}


def test_any_parser_exception_falls_back(tmp_path, monkeypatch):
    import yaml

    real_load = yaml.load

    def exploding_load(stream, Loader=None):
        if "BOOM" in stream:
            raise RuntimeError("arbitrary parser failure")
        return real_load(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", exploding_load)
    _skill_md(tmp_path, "boom", "name: boom\ndescription: BOOM here")
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    resp = _get(tmp_path, monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts_by_source"]["user"] == 2
    assert "boom" in {w["name"] for w in body["strict_yaml_warnings"]}


def test_any_per_file_exception_becomes_excluded(tmp_path, monkeypatch, caplog):
    real = sl.parse_frontmatter

    def flaky(text):
        if "EXPLODE" in text:
            raise RuntimeError("unexpected")
        return real(text)

    monkeypatch.setattr(sl, "parse_frontmatter", flaky)
    _skill_md(tmp_path, "bad", "name: bad\ndescription: EXPLODE")
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    resp = _get(tmp_path, monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts_by_source"]["user"] == 1
    assert any(x["name"] == "bad" and "RuntimeError" in x["reason"] for x in body["excluded"])
    assert "bad" in caplog.text


@pytest.mark.parametrize("value,ok", [
    ("text", True), (True, True), (1.5, True), (12, True), (10 ** 18 - 1, True),
    (10 ** 18, False), (-(10 ** 18), False), ([1, "a"], True), ([10 ** 20], False),
])
def test_text_field_bounds(value, ok):
    assert sl._text_field_ok(value) is ok


def test_flat_list_join_is_capped():
    text = sl._as_text(["x" * 60_000, "y" * 60_000])
    assert len(text) <= sl.MAX_FIELD_CHARS


def test_untraversable_dirs_are_excluded_not_fatal(tmp_path, monkeypatch):
    import os
    import sys

    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs POSIX permissions and a non-root user")
    _skill_md(tmp_path, "good", "name: good\ndescription: Fine.")
    locked = tmp_path / ".claude" / "skills" / "locked"
    locked.mkdir(parents=True)
    root = tmp_path / "plug"
    (root / "skills" / "inner").mkdir(parents=True)
    _write(tmp_path / ".claude" / "plugins" / "installed_plugins.json", json.dumps(
        {"plugins": {"p@m": [{"installPath": str(root)}]}}))
    _settings(tmp_path, {"enabledPlugins": {"p@m": True}})
    locked.chmod(0)
    (root / "skills" / "inner").chmod(0)
    try:
        resp = _get(tmp_path, monkeypatch)
    finally:
        locked.chmod(0o755)
        (root / "skills" / "inner").chmod(0o755)
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts_by_source"]["user"] == 1
    reasons = {x["name"]: x["reason"] for x in body["excluded"]}
    assert "locked" in reasons and "PermissionError" in reasons["locked"]


# --------------------------------------------------------------------------- #
# Wave 2: model precedence
# --------------------------------------------------------------------------- #

def test_anthropic_model_env_beats_settings_model(tmp_path):
    _settings(tmp_path, {"model": "opus", "env": {"ANTHROPIC_MODEL": "claude-sonnet-4-5"}})
    rep = sl.build_report(tmp_path, environ={})
    assert rep["context_window"] == 200_000 and rep["chars_per_token"] == 4
    assert rep["settings"]["model"] == "claude-sonnet-4-5"
    assert rep["settings"]["model_from"] == "ANTHROPIC_MODEL (settings.json env)"


def test_process_anthropic_model_beats_settings_model(tmp_path):
    _settings(tmp_path, {"model": "opus"})
    s = sl.read_listing_settings(tmp_path, environ={"ANTHROPIC_MODEL": "claude-sonnet-4-5"})
    assert s["model"] == "claude-sonnet-4-5"
    assert "process environment" in s["model_from"]


def test_settings_env_model_beats_process_env_model(tmp_path):
    _settings(tmp_path, {"env": {"ANTHROPIC_MODEL": "claude-opus-5-5"}})
    s = sl.read_listing_settings(tmp_path, environ={"ANTHROPIC_MODEL": "claude-sonnet-4-5"})
    assert s["model"] == "claude-opus-5-5"


def test_settings_model_beats_default_model(tmp_path):
    _settings(tmp_path, {"model": "opus", "env": {"ANTHROPIC_DEFAULT_MODEL": "claude-sonnet-4-5"}})
    s = sl.read_listing_settings(tmp_path, environ={})
    assert s["model"] == "opus" and s["model_from"] == "settings model"
    s2 = sl.read_listing_settings(tmp_path / "x", environ={"ANTHROPIC_DEFAULT_MODEL": "haiku"})
    assert s2["model"] == "haiku" and "ANTHROPIC_DEFAULT_MODEL" in s2["model_from"]


# --------------------------------------------------------------------------- #
# Wave 2: messages name the window and the effective source
# --------------------------------------------------------------------------- #

def test_applied_text_names_the_window(tmp_path):
    rep = sl.apply_action(tmp_path, "explicit", max_desc_chars=250, budget_fraction=0.02,
                          context_window=200_000)
    assert "200k-token" in rep["message"]


def test_apply_message_says_when_another_source_overrides(tmp_path, monkeypatch):
    managed = _write(tmp_path / "managed" / "managed-settings.json",
                     json.dumps({"skillListingBudgetFraction": 0.05}))
    monkeypatch.setattr(sl, "managed_settings_paths", lambda: [managed])
    rep = sl.apply_action(tmp_path, "explicit", max_desc_chars=250, budget_fraction=0.02)
    msg = rep["message"]
    assert str(managed) in msg and "overrid" in msg.lower()
    assert not msg.startswith("Saved. Descriptions are capped")
