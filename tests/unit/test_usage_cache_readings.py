"""A provider utilization that is not a finite number is stored as no reading.

#231i: Python's json reads 1e999 as infinity, and NaN as NaN. The Claude usage
mapping stored them in ``cached_usage_5h`` and ``cached_usage_7d``. There an
infinite weekly reading read as a wall, ``jacked usage --json`` printed
``Infinity``, and the usage poll could not send its WebSocket message. The
mapping now reads the utilization with the one reader of every usage percent
(:func:`jacked.service.pill_text.percent_reading`). A value that is not a
finite number then follows the rules of a null utilization.
"""

import json
import logging
import time

import pytest

from jacked.web import auth
from jacked.web.database import Database

RESET = "2026-11-08T00:00:00+00:00"

VALUES = [
    pytest.param(json.loads("1e999"), None, id="1e999"),
    pytest.param(float("nan"), None, id="nan"),
    pytest.param("Infinity", None, id="Infinity-string"),
    pytest.param(float("-inf"), None, id="-inf"),
    pytest.param("92", 92.0, id="numeric-string"),
]


@pytest.fixture(autouse=True)
def _fresh_log_state(monkeypatch):
    monkeypatch.setattr(auth, "_UNREADABLE_LOGGED", set())


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


@pytest.mark.parametrize("value,expected", VALUES)
@pytest.mark.parametrize(
    "window,column", [("five_hour", "cached_usage_5h"), ("seven_day", "cached_usage_7d")]
)
def test_a_utilization_that_is_not_a_finite_number_is_no_reading(
    db, window, column, value, expected
):
    updates = auth.usage_cache_updates({window: {"utilization": value, "resets_at": RESET}})
    assert updates.get(window) == expected
    assert updates[f"{window}_resets_at"] == RESET  # the reset is still real
    json.dumps(updates, allow_nan=False)
    # Through the real cache writer: the column keeps its last finite reading.
    acct = db.create_account("u@x.com", "tok", int(time.time()) + 3600)
    db.update_account_usage_cache(acct["id"], **{window: 40.0})
    db.update_account_usage_cache(acct["id"], **updates)
    assert db.get_account(acct["id"])[column] == (40.0 if expected is None else expected)


def test_an_unreadable_utilization_with_no_reset_clears_the_window():
    # The rule of a null utilization with no reset: no window is open.
    assert auth.usage_cache_updates(
        {"seven_day": {"utilization": float("nan"), "resets_at": None}}
    ) == {"clear_seven_day": True}


def test_an_unreadable_utilization_is_logged_once_without_its_value(caplog):
    with caplog.at_level(logging.WARNING, logger="jacked.auth"):
        for _ in range(3):
            auth.usage_cache_updates(
                {"seven_day": {"utilization": "Infinity", "resets_at": RESET}}
            )
            auth.usage_cache_updates({"seven_day": {"utilization": None, "resets_at": RESET}})
    messages = [r.getMessage() for r in caplog.records if "not a finite number" in r.getMessage()]
    assert len(messages) == 1  # once, and a null utilization is no anomaly
    assert "seven_day" in messages[0] and "str" in messages[0]
    assert "Infinity" not in messages[0]
