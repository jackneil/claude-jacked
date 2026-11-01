"""Tests for the release-shipped usage-window catalog (jacked/usage_plans.py).

The catalog says which usage windows each provider plan has, what the plan
is called, and how long each window is. The live payload stays the truth for
one account; the catalog decides how to read silence for a plan.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jacked import usage_plans as up

EM_DASH = "—"
EN_DASH = "–"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Every test starts with the bundled catalog and an empty drift log."""
    monkeypatch.setattr(up, "_catalog_cache", None)
    monkeypatch.setattr(up, "_drift_logged", set())


ULTRA_RAW = {
    "five_hour": None,
    "seven_day": {"utilization": 33.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 33,
         "resets_at": "2026-11-02T22:59:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 61,
         "resets_at": "2026-11-02T22:59:59+00:00", "is_active": True,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}

SESSION_RAW = {
    "five_hour": {"utilization": 67.0, "resets_at": "2026-11-01T18:59:59+00:00"},
    "seven_day": {"utilization": 23.0, "resets_at": "2026-11-02T04:59:59+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 67,
         "resets_at": "2026-11-01T18:59:59+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 23,
         "resets_at": "2026-11-02T04:59:59+00:00", "scope": None, "is_active": False},
    ],
}

# Authoritative Claude payload (limits list present) that reports no session.
WEEKLY_ONLY_RAW = {
    "five_hour": None,
    "seven_day": {"utilization": 10.0, "resets_at": "2026-11-05T00:00:00+00:00"},
    "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 10}],
}

CODEX_PRO_RAW = {
    "rateLimits": {
        "primary": {"usedPercent": 38, "windowDurationMins": 10080, "resetsAt": 1793780314},
        "secondary": None,
        "planType": "pro",
    },
}

CODEX_PLUS_RAW = {
    "rateLimits": {
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1793780000},
        "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1793990000},
        "planType": "plus",
    },
}


def _ultra(**extra):
    row = {"provider": "claude", "email": "u@x.com", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_20x"}
    row.update(extra)
    return row


def _profile(plan_name, email="u@x.com", org_uuid=None):
    """A stored profile that belongs to the row (the owner rule needs
    account.email, and organization.uuid when the row has an org)."""
    org = {"plan_display_name": plan_name}
    if org_uuid:
        org["uuid"] = org_uuid
    return json.dumps({"account": {"email": email}, "organization": org})


def _max5(**extra):
    row = {"provider": "claude", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_5x"}
    row.update(extra)
    return row


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / up.CATALOG_FILE
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# Bundled catalog
# --------------------------------------------------------------------------- #


class TestBundledCatalog:
    def test_loads_without_warnings(self, caplog):
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(up.DATA_ROOT)
        assert [r.getMessage() for r in caplog.records] == []
        assert cat.version == 1
        assert cat.verified_at == "2026-11-01"
        assert {"claude", "codex"} <= set(cat.providers)

    def test_ships_inside_the_package(self):
        assert (up.DATA_ROOT / up.CATALOG_FILE).is_file()
        assert up.DATA_ROOT == Path(up.__file__).resolve().parents[1] / "data"

    def test_catalog_is_not_excluded_from_the_wheel(self):
        """Hatchling ships every file under jacked/ that git does not ignore
        (pyproject.toml: packages = ["jacked"], no include list). A .gitignore
        pattern that matched the catalog would drop it from the wheel while
        the source tree, and every other test here, still had it."""
        import shutil
        import subprocess

        if shutil.which("git") is None:
            pytest.skip("git not installed")
        repo = Path(up.__file__).resolve().parents[2]
        if not (repo / ".git").exists():
            pytest.skip("not a git checkout")
        result = subprocess.run(
            ["git", "check-ignore", "-q", "jacked/data/usage_plans.json"],
            cwd=repo, capture_output=True,
        )
        assert result.returncode == 1, "jacked/data/usage_plans.json is git-ignored"

    def test_known_plans_are_present(self):
        cat = up.load_catalog(up.DATA_ROOT)
        claude = cat.providers["claude"]
        assert set(claude.subscriptions) >= {"max", "team", "pro", "free", "enterprise"}
        tiers = claude.subscriptions["max"].tiers
        assert tiers["default_claude_max_20x"].id == "ultra"
        assert tiers["default_claude_max_5x"].id == "max"
        codex = cat.providers["codex"]
        assert codex.subscriptions["pro"].plan.session == "never"

    def test_every_plan_carries_provenance(self):
        cat = up.load_catalog(up.DATA_ROOT)
        for provider, pcat in cat.providers.items():
            for name, sub in pcat.subscriptions.items():
                entries = [sub.plan, *sub.tiers.values()]
                for entry in entries:
                    assert entry.verified_at, (provider, name, entry.id)
                    assert entry.sources, (provider, name, entry.id)
                    assert all(s.startswith("https://") for s in entry.sources)

    def test_user_facing_strings_have_no_prose_dashes(self):
        # The labels and notes render in the dashboard (no em-dash rule).
        cat = up.load_catalog(up.DATA_ROOT)
        for pcat in cat.providers.values():
            for sub in pcat.subscriptions.values():
                for entry in (sub.plan, *sub.tiers.values()):
                    for text in (entry.label, entry.formerly, entry.session_note,
                                 entry.session_temporary_note):
                        if text:
                            assert EM_DASH not in text and EN_DASH not in text, text

    def test_window_lengths(self):
        assert up.window_seconds("claude", "session") == 5 * 3600
        assert up.window_seconds("claude", "weekly") == 7 * 86400
        assert up.window_seconds("claude", "weekly_model") == 7 * 86400
        assert up.window_seconds("codex", "session") == 5 * 3600
        # An unknown provider falls back to the built-in lengths.
        assert up.window_seconds("other", "weekly") == 7 * 86400
        with pytest.raises(ValueError):
            up.window_seconds("claude", "monthly")


# --------------------------------------------------------------------------- #
# Loader degradation
# --------------------------------------------------------------------------- #


class TestLoader:
    def test_missing_file_degrades_to_defaults(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(tmp_path)
        assert cat.providers == {}
        assert any("not found" in r.getMessage() for r in caplog.records)

    def test_malformed_json_degrades(self, tmp_path, caplog):
        root = _write(tmp_path, "{not json")
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        assert cat.providers == {}
        assert any("unreadable" in r.getMessage() for r in caplog.records)

    def test_non_object_top_level_degrades(self, tmp_path, caplog):
        root = _write(tmp_path, [1, 2])
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        assert cat.providers == {}

    @pytest.mark.parametrize("version", [None, "1", True, 0, -3])
    def test_bad_version_degrades(self, tmp_path, caplog, version):
        root = _write(tmp_path, {"version": version, "providers": {}})
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        assert cat.providers == {}
        assert any("version" in r.getMessage() for r in caplog.records)

    def test_newer_version_warns_and_reads_best_effort(self, tmp_path, caplog):
        root = _write(tmp_path, {
            "version": up.SUPPORTED_VERSION + 1,
            "providers": {"claude": {"subscriptions": {
                "pro": {"label": "Pro", "session": "always", "session_critical_pct": 80}}}},
        })
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        assert "pro" in cat.providers["claude"].subscriptions
        assert any("newer" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("bad", [
        {"label": "Pro", "session": "sometimes"},
        {"label": "Pro", "session": "always", "session_critical_pct": True},
        {"label": "Pro", "session": "always", "session_critical_pct": 0},
        {"label": "Pro", "session": "always", "session_critical_pct": 101},
        {"label": "Pro " + EM_DASH + " legacy", "session": "always"},
        {"label": "", "session": "always"},
        {"label": "Pro", "session": "always", "sources": ["http://x.example"]},
        {"label": "Pro", "session": "always", "sources": "https://x.example"},
        {"label": "Pro", "session": "always", "verified_at": "yesterday"},
        {"label": "Pro", "session": "always", "session_note": "a" * 400},
        "not an object",
    ])
    def test_invalid_subscription_is_skipped_with_a_warning(self, tmp_path, caplog, bad):
        root = _write(tmp_path, {
            "version": 1,
            "providers": {"claude": {"subscriptions": {
                "pro": bad,
                "team": {"label": "Team", "session": "always"},
            }}},
        })
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        subs = cat.providers["claude"].subscriptions
        assert "pro" not in subs
        assert "team" in subs  # one bad entry never takes the rest down
        assert any("'pro'" in r.getMessage() for r in caplog.records)

    def test_invalid_tier_is_skipped_but_subscription_kept(self, tmp_path, caplog):
        root = _write(tmp_path, {
            "version": 1,
            "providers": {"claude": {"subscriptions": {"max": {
                "label": "Max", "session": "reported",
                "tiers": {
                    "default_claude_max_20x": {"label": "Ultra", "session": "until_verified"},
                    "default_claude_max_5x": {"id": "max", "label": "Max", "session": "always"},
                },
            }}}},
        })
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        tiers = cat.providers["claude"].subscriptions["max"].tiers
        assert "default_claude_max_20x" not in tiers  # no id
        assert "default_claude_max_5x" in tiers
        assert any("default_claude_max_20x" in r.getMessage() for r in caplog.records)

    def test_bad_window_length_falls_back(self, tmp_path, caplog):
        root = _write(tmp_path, {
            "version": 1,
            "providers": {"claude": {"windows": {"session": -5, "weekly": "x", "weekly_model": 3600}}},
        })
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        windows = cat.providers["claude"].windows
        assert windows["session"] == 5 * 3600
        assert windows["weekly"] == 7 * 86400
        assert windows["weekly_model"] == 3600

    def test_tiers_inherit_from_their_subscription(self, tmp_path):
        root = _write(tmp_path, {
            "version": 1,
            "providers": {"claude": {
                "default": {"session": "reported", "session_critical_pct": 70},
                "subscriptions": {"max": {
                    "label": "Max", "session": "always", "session_critical_pct": 91,
                    "verified_at": "2026-11-01", "sources": ["https://example.com/a"],
                    "tiers": {"t9": {"id": "nine", "label": "Nine"}},
                }},
            }},
        })
        cat = up.load_catalog(root)
        nine = cat.providers["claude"].subscriptions["max"].tiers["t9"]
        assert nine.session == "always"
        assert nine.session_critical_pct == 91
        assert nine.verified_at == "2026-11-01"
        assert nine.sources == ("https://example.com/a",)

    def test_resolution_uses_builtin_defaults_without_a_catalog(self, tmp_path, monkeypatch):
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(tmp_path))
        plan = up.resolve_plan(_ultra())
        assert plan.id is None
        assert plan.session == "reported"
        assert plan.session_critical_pct == 80
        assert plan.label == "Max"
        # Silence from an authoritative payload still reads as "no window".
        assert up.session_window(_ultra(), ULTRA_RAW).state == "absent"
        assert up.session_window(_max5(), SESSION_RAW).state == "present"


# --------------------------------------------------------------------------- #
# Plan resolution
# --------------------------------------------------------------------------- #


class TestResolvePlan:
    def test_ultra(self):
        plan = up.resolve_plan(_ultra())
        assert (plan.id, plan.label, plan.formerly) == ("ultra", "Ultra", "Max 20x")
        assert plan.session == "until_verified"
        assert plan.session_critical_pct == 95
        assert plan.label_source == "catalog"
        assert plan.drift == ()

    def test_provider_name_wins(self):
        profile = _profile("Ultra")
        plan = up.resolve_plan(_ultra(profile_json=profile))
        assert plan.label == "Ultra"
        assert plan.label_source == "provider"
        assert plan.drift == ()

    def test_provider_rename_is_drift(self, caplog):
        profile = _profile("Ultra Plus")
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            plan = up.resolve_plan(_ultra(profile_json=profile))
            up.resolve_plan(_ultra(profile_json=profile))
        assert plan.label == "Ultra Plus"
        assert plan.drift == ("renamed",)
        drift_logs = [r for r in caplog.records if "drift" in r.getMessage()]
        assert len(drift_logs) == 1  # once per shape per process

    def test_provider_name_is_sanitized(self):
        long_name = "U" * 200
        profile = _profile(long_name)
        assert up.resolve_plan(_ultra(profile_json=profile)).label_source == "catalog"
        profile = _profile("  ")
        assert up.resolve_plan(_ultra(profile_json=profile)).label == "Ultra"
        assert up.resolve_plan(_ultra(profile_json="not json")).label == "Ultra"
        assert up.resolve_plan(_ultra(profile_json={"organization": "x"})).label == "Ultra"

    def test_max(self):
        plan = up.resolve_plan(_max5())
        assert (plan.id, plan.label, plan.formerly) == ("max", "Max", "Max 5x")
        assert plan.session == "always"
        assert plan.session_critical_pct == 90

    def test_unknown_max_tier_is_drift(self):
        plan = up.resolve_plan({"subscription_type": "max",
                                "rate_limit_tier": "default_claude_max_40x"})
        assert plan.label == "Max"
        assert plan.session == "reported"
        assert plan.session_critical_pct == 90
        assert plan.drift == ("unknown_tier",)

    def test_max_without_tier(self):
        plan = up.resolve_plan({"subscription_type": "max", "rate_limit_tier": None})
        assert plan.label == "Max"
        assert plan.session_critical_pct == 90
        assert plan.drift == ()

    def test_team_ignores_its_tier(self):
        plan = up.resolve_plan({"subscription_type": "team",
                                "rate_limit_tier": "default_claude_max_5x"})
        assert (plan.id, plan.label, plan.session) == ("team", "Team", "always")
        assert plan.session_critical_pct == 90
        assert plan.drift == ()

    @pytest.mark.parametrize("sub,label,policy,crit", [
        ("pro", "Pro", "always", 80),
        ("free", "Free", "always", 80),
        ("enterprise", "Enterprise", "reported", 80),
        ("PRO", "Pro", "always", 80),
    ])
    def test_subscription_plans(self, sub, label, policy, crit):
        plan = up.resolve_plan({"subscription_type": sub})
        assert (plan.label, plan.session, plan.session_critical_pct) == (label, policy, crit)

    def test_unknown_subscription_uses_the_raw_name(self):
        plan = up.resolve_plan({"subscription_type": "galaxy_brain"})
        assert plan.id is None
        assert plan.label == "Galaxy Brain"
        assert plan.label_source == "raw"
        assert plan.session == "reported"
        assert plan.session_critical_pct == 80

    def test_nothing_known(self):
        plan = up.resolve_plan({})
        assert plan.id is None and plan.label is None
        assert plan.session_critical_pct == 80

    def test_tier_without_subscription(self):
        assert up.resolve_plan({"rate_limit_tier": "default_claude_max_20x"}).id == "ultra"
        assert up.resolve_plan({"rate_limit_tier": "default_claude_max_5x"}).id == "max"
        assert up.resolve_plan({"rate_limit_tier": "mystery"}).session_critical_pct == 80

    def test_codex_plans(self):
        pro = up.resolve_plan({"provider": "codex", "subscription_type": "pro"})
        assert (pro.label, pro.session) == ("Pro", "never")
        plus = up.resolve_plan({"provider": "codex", "subscription_type": "plus"})
        assert (plus.label, plus.session) == ("Plus", "always")
        biz = up.resolve_plan({"provider": "codex", "subscription_type": "business"})
        assert (biz.id, biz.label, biz.session) == (None, "Business", "reported")
        # A Codex row never takes a label from a Claude profile.
        profile = json.dumps({"organization": {"plan_display_name": "Ultra"}})
        assert up.resolve_plan({"provider": "codex", "subscription_type": "pro",
                                "profile_json": profile}).label == "Pro"


# --------------------------------------------------------------------------- #
# Session window resolution
# --------------------------------------------------------------------------- #


class TestSessionWindowClaude:
    def test_verified_ultra_has_no_session(self):
        sw = up.session_window(_ultra(), ULTRA_RAW)
        assert sw.state == "absent"
        assert sw.temporary is False
        assert "no 5-hour limit" in sw.note
        assert sw.seconds == 5 * 3600
        assert sw.drift == ()

    def test_unverified_ultra_has_a_temporary_session(self):
        sw = up.session_window(_ultra(), SESSION_RAW)
        assert sw.state == "present"
        assert sw.temporary is True
        assert "temporary" in sw.note

    def test_ultra_without_a_payload_is_unknown(self):
        # No payload and no cached reading: jacked cannot rule a window out.
        assert up.session_window(_ultra(), None).state == "unknown"
        assert up.has_session(_ultra(), None) is True

    def test_partial_ultra_read_keeps_a_cached_window(self):
        # A temporary 5-hour window at 99% must survive a payload that
        # omits five_hour and limits until an authoritative read clears it.
        row = _ultra(cached_usage_5h=99.0, cached_5h_resets_at="2026-11-01T20:00:00+00:00")
        sw = up.session_window(row, {"seven_day": {"utilization": 40.0}})
        assert (sw.state, sw.temporary) == ("present", True)
        assert up.account_view(row, {"seven_day": {}}).five_hour_reading() == 99.0
        # An authoritative read still wins over the cache.
        assert up.session_window(row, ULTRA_RAW).state == "absent"

    def test_unknown_limit_kind_is_never_proof_of_absence(self, caplog):
        raw = {"five_hour": None, "limits": [
            {"kind": "weekly_all"}, {"kind": "rolling_session"}]}
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            sw = up.session_window(_ultra(), raw)
            up.session_window(_ultra(), raw)
        assert sw.state == "unknown"
        assert sw.drift == ("unknown_limit_kind",)
        logs = [r.getMessage() for r in caplog.records if "drift" in r.getMessage()]
        assert len(logs) == 1 and "rolling_session" in logs[0]
        # A plan with a fixed window keeps it.
        assert up.session_window(_max5(), raw).state == "present"

    def test_raw_is_read_from_the_row(self):
        row = _ultra(cached_usage_raw=json.dumps(ULTRA_RAW))
        assert up.session_window(row).state == "absent"
        row = _ultra(cached_usage_raw=json.dumps(SESSION_RAW))
        assert up.session_window(row).state == "present"
        row = _ultra(cached_usage_raw="{broken")
        assert up.session_window(row).state == "unknown"

    def test_max_with_a_session(self):
        sw = up.session_window(_max5(), SESSION_RAW)
        assert (sw.state, sw.temporary, sw.drift) == ("present", False, ())

    def test_max_silence_reads_idle_but_flags_drift(self, caplog):
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            sw = up.session_window(_max5(), WEEKLY_ONLY_RAW)
            up.session_window(_max5(), WEEKLY_ONLY_RAW)
        assert sw.state == "present"
        assert sw.drift == ("session_missing",)
        assert len([r for r in caplog.records if "drift" in r.getMessage()]) == 1

    def test_max_without_a_payload_is_present(self):
        sw = up.session_window(_max5(), None)
        assert (sw.state, sw.drift) == ("present", ())

    def test_legacy_null_five_hour_without_limits_is_not_authoritative(self):
        # Older payloads had no limits list; null meant "no window open".
        sw = up.session_window(_max5(), {"five_hour": None})
        assert (sw.state, sw.drift) == ("present", ())

    def test_reported_plan_follows_an_authoritative_payload(self):
        row = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_40x"}
        assert up.session_window(row, WEEKLY_ONLY_RAW).state == "absent"
        assert up.session_window(row, SESSION_RAW).state == "present"
        # Silence without a limits list keeps the legacy idle reading.
        assert up.session_window(row, {"five_hour": None}).state == "present"

    def test_five_hour_object_counts_as_reported(self):
        row = {"subscription_type": "enterprise"}
        raw = {"five_hour": {"utilization": 3.0, "resets_at": None}, "limits": []}
        assert up.session_window(row, raw).state == "present"
        raw = {"five_hour": {"utilization": None, "resets_at": None}, "limits": []}
        assert up.session_window(row, raw).state == "absent"

    def test_never_plan_with_a_reported_session_follows_the_payload(self, tmp_path, monkeypatch):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "pro": {"label": "Pro", "session": "never"}}}}})
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(root))
        sw = up.session_window({"subscription_type": "pro"}, SESSION_RAW)
        assert (sw.state, sw.drift) == ("present", ("session_on_never_plan",))
        assert up.session_window({"subscription_type": "pro"}, WEEKLY_ONLY_RAW).state == "absent"


class TestSessionWindowCodex:
    def test_pro_has_no_session(self):
        sw = up.session_window({"provider": "codex", "subscription_type": "pro"}, CODEX_PRO_RAW)
        assert sw.state == "absent"
        assert "ChatGPT Pro" in sw.note
        assert sw.drift == ()

    def test_pro_without_a_payload_is_absent(self):
        row = {"provider": "codex", "subscription_type": "pro"}
        assert up.session_window(row, None).state == "absent"

    def test_pro_reporting_a_session_follows_the_payload(self):
        row = {"provider": "codex", "subscription_type": "pro"}
        sw = up.session_window(row, CODEX_PLUS_RAW)
        assert (sw.state, sw.drift) == ("present", ("session_on_never_plan",))

    def test_plus_with_a_session(self):
        row = {"provider": "codex", "subscription_type": "plus"}
        assert up.session_window(row, CODEX_PLUS_RAW).state == "present"

    def test_plus_weekly_only_is_absent_with_drift(self):
        row = {"provider": "codex", "subscription_type": "plus"}
        sw = up.session_window(row, CODEX_PRO_RAW)
        assert (sw.state, sw.drift) == ("absent", ("session_missing",))

    def test_partial_payload_is_unknown(self):
        row = {"provider": "codex", "subscription_type": "business"}
        partial = {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 10080}}}
        assert up.session_window(row, partial).state == "unknown"
        assert up.session_window(row, None).state == "unknown"

    def test_business_weekly_only_is_absent(self):
        row = {"provider": "codex", "subscription_type": "business"}
        sw = up.session_window(row, CODEX_PRO_RAW)
        assert (sw.state, sw.drift) == ("absent", ())


# --------------------------------------------------------------------------- #
# Model windows and payloads
# --------------------------------------------------------------------------- #


class TestModelWindowSeconds:
    def test_claude_scoped_limit_is_weekly(self):
        assert up.model_window_seconds("claude") == 7 * 86400

    def test_codex_duration_wins(self):
        assert up.model_window_seconds("codex", {"windowDurationMins": 300}, "weekly") == 5 * 3600
        assert up.model_window_seconds("codex", {"windowDurationMins": 10080}, "session") == 7 * 86400

    @pytest.mark.parametrize("window", [None, {}, {"windowDurationMins": True},
                                        {"windowDurationMins": 0}, {"windowDurationMins": "300"},
                                        {"windowDurationMins": float("nan")}])
    def test_codex_falls_back_to_the_slot(self, window):
        assert up.model_window_seconds("codex", window, "session") == 5 * 3600
        assert up.model_window_seconds("codex", window, "weekly") == 7 * 86400


class TestAccountView:
    def test_ultra_payloads(self):
        profile = _profile("Ultra")
        row = _ultra(profile_json=profile, cached_usage_raw=json.dumps(ULTRA_RAW),
                     cached_usage_5h=None, cached_usage_7d=33.0)
        view = up.account_view(row)
        assert view.plan_payload() == {
            "id": "ultra", "label": "Ultra", "formerly": "Max 20x",
            "label_source": "provider", "drift": [], "catalog_verified_at": "2026-11-01",
        }
        windows = view.windows_payload()
        assert windows["five_hour"]["state"] == "absent"
        assert windows["five_hour"]["seconds"] == 5 * 3600
        assert windows["five_hour"]["temporary"] is False
        assert windows["five_hour"]["note"].startswith("Verified Ultra")
        assert windows["seven_day"] == {"state": "present", "temporary": False,
                                        "seconds": 7 * 86400, "note": None}

    def test_drift_from_both_sources_is_merged(self):
        row = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_5x",
               "cached_usage_raw": json.dumps(WEEKLY_ONLY_RAW)}
        assert up.account_view(row).plan_payload()["drift"] == ["session_missing"]

    def test_five_hour_reading(self):
        # Absent: no reading, even if a stale value is still cached.
        ultra = _ultra(cached_usage_raw=json.dumps(ULTRA_RAW), cached_usage_5h=67.0)
        assert up.account_view(ultra).five_hour_reading() is None
        # Present Claude window with no reading: no window open, 0% used.
        idle = _max5(cached_usage_raw=json.dumps(SESSION_RAW), cached_usage_5h=None)
        assert up.account_view(idle).five_hour_reading() == 0.0
        busy = _max5(cached_usage_raw=json.dumps(SESSION_RAW), cached_usage_5h=41.5)
        assert up.account_view(busy).five_hour_reading() == 41.5
        # Codex never turns a missing reading into 0%.
        plus = {"provider": "codex", "subscription_type": "plus",
                "cached_usage_raw": json.dumps(CODEX_PLUS_RAW), "cached_usage_5h": None}
        assert up.account_view(plus).five_hour_reading() is None
        partial = {"provider": "codex", "subscription_type": "business", "cached_usage_5h": None}
        assert up.account_view(partial).five_hour_reading() is None

    def test_codex_weekly_state(self):
        pro = {"provider": "codex", "subscription_type": "pro",
               "cached_usage_raw": json.dumps(CODEX_PRO_RAW)}
        assert up.account_view(pro).windows_payload()["seven_day"]["state"] == "present"
        partial = {"provider": "codex", "subscription_type": "pro",
                   "cached_usage_raw": json.dumps(
                       {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 300}}})}
        assert up.account_view(partial).windows_payload()["seven_day"]["state"] == "unknown"

    def test_has_session(self):
        assert up.has_session(_ultra(cached_usage_raw=json.dumps(ULTRA_RAW))) is False
        assert up.has_session(_max5()) is True
        assert up.has_session({"provider": "codex", "subscription_type": "business"}) is True

    def test_raw_usage_tolerates_garbage(self):
        assert up.raw_usage({}) is None
        assert up.raw_usage({"cached_usage_raw": "[1]"}) is None
        assert up.raw_usage({"cached_usage_raw": "nope"}) is None
        assert up.raw_usage({"cached_usage_raw": {"a": 1}}) == {"a": 1}
        assert up.raw_usage({"cached_usage_raw": '{"a": 1}'}) == {"a": 1}


# --------------------------------------------------------------------------- #
# Window lengths per plan and as observed
# --------------------------------------------------------------------------- #


class TestWindowLengths:
    def test_plan_can_override_its_provider_lengths(self, tmp_path, monkeypatch):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {
            "windows": {"session": 18000, "weekly": 604800},
            "subscriptions": {
                "pro": {"label": "Pro", "session": "always", "windows": {"session": 10800}},
                "team": {"label": "Team", "session": "always"},
            },
        }}})
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(root))
        pro = up.account_view({"subscription_type": "pro"}, None)
        assert pro.session.seconds == 10800
        assert pro.windows_payload()["five_hour"]["seconds"] == 10800
        assert pro.windows_payload()["seven_day"]["seconds"] == 604800
        team = up.account_view({"subscription_type": "team"}, None)
        assert team.session.seconds == 18000
        assert up.resolve_plan({"subscription_type": "pro"}).window_seconds("session") == 10800

    def test_unknown_window_kind_is_ignored_not_fatal(self, tmp_path, caplog):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "pro": {"label": "Pro", "session": "always",
                    "windows": {"monthly": 100, "session": 7200}}}}}})
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        pro = cat.providers["claude"].subscriptions["pro"].plan
        assert dict(pro.windows)["session"] == 7200
        assert "monthly" not in dict(pro.windows)
        assert any("monthly" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("windows", [{"session": 0}, {"session": True}, "x"])
    def test_invalid_plan_lengths_skip_the_entry(self, tmp_path, caplog, windows):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "pro": {"label": "Pro", "session": "always", "windows": windows}}}}})
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            cat = up.load_catalog(root)
        assert "pro" not in cat.providers["claude"].subscriptions

    def test_codex_observed_lengths_win(self):
        raw = {"rateLimits": {
            "primary": {"usedPercent": 1, "windowDurationMins": 240, "resetsAt": 1},
            "secondary": {"usedPercent": 2, "windowDurationMins": 20160, "resetsAt": 2},
        }}
        view = up.account_view({"provider": "codex", "subscription_type": "plus"}, raw)
        assert view.session.seconds == 240 * 60
        assert view.windows_payload()["seven_day"]["seconds"] == 20160 * 60

    def test_codex_without_durations_uses_the_catalog(self):
        view = up.account_view({"provider": "codex", "subscription_type": "pro"}, None)
        assert view.session.seconds == 5 * 3600
        assert view.windows_payload()["seven_day"]["seconds"] == 7 * 86400


class TestProjectRow:
    def test_absent_window_drops_stale_values(self):
        row = _ultra(cached_usage_raw=json.dumps(ULTRA_RAW), cached_usage_5h=100.0,
                     cached_5h_resets_at="2026-11-01T20:00:00+00:00", cached_usage_7d=40.0)
        projected = up.project_row(row)
        assert projected["cached_usage_5h"] is None
        assert projected["cached_5h_resets_at"] is None
        assert projected["cached_usage_7d"] == 40.0
        assert row["cached_usage_5h"] == 100.0  # the caller's row is not mutated

    def test_present_window_is_untouched(self):
        row = _max5(cached_usage_raw=json.dumps(SESSION_RAW), cached_usage_5h=67.0)
        assert up.project_row(row) is row

    def test_absent_window_with_nothing_cached_is_untouched(self):
        row = _ultra(cached_usage_raw=json.dumps(ULTRA_RAW), cached_usage_5h=None)
        assert up.project_row(row) is row

    def test_unknown_window_keeps_its_values(self):
        row = _ultra(cached_usage_5h=55.0)  # no payload: prior evidence keeps it
        assert up.project_row(row)["cached_usage_5h"] == 55.0


# --------------------------------------------------------------------------- #
# Plan-review cycle 2 regressions
# --------------------------------------------------------------------------- #


class TestCycleTwoRegressions:
    @pytest.mark.parametrize("kind", [{"name": "session"}, [], 7, None])
    def test_non_string_limit_kind_is_drift_not_a_crash(self, kind):
        raw = {"five_hour": None, "limits": [{"kind": "weekly_all"}, {"kind": kind}]}
        sw = up.session_window(_ultra(), raw)
        assert sw.state == "unknown"
        assert sw.drift == ("unknown_limit_kind",)

    def test_non_dict_limit_entry_is_drift(self):
        raw = {"five_hour": None, "limits": ["session"]}
        assert up.session_window(_ultra(), raw).drift == ("unknown_limit_kind",)

    def test_model_window_follows_a_plan_override(self, tmp_path, monkeypatch):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "pro": {"label": "Pro", "session": "always", "windows": {"weekly_model": 3600}}}}}})
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(root))
        plan = up.resolve_plan({"subscription_type": "pro"})
        assert up.model_window_seconds("claude", None, "weekly_model", plan) == 3600
        assert up.model_window_seconds("claude") == 7 * 86400  # provider length
        # An observed Codex length still wins over the plan.
        assert up.model_window_seconds("codex", {"windowDurationMins": 60}, "weekly", plan) == 3600

    def test_codex_dropped_window_never_lends_its_length(self):
        # primary has no usable duration (positional -> session slot);
        # secondary also classifies as session and is DROPPED by the
        # canonical classifier, so its 240 minutes must not be used.
        raw = {"rateLimits": {
            "primary": {"usedPercent": 30, "resetsAt": 1793780000},
            "secondary": {"usedPercent": 9, "windowDurationMins": 240, "resetsAt": 1793790000},
        }}
        view = up.account_view({"provider": "codex", "subscription_type": "plus"}, raw)
        assert view.session.seconds == 5 * 3600

    def test_codex_normalizer_exposes_window_minutes(self):
        from jacked.codex.usage import normalize_rate_limits

        norm = normalize_rate_limits(CODEX_PLUS_RAW)
        assert norm["five_hour"]["window_minutes"] == 300
        assert norm["seven_day"]["window_minutes"] == 10080
        norm = normalize_rate_limits(CODEX_PRO_RAW)
        assert norm["five_hour"]["window_minutes"] is None
        assert norm["seven_day"]["window_minutes"] == 10080


class TestPerModelWindowSeconds:
    def test_claude_scoped_and_legacy_limits(self):
        from jacked.service.menubar_summary import binding_model_compact, parse_per_model

        models = parse_per_model(ULTRA_RAW)
        assert [(m["label"], m["window_seconds"]) for m in models] == [("Fable", 7 * 86400)]
        legacy = parse_per_model({"seven_day_opus": {"utilization": 4, "resets_at": None}})
        assert legacy[0]["window_seconds"] == 7 * 86400
        compact = binding_model_compact(ULTRA_RAW)
        assert compact["window_seconds"] == 7 * 86400
        assert compact["is_active"] is True

    def test_codex_named_limits(self):
        from jacked.service.menubar_summary import parse_per_model

        raw = {"rateLimitsByLimitId": {
            "weekly": {"limitName": "Spark weekly",
                       "secondary": {"usedPercent": 5, "windowDurationMins": 10080}},
            "short": {"limitName": "Spark short", "primary": {"usedPercent": 7}},
        }}
        lengths = {m["label"]: m["window_seconds"] for m in parse_per_model(raw)}
        assert lengths == {"Spark weekly": 7 * 86400, "Spark short": 5 * 3600}

    def test_plan_override_reaches_the_model_bars(self, tmp_path, monkeypatch):
        from jacked.service.menubar_summary import parse_per_model

        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "max": {"label": "Max", "session": "reported", "windows": {"weekly_model": 86400}}}}}})
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(root))
        plan = up.resolve_plan({"subscription_type": "max"})
        assert parse_per_model(ULTRA_RAW, plan)[0]["window_seconds"] == 86400


class TestCodexNonObjectWindows:
    @pytest.mark.parametrize("junk", ["10080", [1, 2], 7, True, [], 0, "", False])
    def test_non_object_window_is_ignored_not_a_crash(self, junk):
        from jacked.codex.usage import normalize_rate_limits

        raw = {"rateLimits": {"primary": junk, "secondary": None}}
        norm = normalize_rate_limits(raw)
        assert norm["five_hour"]["reported"] is False
        assert norm["windows_complete"] is False  # never authoritative
        row = {"provider": "codex", "subscription_type": "plus"}
        assert up.account_view(row, raw).session.state == "unknown"
        assert up.has_session(row, raw) is True
        # The secondary slot is guarded the same way.
        raw = {"rateLimits": {"primary": {"usedPercent": 3, "windowDurationMins": 300},
                              "secondary": junk}}
        norm = normalize_rate_limits(raw)
        assert norm["five_hour"]["reported"] is True
        assert norm["windows_complete"] is False

    @pytest.mark.parametrize("junk", [[1], "x", 5])
    def test_non_object_rate_limits_is_ignored(self, junk):
        from jacked.codex.usage import normalize_rate_limits

        norm = normalize_rate_limits({"rateLimits": junk})
        assert norm["five_hour"]["reported"] is False
        assert norm["windows_complete"] is False
        assert normalize_rate_limits(None)["windows_complete"] is False


    def test_falsey_junk_window_never_clears_a_cached_session(self):
        """A falsey non-object window ([], 0, "") is not absence: the
        payload is not authoritative, so the cached 5h reading stays."""
        from jacked.codex.usage import normalize_rate_limits

        raw = {"rateLimits": {"primary": [], "secondary": {
            "usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1793790000}}}
        norm = normalize_rate_limits(raw)
        assert norm["windows_complete"] is False
        assert norm["seven_day"]["reported"] is True
        # An empty object still means "no window here".
        raw = {"rateLimits": {"primary": {}, "secondary": {
            "usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1793790000}}}
        assert normalize_rate_limits(raw)["windows_complete"] is True


# --------------------------------------------------------------------------- #
# dcr wave 1 security findings
# --------------------------------------------------------------------------- #


class TestSecurityFindings:
    def test_plan_label_ignores_a_stored_profile_of_another_org(self, caplog):
        """M1: a re-auth moved the row to org B and kept org A's profile.
        Org A's plan name must not label the row, and no false drift."""
        foreign = json.dumps({"account": {"email": "jack@x.com"},
                              "organization": {"uuid": "org-A", "plan_display_name": "Enterprise"}})
        row = {"id": 9, "email": "jack@x.com", "organization_uuid": "org-B",
               "subscription_type": "team", "profile_json": foreign}
        with caplog.at_level(logging.INFO, logger="jacked.usage_plans"):
            plan = up.resolve_plan(row)
            up.resolve_plan(row)
        assert (plan.label, plan.label_source, plan.drift) == ("Team", "catalog", ())
        owner_logs = [r for r in caplog.records if "another owner" in r.getMessage()]
        assert len(owner_logs) == 1  # once per account and field
        owned = dict(row, profile_json=_profile("Team Plus", "jack@x.com", "org-B"))
        assert up.resolve_plan(owned).label == "Team Plus"
        assert up.resolve_plan(owned).label_source == "provider"
        # A profile with no account object has no owner: never trusted.
        ownerless = dict(row, organization_uuid="",
                         profile_json=json.dumps({"organization": {"plan_display_name": "X"}}))
        assert up.resolve_plan(ownerless).label == "Team"

    def test_another_email_is_another_owner(self):
        row = _ultra(email="me@x.com", profile_json=_profile("Ultra Plus", "other@x.com"))
        assert up.resolve_plan(row).label == "Ultra"

    @pytest.mark.parametrize("name", ["Max\u202eartlU", "A\x9bB", "Ultra\x7f", "Ul\u200btra"])
    def test_non_printable_plan_names_are_rejected(self, name):
        """L1: control, C1, bidi and zero-width characters never label a plan."""
        assert up.resolve_plan(_ultra(profile_json=_profile(name))).label == "Ultra"

    def test_model_labels_are_cleaned(self):
        from jacked.service.menubar_summary import parse_per_model

        raw = {"limits": [{"kind": "weekly_scoped", "percent": 5,
                           "scope": {"model": {"display_name": "Fable\x1b]0;owned\x1b\\x"}}}],
               "rateLimitsByLimitId": {"k": {"limitName": "Spark\x1b[2J",
                                             "secondary": {"usedPercent": 1}}}}
        labels = sorted(m["label"] for m in parse_per_model(raw))
        assert all("\x1b" not in label for label in labels)
        assert labels == ["Fable ]0;owned \\x", "Spark [2J"]

    @pytest.mark.parametrize("mins", [1.7976931348623157e308, float("inf"), 2**63 - 1, 10**9])
    def test_absurd_window_lengths_fall_back(self, mins):
        """L2: an "unlimited" sentinel or an overflowing length is not a window."""
        assert up.model_window_seconds("codex", {"windowDurationMins": mins}, "weekly") == 7 * 86400
        raw = {"rateLimits": {"primary": {"usedPercent": 1, "windowDurationMins": 300},
                              "secondary": {"usedPercent": 2, "windowDurationMins": mins}}}
        view = up.account_view({"provider": "codex", "subscription_type": "plus"}, raw)
        assert view.weekly_seconds == 7 * 86400

    def test_catalog_window_lengths_are_bounded(self, tmp_path):
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {
            "windows": {"weekly": up.MAX_WINDOW_SECONDS + 1},
            "subscriptions": {"pro": {"label": "Pro", "windows": {"session": 10**12}}}}}})
        cat = up.load_catalog(root)
        assert cat.providers["claude"].windows["weekly"] == 7 * 86400
        assert "pro" not in cat.providers["claude"].subscriptions

    def test_unknown_limit_kinds_never_forge_log_lines(self, caplog):
        """L3: a provider string in a drift WARNING is escaped."""
        raw = {"limits": [{"kind": "x\nFORGED: everything is fine"}]}
        with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
            up.session_window(_ultra(), raw)
        (record,) = [r for r in caplog.records if "drift" in r.getMessage()]
        assert "\n" not in record.getMessage()
        assert "\\n" in record.getMessage()

    def test_the_drift_log_is_bounded(self, monkeypatch):
        monkeypatch.setattr(up, "_DRIFT_LOG_MAX", 3)
        for i in range(10):
            up.session_window(_ultra(), {"limits": [{"kind": f"k{i}"}]})
        assert len(up._drift_logged) == 3

    def test_account_view_repr_hides_the_row(self):
        """L4: the row holds tokens; a view's repr must never print them."""
        row = _ultra(access_token="sk-ant-oat-SECRET", cc_refresh_token="rt-SECRET")
        assert "SECRET" not in repr(up.account_view(row, None))

    @pytest.mark.parametrize("tier", [{"id": "U" * 200, "label": "X"}, {"id": "bad id!", "label": "X"}])
    def test_catalog_ids_are_bounded(self, tmp_path, tier):
        """L5: a tier id reaches the API and the CLI: [a-z0-9_]{1,40} only."""
        root = _write(tmp_path, {"version": 1, "providers": {"claude": {"subscriptions": {
            "max": {"label": "Max", "tiers": {"t": tier}}, "Bad Key!": {"label": "Y"}}}}})
        subs = up.load_catalog(root).providers["claude"].subscriptions
        assert subs["max"].tiers == {}
        assert "bad key!" not in subs
