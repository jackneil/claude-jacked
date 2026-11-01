"""The menu-bar panel's next-refresh estimate is plan-aware.

When the active-poll loop has not published its schedule yet, the panel
estimates the next refresh from compute_urgency_tier. That estimate must
read the account the way the loop does: an account without a 5-hour window
(a verified Ultra) follows its weekly and model-weekly windows, and a stale
5h reading left in its cache never pulls the estimate to critical.
"""

import json
from types import SimpleNamespace

from jacked.api.routes.menubar import _attach_next_refresh

CACHED_AT = 1_700_000_000


def _ultra_row(*, usage_5h=100.0, usage_7d=40.0, model_pct=10):
    return {
        "id": 1,
        "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "cached_usage_5h": usage_5h,
        "cached_5h_resets_at": "2099-01-01T00:00:00Z",
        "cached_usage_7d": usage_7d,
        "usage_cached_at": CACHED_AT,
        "cached_usage_raw": json.dumps({
            "five_hour": None,
            "limits": [
                {"kind": "weekly_all", "percent": usage_7d},
                {"kind": "weekly_scoped", "percent": model_pct,
                 "scope": {"model": {"display_name": "Fable"}}},
            ],
        }),
    }


def _estimate(row):
    active = {"account_id": row["id"]}
    _attach_next_refresh(active, [row], SimpleNamespace())  # no published schedule
    return active


def test_a_stale_5h_on_a_plan_without_the_window_does_not_raise_the_estimate():
    active = _estimate(_ultra_row(usage_5h=100.0))
    assert active["poll_tier"] == "idle"
    assert active["poll_interval"] == 300
    assert active["next_refresh_at"] == CACHED_AT + 300


def test_the_estimate_follows_the_model_weekly_limit():
    active = _estimate(_ultra_row(usage_5h=None, model_pct=96))
    assert active["poll_tier"] == "critical"
    assert active["next_refresh_at"] == CACHED_AT + 180


def test_an_account_with_a_5h_window_keeps_the_5h_rules():
    row = {
        "id": 1, "subscription_type": "pro", "cached_usage_5h": 88.0,
        "cached_usage_7d": 30.0, "usage_cached_at": CACHED_AT,
    }
    active = _estimate(row)
    assert active["poll_tier"] == "critical"
    assert active["poll_interval"] == 180
