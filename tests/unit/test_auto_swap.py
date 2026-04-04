"""Tests for the auto-swap decision engine (pure functions, no I/O)."""

import time

import pytest

from jacked.web.auto_swap import (
    BurnRate,
    _resets_within,
    compute_7d_deficit,
    compute_effective_working_hours,
    pick_best_target,
    score_candidate,
    should_swap,
    tier_critical_threshold,
    update_burn_rate,
)


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


# ---------------------------------------------------------------------------
# should_swap
# ---------------------------------------------------------------------------

class TestShouldSwap:
    def test_swap_when_5h_above_critical(self):
        assert should_swap(usage_5h=92, usage_7d=0) is True

    def test_no_swap_when_below_warning(self):
        assert should_swap(usage_5h=50, usage_7d=0) is False

    def test_swap_when_7d_above_threshold(self):
        assert should_swap(usage_5h=0, usage_7d=87) is True

    def test_swap_when_warning_and_burn_rate_high(self):
        br = BurnRate(
            rate_5h_per_min=2.0,      # 2%/min -- will hit 90 in 4 min
            last_check_5h=80.0,
            rate_7d_per_min=0.0,
            last_check_7d=0.0,
        )
        assert should_swap(usage_5h=82, usage_7d=0, burn_rate=br) is True

    def test_no_swap_when_warning_but_burn_rate_low(self):
        br = BurnRate(
            rate_5h_per_min=0.01,      # glacial -- won't hit 90 soon
            last_check_5h=80.0,
            rate_7d_per_min=0.0,
            last_check_7d=0.0,
        )
        assert should_swap(usage_5h=82, usage_7d=0, burn_rate=br) is False

    def test_no_swap_when_usage_is_none(self):
        assert should_swap(usage_5h=None, usage_7d=0) is False


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    def test_score_lower_usage_is_better(self):
        low = score_candidate(_acct(1, usage_5h=20, usage_7d=10))
        high = score_candidate(_acct(2, usage_5h=60, usage_7d=40))
        assert low > high

    def test_score_inactive_window_gets_bonus(self):
        from datetime import datetime, timezone, timedelta
        future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with_reset = score_candidate(_acct(1, usage_5h=20, usage_7d=10,
                                           resets_5h=future_iso))
        without_reset = score_candidate(_acct(2, usage_5h=20, usage_7d=10,
                                              resets_5h=None))
        assert without_reset == with_reset + 15

    def test_score_handles_none_usage(self):
        acct = _acct(1)
        acct["cached_usage_5h"] = None
        acct["cached_usage_7d"] = None
        # 100 - 0 (5h) - 0 (7d) + 27.0 (headroom: max tier_crit=90, 90*0.3)
        # + 15 (resets_5h=None -> inactive bonus)
        assert score_candidate(acct) == 142.0


# ---------------------------------------------------------------------------
# pick_best_target
# ---------------------------------------------------------------------------

class TestPickBestTarget:
    def test_pick_excludes_current(self):
        accounts = [_acct(1, usage_5h=10), _acct(2, usage_5h=20)]
        result = pick_best_target(accounts, current_id=1)
        assert result["id"] == 2

    def test_pick_excludes_high_7d(self):
        accounts = [_acct(1, usage_5h=10), _acct(2, usage_5h=10, usage_7d=90)]
        result = pick_best_target(accounts, current_id=1)
        assert result is None  # only candidate is over 7d threshold

    def test_pick_excludes_no_cc_token(self):
        accounts = [_acct(1, usage_5h=10), _acct(2, usage_5h=10, cc_token=False)]
        result = pick_best_target(accounts, current_id=1)
        assert result is None

    def test_pick_excludes_disabled_auto_swap(self):
        accounts = [_acct(1, usage_5h=10), _acct(2, usage_5h=10, auto_swap=False)]
        result = pick_best_target(accounts, current_id=1)
        assert result is None

    def test_pick_best_by_score(self):
        accounts = [
            _acct(1, usage_5h=80),   # current
            _acct(2, usage_5h=50),   # worse
            _acct(3, usage_5h=10),   # best
            _acct(4, usage_5h=30),   # middle
        ]
        result = pick_best_target(accounts, current_id=1)
        assert result["id"] == 3

    def test_pick_returns_none_when_no_candidates(self):
        accounts = [_acct(1, usage_5h=10)]
        result = pick_best_target(accounts, current_id=1)
        assert result is None


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

def test_tier_threshold_10x():
    assert tier_critical_threshold({"rate_limit_tier": "default_claude_max_10x"}) == 90.0

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


# ---------------------------------------------------------------------------
# score_candidate — reset-time and tier-headroom scoring
# ---------------------------------------------------------------------------

def test_score_reset_time_aware():
    """Account resetting sooner gets less 7d penalty at same usage."""
    from datetime import datetime, timezone, timedelta
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    next_week = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
    a = _acct(1, usage_5h=30, usage_7d=60, resets_7d=next_week)
    b = _acct(2, usage_5h=30, usage_7d=60, resets_7d=tomorrow)
    # Account with more days left should score higher (more room)
    assert score_candidate(a) > score_candidate(b)


def test_score_tier_headroom_bonus():
    """20x account at 85% has more headroom than pro at 85%."""
    a = _acct(1, usage_5h=85, rate_limit_tier="default_claude_max_20x")
    b = _acct(2, usage_5h=85, rate_limit_tier="pro", subscription_type="pro")
    # 20x has 10% headroom (95-85), pro has 0% (80-85 clamped to 0)
    assert score_candidate(a) > score_candidate(b)


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
# should_swap — window-reset-aware suppression
# ---------------------------------------------------------------------------

class TestShouldSwapWindowAware:
    def test_suppress_5h_critical_when_reset_imminent(self):
        """5h at 95% but resets in 5 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=resets,
        ) is False

    def test_swap_5h_critical_when_reset_far(self):
        """5h at 95% and resets in 3 hours -> swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=resets,
        ) is True

    def test_suppress_7d_threshold_when_reset_imminent(self):
        """7d at 90% but resets in 5 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert should_swap(
            usage_5h=50, usage_7d=90,
            resets_7d_at=resets,
        ) is False

    def test_suppress_burn_rate_when_reset_imminent(self):
        """Burn rate projects critical but 5h resets in 3 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        br = BurnRate(rate_5h_per_min=5.0, last_check_5h=82.0)
        assert should_swap(
            usage_5h=82, usage_7d=0,
            burn_rate=br,
            resets_5h_at=resets,
        ) is False

    def test_no_suppression_without_reset_data(self):
        """No resets_at data -> normal behavior, swap on critical."""
        assert should_swap(usage_5h=95, usage_7d=0) is True

    def test_stale_data_guard(self):
        """Reset is past but usage_cached_at is older than reset -> suppress."""
        from datetime import datetime, timezone, timedelta
        import time as _time
        reset_time = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        cached_at = int(_time.time()) - 300
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=reset_time,
            usage_cached_at=cached_at,
        ) is False

    def test_stale_guard_not_triggered_when_data_fresh(self):
        """Reset is past but usage_cached_at is AFTER reset -> normal behavior."""
        from datetime import datetime, timezone, timedelta
        import time as _time
        reset_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        cached_at = int(_time.time()) - 60  # cached 1 min ago, after the reset
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=reset_time,
            usage_cached_at=cached_at,
        ) is True


# ---------------------------------------------------------------------------
# score_candidate — reset proximity bonus
# ---------------------------------------------------------------------------

class TestScoreStaleness:
    def test_stale_data_reduces_score(self):
        """Account with usage_cached_at > 30 min old should score lower."""
        import time as _time
        fresh = _acct(1, usage_5h=20)
        fresh["usage_cached_at"] = int(_time.time()) - 60  # 1 min ago
        stale = _acct(2, usage_5h=20)
        stale["usage_cached_at"] = int(_time.time()) - 3600  # 1 hour ago
        assert score_candidate(fresh) > score_candidate(stale)

    def test_stale_data_kills_reset_bonus(self):
        """Imminent reset bonus should be 0 when data is stale."""
        import time as _time
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        acct = _acct(1, usage_5h=80, resets_5h=resets_soon)
        acct["usage_cached_at"] = int(_time.time()) - 3600  # stale
        no_reset = _acct(2, usage_5h=80, resets_5h=resets_soon)
        no_reset["usage_cached_at"] = int(_time.time()) - 3600
        # With stale data, the reset bonus should be killed
        assert abs(score_candidate(acct) - score_candidate(no_reset)) < 5


class TestScoreResetBonus:
    def test_imminent_reset_gets_bonus(self):
        """Account with 5h reset in 3 min should score higher than one without."""
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        a = _acct(1, usage_5h=80, resets_5h=resets_soon)
        b = _acct(2, usage_5h=80)
        assert score_candidate(a) > score_candidate(b)

    def test_no_bonus_beyond_15_min(self):
        """Account with reset in 20 min should NOT get the bonus."""
        from datetime import datetime, timezone, timedelta
        resets_far = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        resets_far2 = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        a = _acct(1, usage_5h=80, resets_5h=resets_far)
        b = _acct(2, usage_5h=80, resets_5h=resets_far2)
        assert abs(score_candidate(a) - score_candidate(b)) < 2


# ---------------------------------------------------------------------------
# score_candidate — 7d deficit bonus
# ---------------------------------------------------------------------------

class TestScoreDeficitBonus:
    def test_behind_schedule_account_scores_higher(self):
        """Account behind on 7d schedule should score higher than one ahead."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Account behind schedule: 20% usage, resets in 3 days (~57% through window)
        # Deficit: ~57% expected - 20% actual = ~37%
        behind = _acct(1, usage_5h=30, usage_7d=20)
        behind["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        behind["usage_cached_at"] = int(_time.time()) - 60

        # Account ahead of schedule: 80% usage, resets in 5 days (~29% through)
        # Deficit: ~29% expected - 80% actual = ~-51% (negative, ahead)
        ahead = _acct(2, usage_5h=30, usage_7d=80)
        ahead["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        ahead["usage_cached_at"] = int(_time.time()) - 60

        score_behind = score_candidate(behind, active_start="07:00", active_end="22:00")
        score_ahead = score_candidate(ahead, active_start="07:00", active_end="22:00")
        assert score_behind > score_ahead

    def test_deficit_bonus_is_proportional(self):
        """Larger deficit should give a larger bonus."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Account A: 10% 7d, resets in 1 day (~86% through window)
        # Deficit: ~86% expected - 10% actual = ~76% -> bonus ~38
        a = _acct(1, usage_5h=20, usage_7d=10)
        a["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        a["usage_cached_at"] = int(_time.time()) - 60

        # Account B: 10% 7d, resets in 5 days (~29% through window)
        # Deficit: ~29% expected - 10% actual = ~19% -> bonus ~9.5
        b = _acct(2, usage_5h=20, usage_7d=10)
        b["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        b["usage_cached_at"] = int(_time.time()) - 60

        score_a = score_candidate(a, active_start="07:00", active_end="22:00")
        score_b = score_candidate(b, active_start="07:00", active_end="22:00")
        assert score_a > score_b

    def test_no_bonus_when_ahead_of_schedule(self):
        """Account ahead of schedule gets no deficit bonus (deficit <= 0)."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Two accounts with SAME 7d reset data so the existing time-weighted
        # 7d penalty is identical, isolating the deficit bonus effect.
        # 80% usage, 29% through window -> deficit = -51% (ahead)
        acct = _acct(1, usage_5h=20, usage_7d=80)
        acct["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        acct["usage_cached_at"] = int(_time.time()) - 60

        # 10% usage, same reset -> deficit positive, WILL get bonus
        behind = _acct(2, usage_5h=20, usage_7d=10)
        behind["cached_7d_resets_at"] = acct["cached_7d_resets_at"]
        behind["usage_cached_at"] = int(_time.time()) - 60

        score_ahead = score_candidate(acct, active_start="07:00", active_end="22:00")
        score_behind = score_candidate(behind, active_start="07:00", active_end="22:00")

        # The behind account gets both: less 7d penalty (10 vs 80) AND
        # a deficit bonus. The ahead account gets NO deficit bonus.
        # Verify ahead account doesn't somehow score higher from deficit.
        assert score_behind > score_ahead

        # Also verify: two ahead-of-schedule accounts with same params
        # produce the same score (no spurious bonus).
        acct2 = _acct(3, usage_5h=20, usage_7d=80)
        acct2["cached_7d_resets_at"] = acct["cached_7d_resets_at"]
        acct2["usage_cached_at"] = int(_time.time()) - 60
        assert abs(
            score_candidate(acct, active_start="07:00", active_end="22:00")
            - score_candidate(acct2, active_start="07:00", active_end="22:00")
        ) < 1

    def test_default_active_hours_backward_compatible(self):
        """Calling score_candidate without active hours still works."""
        acct = _acct(1, usage_5h=20, usage_7d=10)
        score = score_candidate(acct)
        assert score > 0


# ---------------------------------------------------------------------------
# pick_best_target — relax 7d filter for imminent resets
# ---------------------------------------------------------------------------

class TestPickTargetResetRelax:
    def test_7d_over_threshold_but_reset_imminent_not_excluded(self):
        """Account over 7d threshold but resetting in 5 min -> still a candidate."""
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=10, usage_7d=90),
        ]
        accounts[1]["cached_7d_resets_at"] = resets_soon
        result = pick_best_target(accounts, current_id=1)
        assert result is not None
        assert result["id"] == 2

    def test_7d_over_threshold_reset_far_still_excluded(self):
        """Account over 7d threshold with reset far out -> still excluded."""
        from datetime import datetime, timezone, timedelta
        resets_far = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=10, usage_7d=90),
        ]
        accounts[1]["cached_7d_resets_at"] = resets_far
        result = pick_best_target(accounts, current_id=1)
        assert result is None


# ---------------------------------------------------------------------------
# pick_best_target — urgency-based 7d filter relaxation
# ---------------------------------------------------------------------------

class TestPickTargetUrgencyRelax:
    def test_behind_schedule_near_expiry_not_filtered(self):
        """Account at 85% 7d, behind schedule, <24h remaining -> passes filter."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=85),  # high 7d, expiring
        ]
        # 12 hours until reset, ~93% through window, deficit ~8%
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is not None
        assert result["id"] == 2

    def test_behind_schedule_far_from_expiry_still_filtered(self):
        """Account at 86% 7d, behind schedule, but 3 days left -> still filtered."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=86),  # 86% usage
        ]
        # Resets in 3 days (~57% through window). Deficit = ~57% - 86% = -29%
        # Deficit is negative (ahead of schedule). Even if it were positive,
        # 3 days = ~45 working hours >> 24h urgency threshold.
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is None

    def test_ahead_of_schedule_near_expiry_still_filtered(self):
        """Account at 95% 7d, AHEAD of schedule, <24h remaining -> filtered."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=95),
        ]
        # 12 hours until reset, ~93% through window, deficit = 93% - 95% = -2%
        # Negative deficit = ahead of schedule. No urgency relaxation.
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is None

    def test_end_to_end_original_bug_scenario(self):
        """THE original bug: Account 3 at 85% 7d with 12h left should beat Account 2 at 18% 7d."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90, usage_7d=85),  # current (active, triggers swap)
            _acct(2, usage_5h=74, usage_7d=18),   # low 7d, 6.8 days left
            _acct(3, usage_5h=5, usage_7d=85),     # high 7d, 0.5 days left
        ]
        # Account 2: resets in 6.8 days, ahead of schedule
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=6, hours=19)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        # Account 3: resets in 12 hours, behind schedule
        accounts[2]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[2]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is not None
        # Account 3 should win: low 5h (5%), urgency-relaxed filter, deficit bonus
        assert result["id"] == 3

    def test_default_active_hours_backward_compatible(self):
        """Calling pick_best_target without active hours still works."""
        accounts = [_acct(1, usage_5h=90), _acct(2, usage_5h=10)]
        result = pick_best_target(accounts, current_id=1)
        assert result is not None
        assert result["id"] == 2


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
