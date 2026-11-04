"""A provider reset that is not a timestamp is stored as no reset.

dcr wave 2 (F1): the cache stored a reset value verbatim, so "" reached the
window keeper, which read it as "no reset" in one place and as a lapsed reset
in another, and pinged on every sweep.
"""

import logging

import pytest

from jacked.web import auth


@pytest.fixture(autouse=True)
def _fresh_log_state(monkeypatch):
    monkeypatch.setattr(auth, "_UNREADABLE_LOGGED", set())


@pytest.mark.parametrize("value", ["", "   ", "soon", {}, [], True, float("nan")])
def test_an_unreadable_reset_is_stored_as_no_reset(value):
    updates = auth.usage_cache_updates({"seven_day": {"utilization": 41.0, "resets_at": value}})
    assert updates == {"seven_day": 41.0, "clear_seven_day_reset": True}


def test_a_reset_that_parses_is_kept():
    iso = "2026-11-08T19:59:59.844473+00:00"
    updates = auth.usage_cache_updates({"five_hour": {"utilization": 3, "resets_at": iso}})
    assert updates["five_hour_resets_at"] == iso
    updates = auth.usage_cache_updates({"five_hour": {"utilization": 3, "resets_at": 1774933200}})
    assert updates["five_hour_resets_at"] == "2026-03-31T05:00:00+00:00"


def test_a_blank_reset_with_no_reading_clears_the_window():
    assert auth.usage_cache_updates({"seven_day": {"utilization": None, "resets_at": ""}}) == {
        "clear_seven_day": True
    }


def test_each_unreadable_shape_is_logged_once_without_its_value(caplog):
    with caplog.at_level(logging.WARNING, logger="jacked.auth"):
        for _ in range(3):
            auth.usage_cache_updates({"seven_day": {"utilization": 1, "resets_at": "x-secret"}})
    messages = [r.getMessage() for r in caplog.records if "not a timestamp" in r.getMessage()]
    assert len(messages) == 1
    assert "seven_day" in messages[0] and "str" in messages[0]
    assert "x-secret" not in messages[0]
