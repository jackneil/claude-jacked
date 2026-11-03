"""Tests for the auto-swap decision engine (pure functions, no I/O)."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from jacked import usage_plans
from jacked.web.auto_swap import (
    REASON_PREFIX_BURN_RATE,
    REASON_PREFIX_HIGHER_TIER,
    REASON_PREFIX_MODEL_WEEKLY,
    REASON_PREFIX_WEEKLY_CRITICAL,
    BurnRate,
    _resets_within,
    compute_7d_deficit,
    compute_effective_working_hours,
    format_account_label,
    has_viable_headroom,
    pick_best_target,
    plan_label,
    should_swap_now,
    tier_critical_threshold,
    update_burn_rate,
    weekly_critical_threshold,
)

# A verified Ultra payload (live shape, 2026-11-01): an authoritative limits
# list with no session entry, so the account has no 5-hour window.
ULTRA_RAW = json.dumps({
    "five_hour": None,
    "seven_day": {"utilization": 33.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 33,
         "resets_at": "2026-11-02T22:59:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 61,
         "resets_at": "2026-11-02T22:59:59+00:00", "is_active": True,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
})


def _write_catalog(root, providers: dict):
    """Write a test plan catalog under *root* and return the loaded Catalog."""
    (root / usage_plans.CATALOG_FILE).write_text(
        json.dumps({"version": 1, "verified_at": "2026-11-01", "providers": providers}),
        encoding="utf-8",
    )
    return usage_plans.load_catalog(root)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _acct(id, usage_5h=0, usage_7d=0, cc_token=True, active=True,
          failures=0, valid=True, auto_swap=True, resets_5h=None,
          rate_limit_tier=None, subscription_type="max", resets_7d=None):
    return {
        "id": id, "email": f"user{id}@test.com",
        "cached_usage_5h": usage_5h, "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_5h,
        "cached_7d_resets_at": resets_7d,
        "cc_access_token": "tok" if cc_token else None,
        "is_active": 1 if active else 0, "is_deleted": 0,
        "consecutive_failures": failures,
        "validation_status": "valid" if valid else "invalid",
        "auto_swap_enabled": 1 if auto_swap else 0,
        "priority": id - 1, "access_token": f"at_{id}",
        "rate_limit_tier": rate_limit_tier,
        "subscription_type": subscription_type,
    }


def _claude_raw(weekly_pct, weekly_resets, models=()):
    """A live-shape Claude usage payload: the weekly_all limit plus one
    weekly_scoped limit per (label, percent, resets_at, is_active) in models."""
    limits = [{"kind": "weekly_all", "group": "weekly", "percent": weekly_pct,
               "resets_at": weekly_resets, "scope": None, "is_active": False}]
    for label, pct, resets, is_active in models:
        limits.append({
            "kind": "weekly_scoped", "group": "weekly", "percent": pct,
            "resets_at": resets, "is_active": is_active,
            "scope": {"model": {"id": None, "display_name": label}, "surface": None},
        })
    return json.dumps({
        "five_hour": None,
        "seven_day": {"utilization": weekly_pct, "resets_at": weekly_resets},
        "limits": limits,
    })


def _claude_acct(id, *, usage_7d, resets_7d, models=(),
                 rate_limit_tier="default_claude_max_20x"):
    """A Claude row with its cached payload; the default tier is a verified
    Ultra (no 5-hour window, so the 5h value is None)."""
    row = _acct(id, usage_7d=usage_7d, resets_7d=resets_7d,
                rate_limit_tier=rate_limit_tier)
    row["cached_usage_5h"] = None
    row["cached_usage_raw"] = _claude_raw(usage_7d, resets_7d, models)
    return row


# ---------------------------------------------------------------------------
# update_burn_rate
# ---------------------------------------------------------------------------

class TestUpdateBurnRate:
    def test_burn_rate_first_tick_no_spike(self):
        rates: dict = {}
        br = update_burn_rate(rates, account_id=1, current_5h=45.0, current_7d=30.0)
        assert br.rate_5h_per_min == 0.0
        assert br.rate_7d_per_min == 0.0
        assert br.last_check_5h == 45.0
        assert br.last_check_7d == 30.0


# ---------------------------------------------------------------------------
# tier_critical_threshold
# ---------------------------------------------------------------------------

def test_tier_threshold_20x():
    assert tier_critical_threshold({"rate_limit_tier": "default_claude_max_20x"}) == 95.0

def test_tier_threshold_unlisted_max_tier(monkeypatch, caplog):
    """A Max tier the catalog does not list (a realistic row: the subscription
    type is set) reads the Max plan's 90%, and is logged as catalog drift."""
    monkeypatch.setattr(usage_plans, "_drift_logged", set())
    row = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_10x"}
    with caplog.at_level(logging.WARNING, logger="jacked.usage_plans"):
        assert tier_critical_threshold(row) == 90.0
    assert "unknown_tier" in caplog.text
    assert "default_claude_max_10x" in caplog.text

def test_tier_threshold_5x():
    assert tier_critical_threshold({"rate_limit_tier": "default_claude_max_5x"}) == 90.0

def test_tier_threshold_pro():
    assert tier_critical_threshold({"rate_limit_tier": "pro", "subscription_type": "pro"}) == 80.0

def test_tier_threshold_none_max_sub():
    """Max subscription with missing tier info gets conservative 90%."""
    assert tier_critical_threshold({"rate_limit_tier": None, "subscription_type": "max"}) == 90.0

def test_tier_threshold_unknown():
    """Unknown/missing everything falls to 80%."""
    assert tier_critical_threshold({}) == 80.0

def test_tier_threshold_team():
    assert tier_critical_threshold({"subscription_type": "team"}) == 90.0

def test_tier_threshold_codex_pro():
    assert tier_critical_threshold({"provider": "codex", "subscription_type": "pro"}) == 80.0

def test_tier_threshold_comes_from_the_catalog(tmp_path, monkeypatch):
    """No regex on the tier multiplier: the catalog value is the threshold,
    so a catalog edit (not a code change) moves it."""
    monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
        "claude": {"subscriptions": {"max": {
            "label": "Max", "session_critical_pct": 85,
            "tiers": {"default_claude_max_20x": {"id": "ultra", "label": "Ultra",
                                                 "session_critical_pct": 97}},
        }}},
    }))
    assert tier_critical_threshold({"rate_limit_tier": "default_claude_max_20x"}) == 97.0
    assert tier_critical_threshold(
        {"subscription_type": "max", "rate_limit_tier": "default_claude_max_5x"}
    ) == 85.0


# ---------------------------------------------------------------------------
# weekly_critical_threshold (#224i: leave only a walled account)
# ---------------------------------------------------------------------------

def test_weekly_threshold_ultra():
    assert weekly_critical_threshold(
        {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}
    ) == 98.0

def test_weekly_threshold_codex_pro():
    assert weekly_critical_threshold({"provider": "codex", "subscription_type": "pro"}) == 98.0

def test_weekly_threshold_unknown_plan_reads_the_default():
    assert weekly_critical_threshold({}) == 98.0

def test_weekly_threshold_comes_from_the_catalog(tmp_path, monkeypatch):
    """A catalog edit moves the weekly threshold for one plan only. It is a
    separate field from session_critical_pct (the 5h knob)."""
    monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
        "claude": {"subscriptions": {"max": {
            "label": "Max", "session_critical_pct": 85,
            "tiers": {
                "default_claude_max_20x": {"id": "ultra", "label": "Ultra",
                                           "weekly_critical_pct": 90},
                "default_claude_max_5x": {"id": "max", "label": "Max"},
            },
        }}},
    }))
    ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}
    max5 = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_5x"}
    assert weekly_critical_threshold(ultra) == 90.0
    assert weekly_critical_threshold(max5) == 98.0
    assert tier_critical_threshold(ultra) == 85.0

def test_walled_departure_names_are_in_the_facade():
    import jacked.web.auto_swap as auto_swap

    assert REASON_PREFIX_WEEKLY_CRITICAL == "weekly critical:"
    assert REASON_PREFIX_MODEL_WEEKLY == "model weekly critical:"
    for name in ("REASON_PREFIX_WEEKLY_CRITICAL", "REASON_PREFIX_MODEL_WEEKLY",
                 "weekly_critical_threshold"):
        assert name in auto_swap.__all__, name


# ---------------------------------------------------------------------------
# plan_label (replaces the regex-based tier_label)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, label", [
    ({"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}, "Ultra"),
    ({"subscription_type": "max", "rate_limit_tier": "default_claude_max_5x"}, "Max"),
    ({"subscription_type": "team"}, "Team"),
    ({}, ""),
])
def test_plan_label(row, label):
    assert plan_label(row) == label

def test_plan_label_uses_the_provider_plan_name():
    row = {"email": "u@x.com", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_20x",
           "profile_json": json.dumps({"account": {"email": "u@x.com"},
                                       "organization": {"plan_display_name": "Ultra Plus"}})}
    assert plan_label(row) == "Ultra Plus"

def test_plan_label_replaces_tier_label_in_the_facade():
    import jacked.web.auto_swap as auto_swap
    assert "plan_label" in auto_swap.__all__
    assert "tier_label" not in auto_swap.__all__
    assert not hasattr(auto_swap, "tier_label")


# ---------------------------------------------------------------------------
# _resets_within
# ---------------------------------------------------------------------------

class TestResetsWithin:
    def test_resets_in_5_min(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert _resets_within(future, 10) is True

    def test_resets_in_15_min(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        assert _resets_within(future, 10) is False

    def test_already_reset(self):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert _resets_within(past, 10) is False

    def test_none_returns_false(self):
        assert _resets_within(None, 10) is False

    def test_garbage_string_returns_false(self):
        assert _resets_within("not-a-date", 10) is False

    def test_z_suffix_parsed(self):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        assert _resets_within(future, 10) is True


# ---------------------------------------------------------------------------
# compute_effective_working_hours
# ---------------------------------------------------------------------------

class TestEffectiveWorkingHours:
    def test_same_day_within_active_hours(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 16, 0)
        end = datetime(2026, 4, 3, 21, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 5.0) < 0.01

    def test_overnight_skips_sleep(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 16, 0)
        end = datetime(2026, 4, 4, 10, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 9.0) < 0.01

    def test_multiple_days(self):
        from datetime import datetime
        start = datetime(2026, 4, 1, 7, 0)
        end = datetime(2026, 4, 4, 7, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 45.0) < 0.01

    def test_start_before_active_hours(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 5, 0)
        end = datetime(2026, 4, 3, 10, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 3.0) < 0.01

    def test_end_after_active_hours(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 20, 0)
        end = datetime(2026, 4, 3, 23, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 2.0) < 0.01

    def test_zero_when_entirely_outside_active(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 23, 0)
        end = datetime(2026, 4, 4, 5, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert result == 0.0

    def test_start_equals_end(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 12, 0)
        result = compute_effective_working_hours(start, start, "07:00", "22:00")
        assert result == 0.0


# ---------------------------------------------------------------------------
# compute_7d_deficit
# ---------------------------------------------------------------------------

class TestCompute7dDeficit:
    def test_account_behind_schedule(self):
        """Account at 20% usage, ~57% through the window = high deficit."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=3)).isoformat()
        acct = {
            "cached_usage_7d": 20.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert result["deficit"] > 25

    def test_account_ahead_of_schedule(self):
        """Account at 80% usage, ~29% through the window = negative deficit."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=5)).isoformat()
        acct = {
            "cached_usage_7d": 80.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert result["deficit"] < 0

    def test_none_when_no_resets_at(self):
        acct = {"cached_usage_7d": 50.0, "cached_7d_resets_at": None}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_none_when_no_usage(self):
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=3)).isoformat()
        acct = {"cached_usage_7d": None, "cached_7d_resets_at": resets_at}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_expired_window_returns_none(self):
        from datetime import datetime, timedelta
        resets_at = (datetime.now() - timedelta(days=1)).isoformat()
        acct = {"cached_usage_7d": 50.0, "cached_7d_resets_at": resets_at}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_includes_effective_hours_and_windows(self):
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=2)).isoformat()
        acct = {
            "cached_usage_7d": 30.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert "effective_hours_remaining" in result
        assert "effective_windows_remaining" in result
        assert "unused_7d" in result
        assert result["effective_hours_remaining"] > 0
        assert result["unused_7d"] == 70.0


# ---------------------------------------------------------------------------
# has_viable_headroom
# ---------------------------------------------------------------------------

class TestHasViableHeadroom:
    def test_plenty_of_headroom(self):
        """Account at 50% 7d has plenty of headroom."""
        acct = {"cached_usage_7d": 50.0}
        assert has_viable_headroom(acct) is True

    def test_near_exhaustion_rejected(self):
        """Account at 98% 7d has only 2% unused < 4.2% burn → rejected."""
        acct = {"cached_usage_7d": 98.0}
        assert has_viable_headroom(acct) is False

    def test_just_above_burn_threshold(self):
        """Account with unused just above burn_per_window is viable."""
        # burn_per_window ≈ 4.2017% with default 06:00-23:00
        # 95% 7d → 5% unused > 4.2017% → viable
        acct = {"cached_usage_7d": 95.0}
        assert has_viable_headroom(acct) is True

    def test_just_below_burn_threshold(self):
        """Account with unused < burn_per_window is not viable."""
        # 96% 7d → 4% unused < 4.2017% → not viable
        acct = {"cached_usage_7d": 96.0}
        assert has_viable_headroom(acct) is False

    def test_none_usage_treated_as_zero(self):
        """None usage = 0% used = 100% headroom → viable."""
        acct = {"cached_usage_7d": None}
        assert has_viable_headroom(acct) is True

    def test_narrow_active_hours_higher_burn(self):
        """Narrow active hours (09:00-17:00) = higher burn per window = stricter."""
        acct = {"cached_usage_7d": 93.0}
        assert has_viable_headroom(acct, active_start="09:00", active_end="17:00") is False

    def test_t0_with_deficit_bypasses_burn_floor(self):
        """T0 drains toward 100: 97% used (deficit 3 >= 1) is admitted even
        though unused 3% < burn_per_window — the floor would permanently
        strand the final slice of every account. From 98% the #224i weekly
        wall takes over (TestPickBestTargetWalledAdmission)."""
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=0, usage_7d=97,
                  resets_7d=_iso(now + timedelta(hours=2))),
        ]
        result = pick_best_target(
            accounts, current_id=1,
            active_start="06:00", active_end="23:00", now=now,
        )
        assert result is not None
        assert result["id"] == 2

    def test_t0_bypass_requires_deficit_of_at_least_1(self):
        """The 7d reset is 20 minutes away, so the weekly wall is suppressed
        and the deficit rule alone keeps the 99.5% account out."""
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=0, usage_7d=99.5,
                  resets_7d=_iso(now + timedelta(minutes=20))),
        ]
        result = pick_best_target(
            accounts, current_id=1,
            active_start="06:00", active_end="23:00", now=now,
        )
        assert result is None

    def test_non_t0_near_exhausted_still_excluded(self):
        """T1 with a positive deficit but unused < burn floor stays out."""
        expiry = _local_utc(2026, 5, 6, 3, 0)  # 03:00 local -> T1 target 100
        now = expiry - timedelta(hours=36)
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=0, usage_7d=96.5, resets_7d=_iso(expiry)),
        ]
        result = pick_best_target(
            accounts, current_id=1,
            active_start="06:00", active_end="23:00", now=now,
        )
        assert result is None


# ---------------------------------------------------------------------------
# format_account_label
# ---------------------------------------------------------------------------

class TestFormatAccountLabel:
    def test_personal_org(self):
        """Personal org (ends with 's Organization') shows as (personal)."""
        acct = {"email": "user3@example.com", "organization_name": "user3@example.com's Organization", "display_name": "User3"}
        assert format_account_label(acct) == "user3@example.com (personal)"

    def test_real_org(self):
        """Real org name is shown in parens."""
        acct = {"email": "user1@example.com", "organization_name": "Acme", "display_name": "User1"}
        assert format_account_label(acct) == "user1@example.com (Acme)"

    def test_custom_label_prepended(self):
        """User-set display_name that differs from default is prepended."""
        acct = {"email": "user1@example.com", "organization_name": "Acme", "display_name": "Acme Team"}
        assert format_account_label(acct) == "Acme Team - user1@example.com (Acme)"

    def test_default_display_name_not_shown(self):
        """Default display_name (just first name) is NOT prepended."""
        acct = {"email": "user1@example.com", "organization_name": "Acme", "display_name": "User1"}
        result = format_account_label(acct)
        assert not result.startswith("User1 -")
        assert result == "user1@example.com (Acme)"

    def test_no_org_name(self):
        """Missing org_name shows just email."""
        acct = {"email": "user@test.com", "organization_name": None, "display_name": None}
        assert format_account_label(acct) == "user@test.com"

    def test_empty_org_name(self):
        """Empty string org_name shows just email."""
        acct = {"email": "user@test.com", "organization_name": "", "display_name": None}
        assert format_account_label(acct) == "user@test.com"

    def test_no_display_name(self):
        """None display_name is fine — just email + org."""
        acct = {"email": "jack@test.com", "organization_name": "Acme Corp", "display_name": None}
        assert format_account_label(acct) == "jack@test.com (Acme Corp)"

    def test_display_name_empty_string(self):
        """Empty string display_name is treated as no label."""
        acct = {"email": "jack@test.com", "organization_name": "Acme Corp", "display_name": ""}
        assert format_account_label(acct) == "jack@test.com (Acme Corp)"


def _iso(dt: datetime) -> str:
    """Format datetime as ISO with Z suffix (matches Anthropic API)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_utc(*args) -> datetime:
    """UTC-aware instant for a naive LOCAL (system tz) wall-clock time.

    Deadline-aware T1 targets depend on the expiry's LOCAL calendar day,
    so tests must construct expiries in local terms to stay deterministic
    across machine timezones.
    """
    return datetime(*args).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# tier_for — deadline tier classification (T0-T3, 4=excluded)
# ---------------------------------------------------------------------------


class TestTierFor:
    def test_t0_under_24h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        assert tier_for(acct, now=now) == 0

    def test_t1_24_to_48h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=36)))
        assert tier_for(acct, now=now) == 1

    def test_t2_48h_to_4d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        assert tier_for(acct, now=now) == 2

    def test_t3_4d_to_7d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        assert tier_for(acct, now=now) == 3

    def test_excluded_when_expired(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert tier_for(acct, now=now) == 4

    def test_excluded_when_resets_at_missing(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert tier_for(acct, now=now) == 4

    def test_boundary_exactly_24h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=24)))
        assert tier_for(acct, now=now) == 1

    def test_boundary_exactly_48h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=48)))
        assert tier_for(acct, now=now) == 2

    def test_boundary_exactly_4d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=4)))
        assert tier_for(acct, now=now) == 3

    def test_hysteresis_dampens_t1_to_t0_jitter(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=59)))
        assert tier_for(acct, now=now, prev_tier=1) == 1

    def test_hysteresis_allows_clean_t1_to_t0_after_margin(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=54)))
        assert tier_for(acct, now=now, prev_tier=1) == 0

    def test_hysteresis_damps_single_step_less_urgent_flip(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # 2 min past the 24h boundary on the less-urgent side: refetch
        # noise on cached_7d_resets_at, not real movement — hold T0.
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=24, minutes=2)))
        assert tier_for(acct, now=now, prev_tier=0) == 0

    def test_hysteresis_releases_less_urgent_flip_after_margin(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=24, minutes=6)))
        assert tier_for(acct, now=now, prev_tier=0) == 1

    def test_multi_step_less_urgent_flip_is_immediate(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # T0 -> T2 is a genuine window reset, not jitter — flips at once
        # even within 5 min of the 48h boundary.
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=48, minutes=2)))
        assert tier_for(acct, now=now, prev_tier=0) == 2

    def test_hysteresis_no_prev_means_no_dampening(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=59)))
        assert tier_for(acct, now=now) == 0
        assert tier_for(acct, now=now, prev_tier=None) == 0


class TestWhiteBar:
    def test_one_day_left(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=1)))
        assert abs(white_bar(acct, now=now) - 6 / 7) < 1e-6

    def test_just_started(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=7)))
        assert white_bar(acct, now=now) == 0.0

    def test_about_to_expire(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=1)))
        assert abs(white_bar(acct, now=now) - 167 / 168) < 1e-6

    def test_overnight_advances(self):
        from jacked.web.auto_swap import white_bar
        resets_at = datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc)
        before = datetime(2026, 5, 7, 22, 0, tzinfo=timezone.utc)
        after = datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(resets_at))
        wb_before = white_bar(acct, now=before)
        wb_after = white_bar(acct, now=after)
        assert wb_after > wb_before
        assert abs((wb_after - wb_before) - 8 / 168) < 1e-6

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert white_bar(acct, now=now) is None

    def test_clamped_at_one_when_expired(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert white_bar(acct, now=now) == 1.0

    def test_weekly_length_comes_from_the_plan_catalog(self, tmp_path, monkeypatch):
        """The elapsed fraction uses the account's own weekly window length,
        the same ``windows.seven_day.seconds`` the dashboard marker reads."""
        from jacked.web.auto_swap import white_bar
        monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
            "claude": {"subscriptions": {
                "max": {"label": "Max", "windows": {"weekly": 14 * 86400}},
                "pro": {"label": "Pro"},
            }},
        }))
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        resets = _iso(now + timedelta(days=1))
        two_weeks = _acct(1, resets_7d=resets, subscription_type="max")
        one_week = _acct(2, resets_7d=resets, subscription_type="pro")
        assert abs(white_bar(two_weeks, now=now) - 13 / 14) < 1e-6
        assert abs(white_bar(one_week, now=now) - 6 / 7) < 1e-6
        # Parity with the JS elapsed marker: the same length reaches the payload.
        view = usage_plans.account_view(two_weeks)
        assert view.windows_payload()["seven_day"]["seconds"] == 14 * 86400

    def test_t3_target_follows_the_weekly_length(self, tmp_path, monkeypatch):
        from jacked.web.auto_swap import TIER_T3, target_for_tier
        monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
            "claude": {"subscriptions": {
                "max": {"label": "Max", "windows": {"weekly": 14 * 86400}},
            }},
        }))
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=5)))
        assert abs(target_for_tier(TIER_T3, acct, now) - 9 / 14 * 100) < 1e-6


class TestTarget7d:
    def test_t0_target_is_100(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        assert target_7d(acct, now=now) == 100.0

    def test_t1_target_floors_at_90(self):
        from jacked.web.auto_swap import target_7d
        # 23:00 local expiry: full 17h working day -> achievable burn is
        # capped at 10% -> floor target 90.
        expiry = _local_utc(2026, 5, 6, 23, 0)
        now = expiry - timedelta(hours=36)
        acct = _acct(1, resets_7d=_iso(expiry))
        assert target_7d(acct, now=now) == 90.0

    def test_t2_target_is_white_bar_plus_5(self):
        from jacked.web.auto_swap import target_7d, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_7d(acct, now=now) - (wb + 5.0)) < 1e-6

    def test_t2_target_capped_at_100(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=48, seconds=1)))
        result = target_7d(acct, now=now)
        assert result <= 100.0

    def test_t3_target_is_white_bar_exact(self):
        from jacked.web.auto_swap import target_7d, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_7d(acct, now=now) - wb) < 1e-6

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert target_7d(acct, now=now) is None

    def test_returns_none_when_expired(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert target_7d(acct, now=now) is None


class TestDeficitVsTarget:
    def test_t0_at_80_has_20_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_7d=80, resets_7d=_iso(now + timedelta(hours=12)))
        assert deficit_vs_target(acct, now=now) == 20.0

    def test_t1_at_70_has_20_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target
        expiry = _local_utc(2026, 5, 6, 23, 0)  # floor target 90
        now = expiry - timedelta(hours=36)
        acct = _acct(1, usage_7d=70, resets_7d=_iso(expiry))
        assert deficit_vs_target(acct, now=now) == 20.0

    def test_t2_at_white_bar_minus_3_has_8_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct_no_usage = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        wb_pct = white_bar(acct_no_usage, now=now) * 100
        acct = _acct(1, usage_7d=wb_pct - 3, resets_7d=_iso(now + timedelta(days=3)))
        assert abs(deficit_vs_target(acct, now=now) - 8.0) < 1e-6

    def test_t3_at_white_bar_has_zero_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct_no_usage = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        wb_pct = white_bar(acct_no_usage, now=now) * 100
        acct = _acct(1, usage_7d=wb_pct, resets_7d=_iso(now + timedelta(days=6)))
        assert abs(deficit_vs_target(acct, now=now)) < 1e-6

    def test_negative_deficit_when_above_target(self):
        from jacked.web.auto_swap import deficit_vs_target
        expiry = _local_utc(2026, 5, 6, 23, 0)  # floor target 90
        now = expiry - timedelta(hours=36)
        acct = _acct(1, usage_7d=95, resets_7d=_iso(expiry))
        assert deficit_vs_target(acct, now=now) == -5.0

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert deficit_vs_target(acct, now=now) is None

    def test_returns_none_when_usage_missing(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        acct["cached_usage_7d"] = None
        assert deficit_vs_target(acct, now=now) is None


class TestPickBestTargetTierStrict:
    """Spec scenarios C11-C16 — the headline behavior change."""

    def test_t0_with_room_beats_t3_with_room(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, usage_5h=50, usage_7d=50,
                       resets_7d=_iso(now + timedelta(days=2)))
        t0 = _acct(1, usage_5h=10, usage_7d=80,
                   resets_7d=_iso(now + timedelta(hours=12)))
        t3 = _acct(2, usage_5h=10, usage_7d=10,
                   resets_7d=_iso(now + timedelta(days=6)))
        target = pick_best_target([active, t0, t3], current_id=99, now=now)
        assert target is not None
        assert target["id"] == 1

    def test_two_t0s_earlier_expiry_wins(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_early = _acct(1, usage_7d=50,
                         resets_7d=_iso(now + timedelta(hours=4)))
        t0_late = _acct(2, usage_7d=50,
                        resets_7d=_iso(now + timedelta(hours=20)))
        target = pick_best_target([active, t0_early, t0_late],
                                  current_id=99, now=now)
        assert target["id"] == 1

    def test_two_t0s_same_expiry_larger_deficit_wins(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        resets = _iso(now + timedelta(hours=12))
        small_deficit = _acct(1, usage_7d=90, resets_7d=resets)
        big_deficit = _acct(2, usage_7d=50, resets_7d=resets)
        target = pick_best_target([active, small_deficit, big_deficit],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_t0_at_target_skipped_in_favor_of_t1(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_done = _acct(1, usage_7d=100,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, t0_done, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_t0_without_5h_headroom_excluded(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_no_5h = _acct(1, usage_5h=95, usage_7d=50,
                         resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_5h=10, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, t0_no_5h, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_no_candidate_when_all_at_target(self):
        from jacked.web.auto_swap import pick_best_target
        t1_expiry = _local_utc(2026, 5, 6, 23, 0)  # T1 floor target 90
        now = t1_expiry - timedelta(hours=36)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_done = _acct(1, usage_7d=100,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1_done = _acct(2, usage_7d=90, resets_7d=_iso(t1_expiry))
        target = pick_best_target([active, t0_done, t1_done],
                                  current_id=99, now=now)
        assert target is None

    def test_excludes_disabled_account(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        disabled = _acct(1, usage_7d=50, auto_swap=False,
                         resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, disabled, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_invalid_account(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        invalid = _acct(1, usage_7d=50, valid=False,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, invalid, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_failures_above_threshold(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        failing = _acct(1, usage_7d=50, failures=5,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, failing, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_no_token(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        no_tok = _acct(1, usage_7d=50, cc_token=False,
                       resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, no_tok, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_dead_cc_chain_is_never_a_target(self):
        """A dead Claude Code chain may still hold a live-recovered access
        token, but its refresh token is spent: a background swap to it
        would force a re-login when that token expires."""
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        dead = _acct(1, usage_7d=50, resets_7d=_iso(now + timedelta(hours=12)))
        dead["cc_refresh_failure_type"] = "invalid_grant"
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, dead, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2


class TestHas5hHeadroom:
    def test_has_room_when_below_90(self):
        from jacked.web.auto_swap import _has_5h_headroom
        assert _has_5h_headroom({"cached_usage_5h": 50}) is True

    def test_no_room_at_95_no_imminent_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": None,
        }) is False

    def test_room_at_95_with_imminent_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(future),
        }) is True

    def test_no_room_at_95_with_distant_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(future),
        }) is False

    def test_past_reset_treated_as_headroom(self):
        """A PAST cached_5h_resets_at means the window already flipped —
        the >=90 cached usage is stale, not a reason to exclude."""
        from jacked.web.auto_swap import _has_5h_headroom
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(now - timedelta(minutes=10)),
        }, now=now) is True

    def test_now_aware_distant_reset_excluded(self):
        from jacked.web.auto_swap import _has_5h_headroom
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(now + timedelta(hours=2)),
        }, now=now) is False

    def test_pick_best_target_admits_past_reset_candidate(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        stale = _acct(1, usage_5h=95, usage_7d=50,
                      resets_5h=_iso(now - timedelta(minutes=5)),
                      resets_7d=_iso(now + timedelta(hours=12)))
        target = pick_best_target([active, stale], current_id=99, now=now)
        assert target is not None
        assert target["id"] == 1

    # -- plan-aware gate: an absent 5h window is headroom, explicitly ------

    def test_verified_ultra_passes_despite_a_stale_5h_reading(self):
        """A verified Ultra has no 5h window. A 5h value that lingers in its
        cache is stale and must not block it (explicit, not None-reads-0)."""
        from jacked.web.auto_swap import _has_5h_headroom
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_5h=100, resets_5h=_iso(now + timedelta(hours=3)),
                     rate_limit_tier="default_claude_max_20x")
        acct["cached_usage_raw"] = ULTRA_RAW
        assert usage_plans.session_window(acct).state == "absent"
        assert _has_5h_headroom(acct, now=now) is True

    def test_ultra_with_a_partial_read_keeps_the_5h_rule(self):
        """No payload proves the window absent (a partial read), and the row
        still holds a 95% reading: the window counts as present (temporary),
        so today's rule applies and a distant reset excludes the account."""
        from jacked.web.auto_swap import _has_5h_headroom
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_5h=95, resets_5h=_iso(now + timedelta(hours=2)),
                     rate_limit_tier="default_claude_max_20x")
        acct["cached_usage_raw"] = json.dumps({"five_hour": None})  # no limits list
        assert usage_plans.session_window(acct).state == "present"
        assert _has_5h_headroom(acct, now=now) is False
        # An unknown limit kind makes even a limits list a partial read.
        acct["cached_usage_raw"] = json.dumps({"limits": [{"kind": "session_v2"}]})
        assert usage_plans.session_window(acct).state == "present"
        assert _has_5h_headroom(acct, now=now) is False

    def test_unverified_ultra_reporting_a_session_keeps_the_5h_rule(self):
        from jacked.web.auto_swap import _has_5h_headroom
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        resets = _iso(now + timedelta(hours=2))
        acct = _acct(1, usage_5h=95, resets_5h=resets,
                     rate_limit_tier="default_claude_max_20x")
        acct["cached_usage_raw"] = json.dumps({
            "five_hour": {"utilization": 95.0, "resets_at": resets},
            "limits": [{"kind": "session", "percent": 95, "resets_at": resets},
                       {"kind": "weekly_all", "percent": 20}],
        })
        window = usage_plans.session_window(acct)
        assert (window.state, window.temporary) == ("present", True)
        assert _has_5h_headroom(acct, now=now) is False

    def test_unknown_ultra_counts_as_having_a_window(self):
        """No payload and no reading: the state is unknown, which counts as a
        window (has_session is True), so the account is never waved through
        on its plan alone; with nothing recorded, today's rule finds room."""
        from jacked.web.auto_swap import _has_5h_headroom
        acct = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}
        assert usage_plans.session_window(acct).state == "unknown"
        assert usage_plans.has_session(acct) is True
        assert _has_5h_headroom(acct) is True

    def test_pick_best_target_admits_a_verified_ultra(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        ultra = _acct(1, usage_5h=100, usage_7d=50,
                      resets_5h=_iso(now + timedelta(hours=3)),
                      resets_7d=_iso(now + timedelta(hours=12)),
                      rate_limit_tier="default_claude_max_20x")
        ultra["cached_usage_raw"] = ULTRA_RAW
        target = pick_best_target([active, ultra], current_id=99, now=now)
        assert target is not None
        assert target["id"] == 1

    def test_pick_best_target_still_excludes_a_saturated_unverified_ultra(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        ultra = _acct(1, usage_5h=100, usage_7d=50,
                      resets_5h=_iso(now + timedelta(hours=3)),
                      resets_7d=_iso(now + timedelta(hours=12)),
                      rate_limit_tier="default_claude_max_20x")
        assert pick_best_target([active, ultra], current_id=99, now=now) is None


class TestShouldSwapNow:
    """Spec scenarios D17-D23 — departure rule."""

    def test_stay_when_no_higher_tier_candidate(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_swap_when_higher_tier_emerged(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=30,
                     resets_7d=_iso(now + timedelta(hours=36)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "higher tier" in reason.lower() or "tier" in reason.lower()

    def test_stay_when_same_tier_candidate(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        same_tier = _acct(2, usage_5h=10, usage_7d=10,
                          resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=same_tier, now=now)
        assert reason is None

    def test_swap_when_active_drained(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "drain" in reason.lower() or "target" in reason.lower()

    def test_swap_when_5h_critical(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now,
                                 five_hour_rules=True)
        assert reason is not None
        assert "5h" in reason.lower() or "critical" in reason.lower()

    def test_no_swap_when_5h_critical_but_reset_imminent(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=8)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now,
                                 five_hour_rules=True)
        assert reason is None

    def test_5h_critical_suppressed_within_30_min_window(self):
        """RESET_SUPPRESS_MINUTES is 30: a reset 25 min out still
        suppresses (would have fired under the old 10-min window)."""
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=25)),
                       resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=None, now=now,
                                 five_hour_rules=True)
        assert reason is None

    def test_5h_critical_fires_beyond_suppression_window(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=40)),
                       resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=None, now=now,
                                 five_hour_rules=True)
        assert reason is not None
        assert reason.startswith("5h critical:")

    def test_swap_when_5h_imminent_but_higher_tier_emerged(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=8)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=12)))
        reason = should_swap_now(active=active, best=best, now=now,
                                 five_hour_rules=True)
        assert reason is not None

    def test_t3_active_rides_out_5h_window(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=10,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=6)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_burn_rate_projection_triggers_swap(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=82, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        br = BurnRate(rate_5h_per_min=2.0, last_check_5h=82.0,
                      rate_7d_per_min=0.0, last_check_7d=0.0)
        reason = should_swap_now(active=active, best=best, burn_rate=br,
                                 check_interval_min=5, now=now,
                                 five_hour_rules=True)
        assert reason is not None
        assert "burn" in reason.lower() or "project" in reason.lower()

    def test_active_excluded_no_best_means_stay(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=None)
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_active_excluded_5h_critical_reaches_rule_3(self):
        """An active with no 7d data must still get 5h-critical
        exhaustion alerting — TIER_EXCLUDED no longer short-circuits
        rules 3-4 when best is None."""
        from jacked.web.auto_swap import should_swap_now, REASON_PREFIX_FIVE_H
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=None)
        reason = should_swap_now(active=active, best=None, now=now,
                                 five_hour_rules=True)
        assert reason is not None
        assert reason.startswith(REASON_PREFIX_FIVE_H)

    def test_active_excluded_with_best_means_swap(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=None)
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=12)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "higher tier" in reason.lower()

    def test_t3_above_floor_with_no_best_does_not_drain(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=30,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=6)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None, f"got: {reason}"

    def test_t2_above_floor_with_no_best_does_not_drain(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_t0_at_100_drains_even_without_best(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")

    def test_t1_at_target_drains_even_without_best(self):
        from jacked.web.auto_swap import should_swap_now
        expiry = _local_utc(2026, 5, 6, 23, 0)  # T1 floor target 90
        now = expiry - timedelta(hours=36)
        active = _acct(1, usage_5h=20, usage_7d=90,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(expiry))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")

    def test_t1_drained_gate_is_deadline_aware(self):
        """A 15:00-local expiry leaves ~7.6% achievable on the final day,
        so the T1 target is ~92.4 — 91% is NOT drained, 93% is."""
        from jacked.web.auto_swap import should_swap_now
        expiry = _local_utc(2026, 5, 6, 15, 0)
        now = expiry - timedelta(hours=36)
        active = _acct(1, usage_5h=20, usage_7d=91,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(expiry))
        assert should_swap_now(active=active, best=None, now=now) is None
        active["cached_usage_7d"] = 93
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")

    def test_reason_prefixes_match_constants(self):
        from jacked.web.auto_swap import (
            should_swap_now,
            REASON_PREFIX_HIGHER_TIER,
            REASON_PREFIX_DRAINED,
            REASON_PREFIX_FIVE_H,
            REASON_PREFIX_BURN_RATE,
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

        active = _acct(1, usage_7d=50, resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_7d=50, resets_7d=_iso(now + timedelta(hours=12)))
        r = should_swap_now(active=active, best=best, now=now)
        assert r.startswith(REASON_PREFIX_HIGHER_TIER), r

        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        r = should_swap_now(active=active, best=None, now=now)
        assert r.startswith(REASON_PREFIX_DRAINED), r

        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        r = should_swap_now(active=active, best=None, now=now,
                            five_hour_rules=True)
        assert r.startswith(REASON_PREFIX_FIVE_H), r

        active = _acct(1, usage_5h=82, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        br = BurnRate(rate_5h_per_min=2.0, last_check_5h=82.0,
                      rate_7d_per_min=0.0, last_check_7d=0.0)
        r = should_swap_now(active=active, best=None, burn_rate=br,
                            check_interval_min=5, now=now,
                            five_hour_rules=True)
        assert r.startswith(REASON_PREFIX_BURN_RATE), r


class TestActiveTierHysteresis:
    """Regression tests for the user-observed 6-min swap delay.

    Without ``prev_tiers``, jitter at the 24h/48h boundary flickered the
    active account's tier each tick, breaking the higher-tier-emerged
    rule and clearing the emergence streak. With prev_tiers, the active
    account is held at its hysteresis-aware tier so the swap fires.
    """

    def test_active_t2_jitters_to_t1_without_prev_tiers_breaks_rule_1(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # Active sits 1 minute past the 48h boundary (raw = T1).
        active = _acct(99, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=47, minutes=59)))
        best = _acct(1, usage_5h=10, usage_7d=30,
                     resets_7d=_iso(now + timedelta(hours=36)))  # also T1
        # No prev_tiers: active classified as T1, best classified as T1.
        # best_tier (1) is NOT < active_rank (1) → no rule 1 fire.
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is None  # the buggy outcome we're guarding against

    def test_active_t2_jitters_to_t1_WITH_prev_tiers_holds_t2(self):
        from jacked.web.auto_swap import should_swap_now, TIER_T1, TIER_T2
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=47, minutes=59)))
        best = _acct(1, usage_5h=10, usage_7d=30,
                     resets_7d=_iso(now + timedelta(hours=36)))
        # With prev_tiers={99: T2}, hysteresis holds active at T2 even
        # though instant says T1. best (T1) < active (T2) → swap fires.
        prev_tiers = {99: TIER_T2, 1: TIER_T1}
        reason = should_swap_now(
            active=active, best=best, now=now,
            prev_tiers=prev_tiers,
        )
        assert reason is not None
        assert reason.startswith("higher tier emerged")


class TestSelectionDrainedT0T1Only:
    """Drained gate must check active_tier (with hysteresis-aware
    consistency), not target_7d's internal recomputation."""

    def test_t0_active_drained_uses_constant_100(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")
        assert "100.0%" in reason  # target = 100, not white-bar-derived

    def test_t1_active_drained_uses_t1_target(self):
        from jacked.web.auto_swap import should_swap_now, T1_TARGET
        expiry = _local_utc(2026, 5, 6, 23, 0)  # T1 floor target 90
        now = expiry - timedelta(hours=36)
        active = _acct(1, usage_5h=20, usage_7d=90,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(expiry))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")
        assert f"{T1_TARGET}" in reason  # target = 90.0


class TestBurstPattern:
    """Spec scenarios G28-G29 — real-life patterns."""

    def test_burst_drains_t0_then_t1_then_t3(self):
        from jacked.web.auto_swap import pick_best_target
        a2_expiry = _local_utc(2026, 5, 10, 23, 0)  # T1 floor target 90
        now = a2_expiry - timedelta(hours=35)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=2)))
        a1 = _acct(1, usage_5h=10, usage_7d=30,
                   resets_7d=_iso(now + timedelta(hours=11)))
        a2 = _acct(2, usage_5h=10, usage_7d=30, resets_7d=_iso(a2_expiry))
        # a3 is T3 (6d to expiry) — white_bar ~14.3% with 1 day elapsed.
        # usage_7d=5 keeps it BELOW the T3 floor so it has a positive
        # deficit and remains an eligible candidate per the strict
        # selection rule (deficit > 0).
        a3 = _acct(3, usage_5h=10, usage_7d=5,
                   resets_7d=_iso(now + timedelta(days=6)))

        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        assert target["id"] == 1

        a1["cached_usage_7d"] = 100
        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        assert target["id"] == 2

        a2["cached_usage_7d"] = 90
        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        assert target["id"] == 3

    def test_higher_tier_emergence_mid_window(self):
        from jacked.web.auto_swap import pick_best_target, should_swap_now
        now = datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc)
        active = _acct(99, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        a1 = _acct(1, usage_5h=10, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=20)))
        target = pick_best_target([active, a1], current_id=99, now=now)
        assert target["id"] == 1
        reason = should_swap_now(active=active, best=target, now=now)
        assert reason is not None
        assert "tier" in reason.lower() or "T0" in reason


class TestCompute7dDeficitNewShape:
    def test_returns_tier_and_target_fields(self):
        from jacked.web.auto_swap import compute_7d_deficit
        expiry = _local_utc(2026, 5, 6, 23, 0)  # T1 floor target 90
        now = expiry - timedelta(hours=36)
        acct = _acct(1, usage_7d=70, resets_7d=_iso(expiry))
        result = compute_7d_deficit(acct, now=now)
        assert result is not None
        assert "tier" in result
        assert result["tier"] == 1
        assert "target_7d" in result
        assert result["target_7d"] == 90.0
        assert "deficit_vs_tier_target" in result
        assert result["deficit_vs_tier_target"] == 20.0
        assert "white_bar" in result
        assert "hours_to_expiry" in result
        # Backwards-compat aliases retained:
        assert "deficit" in result
        assert "unused_7d" in result


# ---------------------------------------------------------------------------
# target_for_tier — damped-tier-aware targets
# ---------------------------------------------------------------------------


class TestTargetForTier:
    def test_t0_is_100(self):
        from jacked.web.auto_swap import TIER_T0, target_for_tier
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        assert target_for_tier(TIER_T0, acct, now) == 100.0

    def test_t2_is_white_bar_plus_lead(self):
        from jacked.web.auto_swap import TIER_T2, target_for_tier, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_for_tier(TIER_T2, acct, now) - (wb + 5.0)) < 1e-6

    def test_t3_is_white_bar(self):
        from jacked.web.auto_swap import TIER_T3, target_for_tier, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_for_tier(TIER_T3, acct, now) - wb) < 1e-6

    def test_excluded_is_none(self):
        from jacked.web.auto_swap import TIER_EXCLUDED, target_for_tier
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        assert target_for_tier(TIER_EXCLUDED, _acct(1), now) is None

    def test_damped_tier_wins_over_instant_tier(self):
        """An account held at T1 by hysteresis must be priced with T1's
        target, not the instantaneous T0's 100."""
        from jacked.web.auto_swap import TIER_T1, target_for_tier
        expiry = _local_utc(2026, 5, 6, 23, 0)
        now = expiry - timedelta(hours=23, minutes=58)  # instant says T0
        acct = _acct(1, resets_7d=_iso(expiry))
        assert target_for_tier(TIER_T1, acct, now) == 90.0

    def test_target_7d_delegates_to_target_for_tier(self):
        from jacked.web.auto_swap import target_7d, target_for_tier, tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        for hours in (12, 36, 72, 144):
            acct = _acct(1, resets_7d=_iso(now + timedelta(hours=hours)))
            tier = tier_for(acct, now=now)
            assert target_7d(acct, now=now) == target_for_tier(tier, acct, now)


class TestDeadlineAwareT1Target:
    """T1 target = 100 minus what the expiry day's working hours can still
    burn, floored at T1_TARGET. Expiry outside working hours reclaims the
    buffer that a fixed 90 would strand."""

    def test_overnight_expiry_drains_to_100(self):
        from jacked.web.auto_swap import target_7d
        expiry = _local_utc(2026, 5, 6, 3, 0)  # 03:00 local: 0 working hours
        now = expiry - timedelta(hours=36)
        acct = _acct(1, resets_7d=_iso(expiry))
        assert target_7d(acct, now=now) == 100.0

    def test_midafternoon_expiry_partial_buffer(self):
        from jacked.web.auto_swap import target_7d
        expiry = _local_utc(2026, 5, 6, 15, 0)  # 9 working hours that day
        now = expiry - timedelta(hours=36)
        acct = _acct(1, resets_7d=_iso(expiry))
        # 9h / 5h windows * (500/119)%/window = 7.563% achievable
        expected = 100.0 - (9 / 5) * (500 / 119)
        assert target_7d(acct, now=now) == pytest.approx(expected)

    def test_late_evening_expiry_floors_at_90(self):
        from jacked.web.auto_swap import T1_TARGET, target_7d
        expiry = _local_utc(2026, 5, 6, 23, 0)  # full 17h working day
        now = expiry - timedelta(hours=36)
        acct = _acct(1, resets_7d=_iso(expiry))
        assert target_7d(acct, now=now) == T1_TARGET

    def test_custom_active_hours_change_target(self):
        from jacked.web.auto_swap import target_7d
        expiry = _local_utc(2026, 5, 6, 13, 0)
        now = expiry - timedelta(hours=36)
        acct = _acct(1, resets_7d=_iso(expiry))
        # 09:00-17:00: 4 working hours before expiry, burn/window =
        # 100/11.2 -> achievable = 0.8 * 8.93 = 7.14 -> target 92.86
        expected = 100.0 - (4 / 5) * (100 / 11.2)
        result = target_7d(acct, now=now,
                           active_start="09:00", active_end="17:00")
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# pick_best_target — damped-tier deficit (hysteresis admit consistency)
# ---------------------------------------------------------------------------


class TestPickBestTargetDampedDeficit:
    def test_hysteresis_held_t1_admitted_with_t1_target(self):
        """An account instantaneously T0 but damped to T1 must be priced
        with T1's target: at 95% vs a floor-90 target it is NOT a
        candidate. Without the hold it IS (T0 target 100, deficit 5)."""
        from jacked.web.auto_swap import TIER_T1, pick_best_target
        expiry = _local_utc(2026, 5, 6, 23, 0)
        now = expiry - timedelta(hours=23, minutes=58)  # 2 min into T0
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        held = _acct(1, usage_7d=95, resets_7d=_iso(expiry))
        target = pick_best_target([active, held], current_id=99, now=now,
                                  prev_tiers={1: TIER_T1})
        assert target is None
        target = pick_best_target([active, held], current_id=99, now=now)
        assert target is not None
        assert target["id"] == 1


# ---------------------------------------------------------------------------
# _SortKey — chronological ordering across timestamp formats
# ---------------------------------------------------------------------------


class TestSortKeyChronological:
    def test_mixed_timestamp_formats_sort_chronologically(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        # Lexicographically "2026-05-04T18:00:00Z" sorts BEFORE
        # "2026-05-05T01:00:00+09:00" even though the latter is 16:00 UTC
        # — two hours EARLIER. The epoch key must pick the earlier one.
        later = _acct(1, usage_7d=50)
        later["cached_7d_resets_at"] = "2026-05-04T18:00:00Z"
        earlier = _acct(2, usage_7d=50)
        earlier["cached_7d_resets_at"] = "2026-05-05T01:00:00+09:00"
        target = pick_best_target([active, later, earlier],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_epoch_or_inf_fallback(self):
        from jacked.web.auto_swap.selection import _epoch_or_inf
        assert _epoch_or_inf(None) == float("inf")
        assert _epoch_or_inf("") == float("inf")
        assert _epoch_or_inf("not-a-date") == float("inf")
        assert _epoch_or_inf("2026-05-04T18:00:00Z") < float("inf")


# ---------------------------------------------------------------------------
# should_swap_now rule 1b — intra-T0 preemption
# ---------------------------------------------------------------------------


class TestIntraT0Preemption:
    """Strict tier inequality alone lets an active T0 with a long runway
    block a T0 candidate expiring in hours — the largest stranding
    mechanism found. Margin gates prevent ping-pong."""

    def test_fires_for_faster_losing_earlier_t0(self):
        from jacked.web.auto_swap import (
            REASON_PREFIX_INTRA_TIER,
            should_swap_now,
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=20)))  # 1.0%/h
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=6)))  # 8.3%/h
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert reason.startswith(REASON_PREFIX_INTRA_TIER)

    def test_does_not_fire_below_loss_rate_margin(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=20)))  # 2.5%/h
        best = _acct(2, usage_5h=10, usage_7d=40,
                     resets_7d=_iso(now + timedelta(hours=18)))  # 3.33 < 3.75
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is None

    def test_does_not_fire_below_deficit_floor(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=20)))  # 1.0%/h
        best = _acct(2, usage_5h=10, usage_7d=96,
                     resets_7d=_iso(now + timedelta(hours=2)))  # deficit 4 < 5
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is None

    def test_does_not_fire_when_best_expires_later(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=6)))
        best = _acct(2, usage_5h=10, usage_7d=10,
                     resets_7d=_iso(now + timedelta(hours=20)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is None

    def test_overrides_5h_reset_suppression(self):
        """Part of rule 1 — fires even while an imminent 5h reset
        suppresses the critical rule."""
        from jacked.web.auto_swap import (
            REASON_PREFIX_INTRA_TIER,
            should_swap_now,
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=80,
                       resets_5h=_iso(now + timedelta(minutes=8)),
                       resets_7d=_iso(now + timedelta(hours=20)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=6)))
        reason = should_swap_now(active=active, best=best, now=now,
                                 five_hour_rules=True)
        assert reason is not None
        assert reason.startswith(REASON_PREFIX_INTRA_TIER)


# ---------------------------------------------------------------------------
# Reset-suppression invariant
# ---------------------------------------------------------------------------


class TestResetSuppressInvariant:
    def test_headroom_lookahead_within_suppression_window(self):
        """Selection's imminent-reset lookahead must be <= the 5h-critical
        suppression window, else accounts admitted via that branch are
        immediately ejected by rule 3 (deterministic ping-pong)."""
        from jacked.web.auto_swap import RESET_SUPPRESS_MINUTES
        from jacked.web.auto_swap.selection import _FIVE_H_HEADROOM_RESET_MIN
        assert RESET_SUPPRESS_MINUTES == 30
        assert _FIVE_H_HEADROOM_RESET_MIN <= RESET_SUPPRESS_MINUTES


# ---------------------------------------------------------------------------
# achievable_burn / stranding_estimate diagnostics
# ---------------------------------------------------------------------------


class TestAchievableBurn:
    def test_remaining_working_hours_convert_to_burn(self):
        from jacked.web.auto_swap import achievable_burn
        now = _local_utc(2026, 5, 4, 6, 0)       # 06:00 local
        expiry = _local_utc(2026, 5, 4, 16, 0)   # 16:00 local -> 10h working
        acct = _acct(1, resets_7d=_iso(expiry))
        # 10h / 5h windows * (500/119)%/window
        assert achievable_burn(acct, now=now) == pytest.approx(2 * 500 / 119)

    def test_zero_when_expired(self):
        from jacked.web.auto_swap import achievable_burn
        now = _local_utc(2026, 5, 4, 12, 0)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert achievable_burn(acct, now=now) == 0.0

    def test_none_when_no_reset_data(self):
        from jacked.web.auto_swap import achievable_burn
        assert achievable_burn(_acct(1, resets_7d=None)) is None

    def test_custom_active_hours(self):
        from jacked.web.auto_swap import achievable_burn
        now = _local_utc(2026, 5, 4, 9, 0)
        expiry = _local_utc(2026, 5, 4, 17, 0)
        acct = _acct(1, resets_7d=_iso(expiry))
        # 8 working hours, burn/window = 100/11.2
        result = achievable_burn(acct, now=now,
                                 active_start="09:00", active_end="17:00")
        assert result == pytest.approx((8 / 5) * (100 / 11.2))


class TestStrandingEstimate:
    def test_deficit_beyond_achievable_burn_is_stranded(self):
        from jacked.web.auto_swap import stranding_estimate
        now = _local_utc(2026, 5, 4, 6, 0)
        expiry = _local_utc(2026, 5, 4, 16, 0)  # T0, 10 working hours left
        acct = _acct(1, usage_7d=70, resets_7d=_iso(expiry))
        # deficit 30 (T0 target 100) minus 8.4% achievable
        assert stranding_estimate(acct, now=now) == pytest.approx(
            30.0 - 2 * 500 / 119)

    def test_zero_when_deficit_recoverable(self):
        from jacked.web.auto_swap import stranding_estimate
        now = _local_utc(2026, 5, 4, 6, 0)
        expiry = _local_utc(2026, 5, 4, 16, 0)
        acct = _acct(1, usage_7d=99, resets_7d=_iso(expiry))
        assert stranding_estimate(acct, now=now) == 0.0

    def test_none_when_usage_missing(self):
        from jacked.web.auto_swap import stranding_estimate
        now = _local_utc(2026, 5, 4, 6, 0)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=10)))
        acct["cached_usage_7d"] = None
        assert stranding_estimate(acct, now=now) is None

    def test_none_when_no_reset_data(self):
        from jacked.web.auto_swap import stranding_estimate
        assert stranding_estimate(_acct(1, resets_7d=None)) is None


# ---------------------------------------------------------------------------
# #224i walled-only departure: rules 2a (weekly) and 2b (model weekly)
# ---------------------------------------------------------------------------


class TestWalledOnlyDeparture:
    """Leave an account only when it cannot serve: its weekly limit, or its
    binding model limit, is at the plan's weekly_critical_pct (98). The 5h
    rules (3 and 4) run only when the caller turns them on."""

    NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

    def _far(self):
        return _iso(self.NOW + timedelta(hours=72))  # T2 for the weekly window

    def _soon(self):
        return _iso(self.NOW + timedelta(minutes=10))

    # -- rule 2a: the weekly limit -----------------------------------------

    def test_weekly_wall_departs(self):
        active = _claude_acct(1, usage_7d=100, resets_7d=self._far())
        healthy = _claude_acct(2, usage_7d=30, resets_7d=self._far())
        reason = should_swap_now(active=active, best=healthy, now=self.NOW)
        assert reason == "weekly critical: 7d usage 100.0% >= 98%"
        assert reason.startswith(REASON_PREFIX_WEEKLY_CRITICAL)

    def test_weekly_wall_resetting_soon_is_suppressed(self):
        """The account gets its whole week back before a swap pays off. At
        100% a T0 account is drained by rule 2, which has no reset
        suppression, so 99% isolates rule 2a."""
        active = _claude_acct(1, usage_7d=99, resets_7d=self._soon())
        assert should_swap_now(active=active, best=None, now=self.NOW) is None
        active = _claude_acct(1, usage_7d=99, resets_7d=_iso(self.NOW + timedelta(hours=2)))
        assert should_swap_now(active=active, best=None, now=self.NOW) == (
            "weekly critical: 7d usage 99.0% >= 98%"
        )

    def test_the_weekly_wall_starts_at_the_threshold(self):
        active = _claude_acct(1, usage_7d=97.9, resets_7d=self._far())
        assert should_swap_now(active=active, best=None, now=self.NOW) is None
        active = _claude_acct(1, usage_7d=98.0, resets_7d=self._far())
        assert should_swap_now(active=active, best=None, now=self.NOW) == (
            "weekly critical: 7d usage 98.0% >= 98%"
        )

    def test_the_model_wall_starts_at_the_threshold(self):
        far = self._far()
        below = _claude_acct(1, usage_7d=60, resets_7d=far,
                             models=[("Fable", 97.9, far, True)])
        assert should_swap_now(active=below, best=None, now=self.NOW) is None
        at = _claude_acct(1, usage_7d=60, resets_7d=far, models=[("Fable", 98, far, True)])
        assert should_swap_now(active=at, best=None, now=self.NOW) == (
            "model weekly critical: Fable 98.0% >= 98%"
        )

    def test_a_missing_or_text_weekly_reading_is_not_a_wall(self):
        """A missing reading is not 0% and not 100%; a TEXT value from an
        old writer reads through coerce_pct and never raises."""
        active = _claude_acct(1, usage_7d=100, resets_7d=self._far())
        active["cached_usage_7d"] = None
        assert should_swap_now(active=active, best=None, now=self.NOW) is None
        active["cached_usage_7d"] = "99.5"
        assert should_swap_now(active=active, best=None, now=self.NOW) == (
            "weekly critical: 7d usage 99.5% >= 98%"
        )

    # -- rule 2b: the binding model limit ----------------------------------

    def test_model_wall_departs_with_the_exact_reason(self):
        far = self._far()
        active = _claude_acct(1, usage_7d=60, resets_7d=far,
                              models=[("Fable", 100, far, True)])
        healthy = _claude_acct(2, usage_7d=30, resets_7d=far)
        assert should_swap_now(active=active, best=healthy, now=self.NOW) == (
            "model weekly critical: Fable 100.0% >= 98%"
        )

    def test_model_wall_resetting_soon_is_suppressed(self):
        active = _claude_acct(1, usage_7d=60, resets_7d=self._far(),
                              models=[("Fable", 100, self._soon(), True)])
        assert should_swap_now(active=active, best=None, now=self.NOW) is None

    def test_the_binding_model_is_the_provider_flagged_one(self):
        """The same selection as the pill and the poll urgency: the limit the
        provider flags is_active, else the highest. A walled model that is
        not binding is not the active's wall."""
        far = self._far()
        active = _claude_acct(1, usage_7d=60, resets_7d=far,
                              models=[("Fable", 40, far, True), ("Sonnet", 99, far, False)])
        assert should_swap_now(active=active, best=None, now=self.NOW) is None

    def test_a_non_string_model_reset_never_raises(self):
        """A provider can send any JSON type for resets_at. A number is not a
        reset the code can read, so it never suppresses the wall."""
        active = _claude_acct(1, usage_7d=60, resets_7d=self._far(),
                              models=[("Fable", 100, 1793780314, True)])
        assert should_swap_now(active=active, best=None, now=self.NOW) == (
            "model weekly critical: Fable 100.0% >= 98%"
        )

    # -- a reset that already passed makes the reading stale ---------------

    def _past(self):
        return _iso(self.NOW - timedelta(minutes=5))

    def test_a_weekly_wall_whose_reset_passed_is_stale(self):
        """The window reset 5 minutes ago; the 100% reading is from before
        the reset. The next poll replaces it, so it is not a departure."""
        active = _claude_acct(1, usage_7d=100, resets_7d=self._past())
        assert should_swap_now(active=active, best=None, now=self.NOW) is None

    def test_a_model_wall_whose_reset_passed_is_stale(self):
        active = _claude_acct(1, usage_7d=60, resets_7d=self._far(),
                              models=[("Fable", 100, self._past(), True)])
        assert should_swap_now(active=active, best=None, now=self.NOW) is None

    def test_a_missing_or_unreadable_reset_keeps_the_wall(self):
        """With no reset that the code can read, nothing shows the reading is
        stale or about to lift, so the wall holds (today's behavior)."""
        far = self._far()
        for resets in (None, "not-a-date"):
            weekly = _claude_acct(1, usage_7d=100, resets_7d=resets)
            assert should_swap_now(active=weekly, best=None, now=self.NOW) == (
                "weekly critical: 7d usage 100.0% >= 98%"
            )
            model = _claude_acct(1, usage_7d=60, resets_7d=far,
                                 models=[("Fable", 100, resets, True)])
            assert should_swap_now(active=model, best=None, now=self.NOW) == (
                "model weekly critical: Fable 100.0% >= 98%"
            )

    def test_the_5h_rules_keep_their_own_reset_handling(self):
        """Unchanged by the stale-wall rule: a past 5h reset is headroom for a
        candidate (_has_5h_headroom), and it does not suppress rule 3."""
        from jacked.web.auto_swap import _has_5h_headroom

        active = _acct(1, usage_5h=95, usage_7d=50, resets_5h=self._past(),
                       resets_7d=_iso(self.NOW + timedelta(days=3)))
        assert _has_5h_headroom(active, now=self.NOW) is True
        assert should_swap_now(active=active, best=None, now=self.NOW,
                               five_hour_rules=True) == "5h critical: 95.0% >= 90%"

    # -- no candidate, rule order, thresholds ------------------------------

    def test_walls_fire_without_a_candidate(self):
        """No candidate does not silence a wall. The monitor turns a reason
        with no target into the no_target decision, the exhaustion WARNING
        and the all_accounts_exhausted banner. An Ultra user walled on the
        week needs that banner, so the rule must not require a candidate."""
        far = self._far()
        weekly = _claude_acct(1, usage_7d=100, resets_7d=far)
        assert should_swap_now(active=weekly, best=None, now=self.NOW).startswith(
            REASON_PREFIX_WEEKLY_CRITICAL)
        model = _claude_acct(1, usage_7d=60, resets_7d=far,
                             models=[("Fable", 100, far, True)])
        assert should_swap_now(active=model, best=None, now=self.NOW).startswith(
            REASON_PREFIX_MODEL_WEEKLY)

    def test_the_weekly_rule_comes_before_the_model_rule(self):
        far = self._far()
        both = _claude_acct(1, usage_7d=100, resets_7d=far,
                            models=[("Fable", 100, far, True)])
        assert should_swap_now(active=both, best=None, now=self.NOW).startswith(
            REASON_PREFIX_WEEKLY_CRITICAL)

    def test_a_higher_tier_candidate_still_wins_over_a_weekly_wall(self):
        active = _claude_acct(1, usage_7d=100, resets_7d=self._far())  # T2
        t0 = _acct(2, usage_7d=50, resets_7d=_iso(self.NOW + timedelta(hours=12)))
        assert should_swap_now(active=active, best=t0, now=self.NOW).startswith(
            REASON_PREFIX_HIGHER_TIER)

    def test_an_explicit_threshold_wins_over_the_catalog(self):
        far = self._far()
        weekly = _claude_acct(1, usage_7d=92, resets_7d=far)
        assert should_swap_now(active=weekly, best=None, now=self.NOW) is None
        assert should_swap_now(active=weekly, best=None, now=self.NOW,
                               weekly_critical=90) == "weekly critical: 7d usage 92.0% >= 90%"
        model = _claude_acct(1, usage_7d=60, resets_7d=far,
                             models=[("Fable", 92, far, True)])
        assert should_swap_now(active=model, best=None, now=self.NOW) is None
        assert should_swap_now(active=model, best=None, now=self.NOW, weekly_critical=90) == (
            "model weekly critical: Fable 92.0% >= 90%"
        )

    def test_the_threshold_defaults_to_the_active_plan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
            "claude": {"subscriptions": {"max": {"label": "Max", "tiers": {
                "default_claude_max_20x": {"id": "ultra", "label": "Ultra",
                                           "weekly_critical_pct": 90},
                "default_claude_max_5x": {"id": "max", "label": "Max"},
            }}}},
        }))
        far = self._far()
        ultra = _claude_acct(1, usage_7d=92, resets_7d=far)
        assert should_swap_now(active=ultra, best=None, now=self.NOW) == (
            "weekly critical: 7d usage 92.0% >= 90%"
        )
        max5 = _claude_acct(1, usage_7d=92, resets_7d=far,
                            rate_limit_tier="default_claude_max_5x")
        assert should_swap_now(active=max5, best=None, now=self.NOW) is None

    # -- rules 3 and 4 are off by default ----------------------------------

    def test_the_5h_critical_rule_is_off_by_default(self):
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(self.NOW + timedelta(hours=2)),
                       resets_7d=_iso(self.NOW + timedelta(days=3)))
        assert should_swap_now(active=active, best=None, now=self.NOW) is None
        assert should_swap_now(active=active, best=None, now=self.NOW,
                               five_hour_rules=True) == "5h critical: 95.0% >= 90%"

    def test_the_burn_rate_rule_is_off_by_default(self):
        active = _acct(1, usage_5h=82, usage_7d=50,
                       resets_5h=_iso(self.NOW + timedelta(hours=2)),
                       resets_7d=_iso(self.NOW + timedelta(days=3)))
        br = BurnRate(rate_5h_per_min=2.0, last_check_5h=82.0,
                      rate_7d_per_min=0.0, last_check_7d=0.0)
        assert should_swap_now(active=active, best=None, burn_rate=br,
                               check_interval_min=5, now=self.NOW) is None
        reason = should_swap_now(active=active, best=None, burn_rate=br,
                                 check_interval_min=5, now=self.NOW,
                                 five_hour_rules=True)
        assert reason.startswith(REASON_PREFIX_BURN_RATE)


class TestPickBestTargetWalledAdmission:
    """pick_best_target never offers an account that cannot serve: one walled
    on the model the active account binds on, or one that rule 2a or 2b would
    eject on the next tick (a swap that undoes itself is a ping-pong)."""

    NOW = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

    def _far(self):
        return _iso(self.NOW + timedelta(hours=72))

    def _t0(self, hours=12):
        return _iso(self.NOW + timedelta(hours=hours))

    def _fable_active(self, pct=100):
        far = self._far()
        return _claude_acct(99, usage_7d=60, resets_7d=far,
                            models=[("Fable", pct, far, True)])

    def test_a_candidate_walled_on_the_active_binding_model_is_skipped(self):
        """The candidate binds on Sonnet (20%), so rule 2b would not eject it,
        but it cannot serve Fable, the model the user works on."""
        far = self._far()
        walled = _claude_acct(1, usage_7d=50, resets_7d=self._t0(12),
                              models=[("Sonnet", 20, far, True), ("Fable", 98, far, False)])
        healthy = _claude_acct(2, usage_7d=50, resets_7d=self._t0(20),
                               models=[("Fable", 20, far, True)])
        active = self._fable_active()
        assert pick_best_target([active, walled, healthy], current_id=99, now=self.NOW)["id"] == 2
        assert pick_best_target([active, walled], current_id=99, now=self.NOW) is None
        # The rule follows the active's binding model, walled or not.
        assert pick_best_target([self._fable_active(pct=40), walled],
                                current_id=99, now=self.NOW) is None
        # An active with no model limit names no model to protect.
        bare = _claude_acct(99, usage_7d=60, resets_7d=far)
        assert pick_best_target([bare, walled], current_id=99, now=self.NOW)["id"] == 1

    def test_a_candidate_walled_on_another_model_is_admitted(self):
        """Walled on Sonnet, binding on Fable at 20%: it serves Fable, and
        rule 2b reads Fable, so the swap holds."""
        far = self._far()
        sonnet_walled = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                                     models=[("Fable", 20, far, True), ("Sonnet", 100, far, False)])
        assert pick_best_target([self._fable_active(), sonnet_walled],
                                current_id=99, now=self.NOW)["id"] == 1

    def test_a_candidate_whose_own_binding_model_is_walled_is_skipped(self):
        """Rule 2b would eject it on the next tick (model weekly critical:
        Sonnet), and the walled active would come back: a ping-pong."""
        far = self._far()
        binds_walled = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                                    models=[("Sonnet", 100, far, True), ("Fable", 20, far, False)])
        assert pick_best_target([self._fable_active(), binds_walled],
                                current_id=99, now=self.NOW) is None
        resets_soon = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                                   models=[("Sonnet", 100, _iso(self.NOW + timedelta(minutes=10)), True),
                                           ("Fable", 20, far, False)])
        assert pick_best_target([self._fable_active(), resets_soon],
                                current_id=99, now=self.NOW)["id"] == 1

    def test_a_t0_candidate_at_its_weekly_wall_is_skipped(self):
        """A T0 candidate at 98.5% has a deficit of 1.5, which bypasses the
        burn floor, but rule 2a would eject it on the next tick. One that
        resets within the suppression window is admitted: 2a stays quiet
        until its week comes back."""
        active = _claude_acct(99, usage_7d=100, resets_7d=self._far())
        near_wall = _claude_acct(1, usage_7d=98.5, resets_7d=self._t0(6))
        assert pick_best_target([active, near_wall], current_id=99, now=self.NOW) is None
        resets_soon = _claude_acct(1, usage_7d=98.5,
                                   resets_7d=_iso(self.NOW + timedelta(minutes=20)))
        assert pick_best_target([active, resets_soon], current_id=99, now=self.NOW)["id"] == 1

    def test_two_t0_accounts_at_their_weekly_wall_never_swap(self):
        """The ping-pong this admission rule closes. A is at 98.5% and B at
        99.0%, both T0 with resets hours away, so the T0 burn-floor bypass
        (deficit >= 1.0) would admit each. Whichever is active, rule 2a
        walls it and pick_best_target offers the other in neither
        direction: a no-target stay, not a swap every cooldown."""
        a = _claude_acct(1, usage_7d=98.5, resets_7d=self._t0(6))
        b = _claude_acct(2, usage_7d=99.0, resets_7d=self._t0(8))
        for active in (a, b):
            assert should_swap_now(active=active, best=None, now=self.NOW).startswith(
                REASON_PREFIX_WEEKLY_CRITICAL)
            assert pick_best_target([a, b], current_id=active["id"], now=self.NOW) is None

    def test_a_pro_candidate_at_its_own_wall_is_skipped_under_an_ultra_active(
        self, tmp_path, monkeypatch,
    ):
        """Pro's field is 95 here and Ultra's 98. A Pro candidate at 96% is
        walled on its own plan, so it is skipped. The Ultra active's 98
        would admit it (the explicit override shows it), and rule 2a at
        Pro's 95 would eject it on the next tick. This is why the monitor
        never passes the active's threshold to pick_best_target."""
        monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
            "claude": {"subscriptions": {
                "max": {"label": "Max", "tiers": {
                    "default_claude_max_20x": {"id": "ultra", "label": "Ultra"},
                }},
                "pro": {"label": "Pro", "session": "always", "weekly_critical_pct": 95},
            }},
        }))
        active = _claude_acct(99, usage_7d=100, resets_7d=self._far())
        pro = _acct(1, usage_7d=96, resets_7d=self._t0(6), subscription_type="pro")
        assert weekly_critical_threshold(active) == 98.0
        assert weekly_critical_threshold(pro) == 95.0
        assert pick_best_target([active, pro], current_id=99, now=self.NOW) is None
        assert pick_best_target([active, pro], current_id=99, now=self.NOW,
                                weekly_critical=98)["id"] == 1
        assert should_swap_now(active=pro, best=None, now=self.NOW) == (
            "weekly critical: 7d usage 96.0% >= 95%"
        )

    def test_a_candidate_whose_fable_reset_passed_is_admitted(self):
        """Its Fable limit reset 5 minutes ago, so the 100% reading is stale:
        neither rule 2b nor the active's-binding-model rule holds it back."""
        far = self._far()
        stale = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                             models=[("Fable", 100, _iso(self.NOW - timedelta(minutes=5)), True)])
        assert pick_best_target([self._fable_active(), stale],
                                current_id=99, now=self.NOW)["id"] == 1
        live = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                            models=[("Fable", 100, far, True)])
        assert pick_best_target([self._fable_active(), live],
                                current_id=99, now=self.NOW) is None

    def test_a_candidate_whose_weekly_reset_passed_is_not_walled(self):
        """The shared predicate does not hold a 99% candidate whose 7d reset
        passed: the reading is stale. pick_best_target still waits for the
        refresh, because an expired 7d window has no tier (TIER_EXCLUDED)
        to price it with. The monitor re-fetches such a candidate on the next
        tick (_candidate_staleness_override)."""
        from jacked.web.auto_swap import TIER_EXCLUDED, tier_for
        from jacked.web.auto_swap.selection import _cannot_serve

        past = _iso(self.NOW - timedelta(minutes=5))
        stale = _claude_acct(1, usage_7d=99, resets_7d=past)
        assert _cannot_serve(stale, None, None, self.NOW) is False
        live = _claude_acct(1, usage_7d=99, resets_7d=self._t0(6))
        assert _cannot_serve(live, None, None, self.NOW) is True
        assert tier_for(stale, now=self.NOW) == TIER_EXCLUDED

    def test_pick_honors_an_explicit_threshold(self):
        far = self._far()
        cand = _claude_acct(1, usage_7d=50, resets_7d=self._t0(),
                            models=[("Sonnet", 10, far, True), ("Fable", 92, far, False)])
        active = self._fable_active(pct=50)
        assert pick_best_target([active, cand], current_id=99, now=self.NOW)["id"] == 1
        assert pick_best_target([active, cand], current_id=99, now=self.NOW,
                                weekly_critical=90) is None

    def test_each_candidate_is_judged_by_its_own_plan(self, tmp_path, monkeypatch):
        """A candidate's wall is its own plan's field: the threshold rule 2b
        applies to it once it is active."""
        monkeypatch.setattr(usage_plans, "_catalog_cache", _write_catalog(tmp_path, {
            "claude": {"subscriptions": {"max": {"label": "Max", "tiers": {
                "default_claude_max_20x": {"id": "ultra", "label": "Ultra",
                                           "weekly_critical_pct": 90},
                "default_claude_max_5x": {"id": "max", "label": "Max"},
            }}}},
        }))
        far = self._far()
        models = [("Sonnet", 10, far, True), ("Fable", 92, far, False)]
        ultra = _claude_acct(1, usage_7d=50, resets_7d=self._t0(), models=models)
        max5 = _claude_acct(2, usage_7d=50, resets_7d=self._t0(),
                            models=models, rate_limit_tier="default_claude_max_5x")
        active = _claude_acct(99, usage_7d=60, resets_7d=far,
                              models=[("Fable", 50, far, True)],
                              rate_limit_tier="default_claude_max_5x")
        assert pick_best_target([active, ultra], current_id=99, now=self.NOW) is None
        assert pick_best_target([active, max5], current_id=99, now=self.NOW)["id"] == 2


def test_resets_within_at_reads_only_strings():
    from jacked.web.auto_swap.selection import _resets_within_at

    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    assert _resets_within_at(1793780314, 30, now) is False
    assert _resets_within_at({"at": "soon"}, 30, now) is False
    assert _resets_within_at(_iso(now + timedelta(minutes=10)), 30, now) is True
