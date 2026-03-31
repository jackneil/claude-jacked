"""Tests for the auto-swap decision engine (pure functions, no I/O)."""

import time

import pytest

from jacked.web.auto_swap import (
    BurnRate,
    pick_best_target,
    score_candidate,
    should_swap,
    update_burn_rate,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _acct(id, usage_5h=0, usage_7d=0, cc_token=True, active=True,
          failures=0, valid=True, auto_swap=True, resets_5h=None):
    return {
        "id": id, "email": f"user{id}@test.com",
        "cached_usage_5h": usage_5h, "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_5h,
        "cc_access_token": "tok" if cc_token else None,
        "is_active": 1 if active else 0, "is_deleted": 0,
        "consecutive_failures": failures,
        "validation_status": "valid" if valid else "invalid",
        "auto_swap_enabled": 1 if auto_swap else 0,
        "priority": id - 1, "access_token": f"at_{id}",
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
        with_reset = score_candidate(_acct(1, usage_5h=20, usage_7d=10,
                                           resets_5h=time.time() + 3600))
        without_reset = score_candidate(_acct(2, usage_5h=20, usage_7d=10,
                                              resets_5h=None))
        assert without_reset == with_reset + 15

    def test_score_handles_none_usage(self):
        acct = _acct(1)
        acct["cached_usage_5h"] = None
        acct["cached_usage_7d"] = None
        # 100 - 0 - 0 + 15 (resets_5h=None -> inactive bonus)
        assert score_candidate(acct) == 115


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
