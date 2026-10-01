"""Tests for jacked.skill_listing: enumeration, budget math, drop order,
recommendation, the settings writer, and the calibration snapshot.

Every test builds a throwaway home under ``tmp_path`` and passes it in
explicitly, so nothing here reads or writes the real ``~/.claude``.
"""
import json
import os
import re
from pathlib import Path

import pytest

from jacked import skill_listing as sl
from jacked.memory.settings_io import SettingsUnreadableError
from tests._platform import requires_symlinks

FIXTURES = Path(__file__).parent.parent / "fixtures" / "skill_listing"
DATA_ROOT = Path(__file__).parent.parent.parent / "jacked" / "data"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _skill(root: Path, name: str, description: str | None = "Does a thing.", *,
           extra: str = "", raw_frontmatter: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if raw_frontmatter is not None:
        fm = raw_frontmatter
    else:
        lines = [f"name: {name}"]
        if description is not None:
            lines.append(f"description: {json.dumps(description)}")
        fm = "\n".join(lines) + ("\n" + extra if extra else "")
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n\nBody.\n", encoding="utf-8")
    return d


def _command(root: Path, name: str, description: str | None = "Runs a command.", extra: str = ""):
    root.mkdir(parents=True, exist_ok=True)
    fm = f"description: {json.dumps(description)}\n" if description is not None else ""
    (root / f"{name}.md").write_text(f"---\n{fm}{extra}---\n\nDo it.\n", encoding="utf-8")


def _settings(home: Path, data: dict) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _plugin(home: Path, key: str, *, skills=(), commands=(), workflows=()) -> Path:
    plugin = key.split("@")[0]
    root = home / ".claude" / "plugins" / "cache" / "mkt" / plugin / "1.0.0"
    for name, desc in skills:
        _skill(root / "skills", name, desc)
    for name, desc in commands:
        _command(root / "commands", name, desc)
    for fname, src in workflows:
        (root / "workflows").mkdir(parents=True, exist_ok=True)
        (root / "workflows" / fname).write_text(src, encoding="utf-8")
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(installed.read_text()) if installed.exists() else {"version": 2, "plugins": {}}
    data["plugins"][key] = [{"scope": "user", "installPath": str(root), "version": "1.0.0"}]
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(json.dumps(data), encoding="utf-8")
    return root


def _entries(home: Path, **kw) -> sl.Inventory:
    return sl.enumerate_entries(home, sl.read_listing_settings(home, environ={}),
                                include_builtins=False, **kw)


def _names(inv: sl.Inventory) -> list[str]:
    return [e.name for e in inv.entries]


def _entry(name: str, desc_len: int, usage: float = 0.0, protected: bool = False):
    return sl.ListingEntry(name=name, source="user", group="user", group_label="Your skills",
                           desc_len=desc_len, usage=usage, protected=protected)


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #

class TestEnumeration:
    def test_user_skills_and_commands(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "alpha", "Alpha skill.")
        _skill(tmp_path / ".claude" / "skills", "beta", "Beta skill.")
        _command(tmp_path / ".claude" / "commands", "go", "Go command.")
        inv = _entries(tmp_path)
        assert _names(inv) == ["alpha", "beta", "go"]
        by = {e.name: e for e in inv.entries}
        assert by["alpha"].source == "user" and by["go"].source == "command"
        assert by["alpha"].desc_len == len("Alpha skill.")

    def test_duplicate_names_keep_the_first_entry(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "same", "Skill version.")
        _command(tmp_path / ".claude" / "commands", "same", "Command version.")
        inv = _entries(tmp_path)
        assert _names(inv) == ["same"] and inv.entries[0].source == "user"
        assert inv.excluded[0] == {"name": "same", "source": "command", "reason": "duplicate name"}

    def test_when_to_use_is_appended(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "w", "Desc.", extra="when_to_use: Use it now.")
        (entry,) = _entries(tmp_path).entries
        assert entry.desc_len == len("Desc. - Use it now.")

    def test_desc_len_counts_utf16_units_like_javascript(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "emoji", "ok \U0001F600")
        (entry,) = _entries(tmp_path).entries
        assert entry.desc_len == 5  # "ok " + one astral char (2 UTF-16 units)

    @requires_symlinks
    def test_symlinked_skill_is_followed(self, tmp_path):
        real = _skill(tmp_path / ".agents" / "skills", "linked", "Linked skill.")
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        os.symlink(real, tmp_path / ".claude" / "skills" / "linked")
        assert _names(_entries(tmp_path)) == ["linked"]

    def test_pack_skills_group_under_their_pack(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "ads", "Paid ads.")
        _skill(tmp_path / ".claude" / "skills", "mine", "Mine.")
        inv = _entries(tmp_path, data_root=DATA_ROOT)
        by = {e.name: e for e in inv.entries}
        assert by["ads"].group == "pack:marketing"
        assert by["ads"].group_label == "Marketing Skills (skill pack)"
        assert by["mine"].group == "user"

    def test_disable_model_invocation_is_excluded(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "hidden", "Hidden.",
               extra="disable-model-invocation: true")
        _command(tmp_path / ".claude" / "commands", "manual", "Manual.",
                 extra="disable-model-invocation: true\n")
        inv = _entries(tmp_path)
        assert inv.entries == []
        assert {x["reason"] for x in inv.excluded} == {"disable-model-invocation"}

    def test_entry_without_description_is_excluded(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "bare", None)
        inv = _entries(tmp_path)
        assert inv.entries == [] and inv.excluded[0]["reason"] == "no description"

    @pytest.mark.parametrize("mode", ["off", "user-invocable-only"])
    def test_skill_overrides_remove_user_skills(self, tmp_path, mode):
        _skill(tmp_path / ".claude" / "skills", "gone", "Gone.")
        _settings(tmp_path, {"skillOverrides": {"gone": mode}})
        inv = _entries(tmp_path)
        assert inv.entries == []
        assert inv.excluded[0]["reason"] == f"skillOverrides {mode}"

    def test_skill_override_name_only_shrinks_and_protects(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "short", "A long description here.")
        _settings(tmp_path, {"skillOverrides": {"short": "name-only"}})
        (entry,) = _entries(tmp_path).entries
        assert entry.name_only and entry.protected
        assert entry.full_chars(1536) == len("- short")

    def test_enabled_plugin_skills_and_commands(self, tmp_path):
        _plugin(tmp_path, "tools@mkt", skills=[("lint", "Lint code.")],
                commands=[("fix", "Fix code.")])
        _plugin(tmp_path, "off@mkt", skills=[("nope", "Disabled plugin.")])
        _settings(tmp_path, {"enabledPlugins": {"tools@mkt": True, "off@mkt": False}})
        inv = _entries(tmp_path)
        assert _names(inv) == ["tools:fix", "tools:lint"]
        assert all(e.source == "plugin" and e.group == "plugin:tools" for e in inv.entries)

    def test_plugin_skill_uses_frontmatter_name(self, tmp_path):
        root = _plugin(tmp_path, "cave@mkt")
        _skill(root / "skills", "compress", None,
               raw_frontmatter="name: cave-compress\ndescription: Squeeze files.")
        _settings(tmp_path, {"enabledPlugins": {"cave@mkt": True}})
        assert _names(_entries(tmp_path)) == ["cave:cave-compress"]

    def test_plugin_entries_ignore_skill_overrides(self, tmp_path):
        _plugin(tmp_path, "tools@mkt", skills=[("lint", "Lint code.")])
        _settings(tmp_path, {"enabledPlugins": {"tools@mkt": True},
                             "skillOverrides": {"tools:lint": "off", "lint": "off"}})
        assert _names(_entries(tmp_path)) == ["tools:lint"]

    def test_plugin_workflow_meta_is_listed(self, tmp_path):
        src = (
            "export const meta = {\n"
            "  name: 'flow-scan',\n"
            "  description:\n    'Scans things \\u2014 fast',\n"
            "  whenToUse: \"Use \" + \"when asked\",\n"
            "  phases: [{ title: 'x', name: 'ignored' }],\n"
            "}\n"
        )
        _plugin(tmp_path, "wf@mkt", workflows=[("scan.js", src)])
        _settings(tmp_path, {"enabledPlugins": {"wf@mkt": True}})
        (entry,) = _entries(tmp_path).entries
        assert entry.name == "wf:flow-scan"
        assert entry.desc_len == len("Scans things — fast - Use when asked")

    def test_workflow_without_meta_is_reported_not_listed(self, tmp_path):
        _plugin(tmp_path, "wf@mkt", workflows=[("x.js", "export default 1\n")])
        _settings(tmp_path, {"enabledPlugins": {"wf@mkt": True}})
        inv = _entries(tmp_path)
        assert inv.entries == []
        assert inv.excluded[0]["reason"] == "workflow meta not readable"

    def test_synced_skills_of_signed_in_account_with_collision_prefix(self, tmp_path):
        (tmp_path / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"organizationUuid": "org1", "accountUuid": "acc1"}}))
        synced = tmp_path / ".claude" / "skills" / "synced"
        _skill(synced / "org1_acc1", "pdf", "PDF work.")
        _skill(synced / "org1_acc1", "lint", "Lint docs.")
        _skill(synced / "org2_acc9", "other", "Other account.")
        _plugin(tmp_path, "tools@mkt", skills=[("lint", "Lint code.")])
        _settings(tmp_path, {"enabledPlugins": {"tools@mkt": True}})
        inv = _entries(tmp_path)
        assert _names(inv) == ["tools:lint", "anthropic-skills:lint", "pdf"]
        assert [e.source for e in inv.entries] == ["plugin", "synced", "synced"]

    def test_builtins_are_included_and_mostly_protected(self, tmp_path):
        inv = sl.enumerate_entries(tmp_path, sl.read_listing_settings(tmp_path, environ={}))
        builtins = [e for e in inv.entries if e.source == "builtin"]
        assert len(builtins) == len(sl.BUILTIN_ENTRIES)
        unprotected = {e.name for e in builtins if not e.protected}
        assert unprotected == {"init", "security-review"}


class TestStrictYaml:
    def test_unquoted_colon_falls_back_and_warns(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "colon", None,
               raw_frontmatter="name: colon\ndescription: Build it in one shot: fast and clean")
        inv = _entries(tmp_path)
        (entry,) = inv.entries
        assert entry.desc_len == len("Build it in one shot: fast and clean")
        (warning,) = inv.yaml_warnings
        assert warning.name == "colon"
        assert warning.path.endswith("SKILL.md")
        assert "line" in warning.error

    def test_lenient_parse_reads_block_scalars(self):
        fm, err = sl.parse_frontmatter(
            "---\nname: x\ndescription: >\n  First line: here\n  second line.\nbad: a: b\n---\n"
        )
        assert err is not None
        assert fm["description"] == "First line: here second line."

    def test_valid_yaml_has_no_warning(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "fine", "Quoted: fine")
        assert _entries(tmp_path).yaml_warnings == []

    def test_no_frontmatter_is_empty(self):
        assert sl.parse_frontmatter("# Just a heading\n") == ({}, None)


# --------------------------------------------------------------------------- #
# Settings + usage
# --------------------------------------------------------------------------- #

class TestSettingsAndUsage:
    def test_defaults_when_no_settings(self, tmp_path):
        s = sl.read_listing_settings(tmp_path, environ={})
        assert s["max_desc_chars"] == 1536 and not s["max_desc_chars_set"]
        assert s["budget_fraction"] == 0.01 and not s["budget_fraction_set"]
        assert s["env_budget"] is None and not s["settings_unreadable"]

    def test_unreadable_settings_degrade_for_reads(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("{nope", encoding="utf-8")
        s = sl.read_listing_settings(tmp_path, environ={})
        assert s["settings_unreadable"] and s["budget_fraction"] == 0.01

    def test_settings_env_budget_wins_over_process_env(self, tmp_path):
        # Claude Code copies settings ``env`` into its own environment, so it wins.
        _settings(tmp_path, {"env": {sl.ENV_BUDGET_VAR: "5000"}})
        s = sl.read_listing_settings(tmp_path, environ={sl.ENV_BUDGET_VAR: "7000"})
        assert s["env_budget"] == 5000 and s["env_budget_from"] == "settings.json env"
        s2 = sl.read_listing_settings(tmp_path / "other", environ={sl.ENV_BUDGET_VAR: "7000"})
        assert s2["env_budget"] == 7000 and "process environment" in s2["env_budget_from"]

    def test_usage_score_decays_like_claude_code(self, tmp_path):
        now = 1_800_000_000.0
        week_ms = 7 * 86_400_000
        (tmp_path / ".claude.json").write_text(json.dumps({"skillUsage": {
            "fresh": {"usageCount": 10, "lastUsedAt": now * 1000},
            "week": {"usageCount": 10, "lastUsedAt": now * 1000 - week_ms},
            "ancient": {"usageCount": 10, "lastUsedAt": now * 1000 - 100 * week_ms},
            "junk": "x",
        }}))
        scores = sl.usage_scores(tmp_path, now)
        assert scores["fresh"] == pytest.approx(10)
        assert scores["week"] == pytest.approx(5)
        assert scores["ancient"] == pytest.approx(1)  # floor at 0.1 x count
        assert "junk" not in scores


# --------------------------------------------------------------------------- #
# Budget math
# --------------------------------------------------------------------------- #

class TestBudget:
    def test_fraction_times_window_times_factor(self):
        assert sl.budget_chars(0.01, 1_000_000) == 30_000
        assert sl.budget_chars(0.02, 200_000) == 12_000
        assert sl.budget_chars(0.01, 1_000_000, chars_per_token=4) == 40_000

    def test_env_override_wins(self):
        assert sl.budget_chars(0.05, 1_000_000, env_budget=1234) == 1234

    def test_chars_per_token_for_model(self):
        assert sl.chars_per_token_for_model("claude-opus-5-5") == 3
        assert sl.chars_per_token_for_model("opus") == 3
        assert sl.chars_per_token_for_model(None) == 3
        assert sl.chars_per_token_for_model("claude-sonnet-4-6") == 4
        assert sl.chars_per_token_for_model("claude-sonnet-4.5-20250929") == 4
        assert sl.chars_per_token_for_model("claude-opus-4-6[1m]") == 4

    def test_listing_chars_matches_claude_code_format(self):
        entries = [_entry("ab", 10), _entry("cde", 5)]
        # "- ab: " + 10 chars, newline, "- cde: " + 5 chars
        assert sl.listing_chars(entries, 1536) == (2 + 4 + 10) + 1 + (3 + 4 + 5)
        assert sl.listing_chars(entries, 3) == (2 + 4 + 3) + 1 + (3 + 4 + 3)

    def test_report_uses_window(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "a", "x" * 100)
        small = sl.build_report(tmp_path, context_window=200_000, environ={})
        big = sl.build_report(tmp_path, context_window=1_000_000, environ={})
        assert small["budget_chars"] == 6_000 and big["budget_chars"] == 30_000
        assert set(big["windows"]) == {"200000", "1000000"}


# --------------------------------------------------------------------------- #
# Drop simulation
# --------------------------------------------------------------------------- #

class TestDropOrder:
    def test_fits_drops_nothing(self):
        sim = sl.simulate([_entry("a", 10)], 1536, 1000)
        assert sim.fits and sim.dropped == []

    def test_least_used_descriptions_drop_first(self):
        entries = [_entry("low", 50, usage=1), _entry("high", 50, usage=9),
                   _entry("mid", 50, usage=5)]
        # bare names: 5+6+5 + 2 newlines = 18; room for two 52-char extras.
        sim = sl.simulate(entries, 1536, 18 + 52 * 2)
        assert [e.name for e in sim.dropped] == ["low"]

    def test_ties_keep_listing_order(self):
        entries = [_entry("aa", 50), _entry("bb", 50), _entry("cc", 50)]
        sim = sl.simulate(entries, 1536, (4 * 3 + 2) + 52 * 2)
        assert [e.name for e in sim.dropped] == ["cc"]

    def test_greedy_fill_skips_an_entry_that_does_not_fit(self):
        entries = [_entry("big", 500, usage=9), _entry("small", 10, usage=1)]
        sim = sl.simulate(entries, 1536, (5 + 7 + 1) + 20)
        assert [e.name for e in sim.dropped] == ["big"]

    def test_protected_entries_never_drop(self):
        entries = [_entry("keep", 100, protected=True), _entry("go", 100, usage=50)]
        sim = sl.simulate(entries, 1536, 50)
        assert [e.name for e in sim.dropped] == ["go"]
        assert sim.emitted_chars == (4 + 4 + 100) + 1 + (2 + 2)


# --------------------------------------------------------------------------- #
# Recommendation
# --------------------------------------------------------------------------- #

class TestRecommend:
    def _many(self, n=100, desc=400):
        return [_entry(f"s{i:03d}", desc) for i in range(n)]

    def test_none_when_it_fits(self):
        assert sl.recommend([_entry("a", 10)], fraction=0.01, max_desc=1536,
                            context_window=1_000_000) is None

    def test_largest_cap_that_fits_at_current_fraction(self):
        entries = self._many()  # 100 x (4 + 4 + desc) + 99
        rec = sl.recommend(entries, fraction=0.01, max_desc=1536, context_window=1_000_000)
        assert rec["budget_fraction"] == 0.01
        cap = rec["max_desc_chars"]
        assert sl.listing_chars(entries, cap) <= 30_000
        assert cap + 50 > 400 or sl.listing_chars(entries, cap + 50) > 30_000
        assert rec["fits"] and rec["extra_tokens"] == 0

    def test_fallback_raises_fraction_at_250(self):
        entries = self._many(n=200, desc=400)  # 200 x 158 at cap 150 > 30,000
        rec = sl.recommend(entries, fraction=0.01, max_desc=1536, context_window=1_000_000)
        assert rec["max_desc_chars"] == 250
        assert rec["budget_fraction"] == 0.02
        assert rec["fits"]
        assert rec["extra_tokens"] > 0
        assert rec["summary"].startswith("Cap descriptions at 250 characters and use a 2% budget")
        assert "more tokens per session" in rec["summary"]

    def test_nothing_fits_says_so(self):
        entries = self._many(n=2000, desc=400)
        budget = sl.budget_chars(0.01, 200_000)
        current = sl.simulate(entries, 1536, budget)
        rec = sl.recommend(entries, fraction=0.01, max_desc=1536, context_window=200_000)
        assert rec["fits"] is False and rec["budget_fraction"] >= 0.01
        assert rec["dropped_after"] < len(current.dropped)
        assert "Turn off skills" in rec["summary"]

    def test_env_budget_cannot_be_raised(self):
        entries = self._many(n=200, desc=400)
        rec = sl.recommend(entries, fraction=0.01, max_desc=1536, context_window=1_000_000,
                           env_budget=20_000)
        assert rec["budget_from_env"] and rec["fits"] is False
        assert rec["budget_fraction"] == 0.01 and rec["max_desc_chars"] == 150
        assert sl.ENV_BUDGET_VAR in rec["summary"]

    def test_env_budget_too_small_for_any_gain_is_no_recommendation(self):
        entries = self._many(n=200, desc=400)
        assert sl.recommend(entries, fraction=0.01, max_desc=1536, context_window=1_000_000,
                            env_budget=1000) is None

    def test_summary_has_no_em_dash(self):
        rec = sl.recommend(self._many(n=200), fraction=0.01, max_desc=1536,
                           context_window=1_000_000)
        assert "—" not in rec["summary"]


class TestReport:
    def test_report_shape_and_groups(self, tmp_path):
        skills = tmp_path / ".claude" / "skills"
        for i in range(60):
            _skill(skills, f"user-{i:02d}", "x" * 600)
        _skill(skills, "ads", "y" * 600)
        rep = sl.build_report(tmp_path, environ={}, data_root=DATA_ROOT)
        assert not rep["fits"] and rep["dropped_count"] > 0
        labels = {g["label"] for g in rep["dropped_groups"]}
        assert "Your skills" in labels
        assert rep["summary"].startswith("Skill listing: ")
        assert "With a 1M-token context window, " in rep["summary"]
        assert "without a description, so Claude rarely picks them" in rep["summary_short"]
        assert rep["recommendation"] is not None
        json.dumps(rep)  # serializable

    def test_report_ok_summary(self, tmp_path):
        _skill(tmp_path / ".claude" / "skills", "one", "Short.")
        rep = sl.build_report(tmp_path, environ={})
        assert rep["fits"] and rep["recommendation"] is None
        assert "every skill shows its description" in rep["summary_short"]
        assert "1M-token" in rep["summary_short"]


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

class TestWriter:
    def test_only_the_two_keys_change(self, tmp_path):
        original = {"hooks": {"Stop": [{"x": 1}]}, "env": {"A": "1"}, "model": "opus",
                    "skillListingMaxDescChars": 999}
        path = _settings(tmp_path, original)
        sl.apply_listing_settings(tmp_path, 250, 0.02)
        data = json.loads(path.read_text())
        assert data["skillListingMaxDescChars"] == 250
        assert data["skillListingBudgetFraction"] == 0.02
        rest = {k: v for k, v in data.items() if not k.startswith("skillListing")}
        assert rest == {k: v for k, v in original.items() if not k.startswith("skillListing")}

    def test_creates_settings_when_missing(self, tmp_path):
        sl.apply_listing_settings(tmp_path, 300, 0.015)
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert data == {"skillListingMaxDescChars": 300, "skillListingBudgetFraction": 0.015}

    def test_unreadable_settings_refused_and_untouched(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        path = tmp_path / ".claude" / "settings.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(SettingsUnreadableError):
            sl.apply_listing_settings(tmp_path, 250, 0.02)
        with pytest.raises(SettingsUnreadableError):
            sl.reset_listing_settings(tmp_path)
        assert path.read_text() == "{broken"

    @pytest.mark.parametrize("value", [99, 1537, 250.0, True, "250", None])
    def test_max_desc_range(self, tmp_path, value):
        with pytest.raises(sl.ListingSettingsError):
            sl.apply_listing_settings(tmp_path, value, 0.02)
        assert not (tmp_path / ".claude" / "settings.json").exists()

    @pytest.mark.parametrize("value", [0, -0.01, 1.01, float("nan"), float("inf"), True, "0.02"])
    def test_fraction_range(self, tmp_path, value):
        with pytest.raises(sl.ListingSettingsError):
            sl.apply_listing_settings(tmp_path, 250, value)

    def test_bounds_are_inclusive(self, tmp_path):
        sl.apply_listing_settings(tmp_path, 100, 1.0)
        sl.apply_listing_settings(tmp_path, 1536, 0.001)

    def test_reset_removes_keys_only(self, tmp_path):
        path = _settings(tmp_path, {"model": "opus", "skillListingMaxDescChars": 250,
                                    "skillListingBudgetFraction": 0.02})
        assert sl.reset_listing_settings(tmp_path) is True
        assert json.loads(path.read_text()) == {"model": "opus"}
        assert sl.reset_listing_settings(tmp_path) is False

    def test_reset_with_no_file_is_a_no_op(self, tmp_path):
        assert sl.reset_listing_settings(tmp_path) is False
        assert not (tmp_path / ".claude" / "settings.json").exists()


# --------------------------------------------------------------------------- #
# Calibration snapshot (real /context numbers from this machine, 2026-10-01)
# --------------------------------------------------------------------------- #

def _context_skills_tokens(name: str) -> float:
    text = (FIXTURES / f"context_{name}.md").read_text(encoding="utf-8")
    m = re.search(r"^\| Skills \| ([\d.]+)k \|", text, re.M)
    return float(m.group(1)) * 1000


def _context_small_rows(name: str) -> set[str]:
    text = (FIXTURES / f"context_{name}.md").read_text(encoding="utf-8")
    section = text[text.index("### Skills"):]
    return {m.group(1) for m in re.finditer(r"^\| (\S+) \| [^|]+ \| < 20 \|$", section, re.M)}


def _snapshot_entries() -> list[sl.ListingEntry]:
    snap = json.loads((FIXTURES / "inventory_snapshot.json").read_text(encoding="utf-8"))
    return sl.entries_from_snapshot(snap)


class TestCalibration:
    @pytest.mark.parametrize("fixture,max_desc", [("full", 1536), ("cap250", 250)])
    def test_estimator_within_one_percent_of_context(self, fixture, max_desc):
        entries = _snapshot_entries()
        measured = _context_skills_tokens(fixture)
        estimated = sl.listing_chars(entries, max_desc) / sl.CHARS_PER_TOKEN
        # /context rounds the row to 0.1k, so 1% is the tightest honest bound.
        assert abs(estimated - measured) / measured < 0.01, (estimated, measured)

    def test_snapshot_matches_context_entry_names(self):
        text = (FIXTURES / "context_full.md").read_text(encoding="utf-8")
        section = text[text.index("### Skills"):]
        rows = re.findall(r"^\| (\S+) \| [^|]+ \| (?:< 20|~\d+) \|$", section, re.M)
        assert sorted(rows) == sorted(e.name for e in _snapshot_entries())

    def test_default_budget_drop_count_matches_context(self):
        entries = _snapshot_entries()
        budget = sl.budget_chars(0.01, 1_000_000)
        assert sl.tokens(budget) == pytest.approx(_context_skills_tokens("default"), rel=0.05)
        sim = sl.simulate(entries, 1536, budget)
        dropped = {e.name for e in sim.dropped}
        measured = _context_small_rows("default")
        naturally_small = {e.name for e in entries if e.full_chars(1536) / sl.CHARS_PER_TOKEN < 20}
        assert abs(len(dropped | naturally_small) - len(measured)) <= 5
        assert len(dropped & measured) / len(measured) > 0.95
