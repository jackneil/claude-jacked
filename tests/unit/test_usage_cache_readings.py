"""A usage reset travels only with its percent.

#231i: Python's json reads 1e999 as infinity, and NaN as NaN. The Claude usage
mapping stored them in ``cached_usage_5h`` and ``cached_usage_7d``. There an
infinite weekly reading read as a wall, ``jacked usage --json`` printed
``Infinity``, and the usage poll could not send its WebSocket message. The
mapping reads the utilization with the one reader of every usage percent
(:func:`jacked.service.pill_text.percent_reading`).

M6 review wave 1 (W1): the first fix gave a rejected percent the rules of a
null utilization. A payload with a rejected percent and a valid reset then
wrote the new reset beside the old percent. After a weekly rollover the cache
paired 100% of the expired week with the reset of the next week, and the
walls read "weekly critical". A null utilization (the provider reports no
reading) with a valid reset did the same before M6. Now a reset travels only
with its percent: a window with a reset but no percent writes nothing, and
the cache keeps the last percent and the last reset of that window together.
A rejected percent makes the window unreadable, also when it has no reset. A
null percent with no reset still clears the window: no window is open.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web import auth
from jacked.web.auto_swap.walls import walled_reason
from jacked.web.database import Database

OLD_RESET = "2026-11-01T00:00:00+00:00"
NEW_RESET = "2026-11-08T00:00:00+00:00"

REJECTED = [
    pytest.param(json.loads("1e999"), id="1e999"),
    pytest.param(float("nan"), id="nan"),
    pytest.param("Infinity", id="Infinity-string"),
    pytest.param(float("-inf"), id="-inf"),
]

# No percent: a rejected value, or a null (the provider reports no reading).
NO_PERCENT = [*REJECTED, pytest.param(None, id="null")]

WINDOWS = [
    pytest.param("five_hour", "cached_usage_5h", "cached_5h_resets_at", id="5h"),
    pytest.param("seven_day", "cached_usage_7d", "cached_7d_resets_at", id="7d"),
]


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch):
    monkeypatch.setattr(auth, "_UNREADABLE_LOGGED", set())
    monkeypatch.setattr(auth, "_account_usage_state", {})
    monkeypatch.setattr(auth, "_profile_refreshed_at", {})


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


def _account(db, **cache) -> int:
    """A Claude account whose usage cache holds *cache*."""
    acct = db.create_account("u@x.com", "tok", int(time.time()) + 3600)
    db.update_account_usage_cache(acct["id"], **cache)
    return acct["id"]


def _store_read(db, account_id: int, payload: dict) -> dict:
    """Store *payload* the way production does, and return the account row.

    One real ``fetch_usage`` call gets a 200 reply that carries *payload*,
    and the real cache writer stores it. The test first moves the last read
    one hour back, so that the pacing floor lets the call through.
    """
    db.update_account(account_id, usage_cached_at=int(time.time()) - 3600)
    auth._account_usage_state.pop(account_id, None)
    reply = MagicMock(status_code=200, headers={})
    reply.json.return_value = payload
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=reply)
    with patch.object(auth.httpx, "AsyncClient", return_value=client), patch.object(
        auth, "fetch_profile", new=AsyncMock()
    ):
        assert asyncio.run(auth.fetch_usage(account_id, db)) is payload
    client.get.assert_awaited_once()
    return db.get_account(account_id)


@pytest.mark.parametrize("value", NO_PERCENT)
def test_a_weekly_read_with_no_percent_never_pairs_the_old_week_with_the_next_reset(
    db, value
):
    """The W1 regression: a rollover read with no percent is no wall."""
    now = datetime.now(timezone.utc)
    expired = (now - timedelta(hours=1)).isoformat()
    next_week = (now + timedelta(days=7)).isoformat()
    aid = _account(db, seven_day=100.0, seven_day_resets_at=expired)
    assert walled_reason(db.get_account(aid), now) is None  # that week is over

    row = _store_read(db, aid, {"seven_day": {"utilization": value, "resets_at": next_week}})

    assert (row["cached_usage_7d"], row["cached_7d_resets_at"]) == (100.0, expired)
    assert walled_reason(row, now) is None
    # Not vacuous: the old percent beside the next reset is a wall.
    assert walled_reason({**row, "cached_7d_resets_at": next_week}, now).startswith(
        "weekly critical"
    )


@pytest.mark.parametrize("value", NO_PERCENT)
@pytest.mark.parametrize("window,column,reset_column", WINDOWS)
def test_a_reset_with_no_percent_writes_nothing_for_its_window(
    db, window, column, reset_column, value
):
    payload = {window: {"utilization": value, "resets_at": NEW_RESET}}
    assert auth.usage_cache_updates(payload) == {}
    aid = _account(db, **{window: 40.0, f"{window}_resets_at": OLD_RESET})

    row = _store_read(db, aid, payload)

    assert (row[column], row[reset_column]) == (40.0, OLD_RESET)


@pytest.mark.parametrize("reset", [None, "", "soon"])
def test_a_rejected_percent_keeps_its_window_whatever_the_reset(reset):
    # A null percent with no reset clears the window: no window is open. A
    # rejected percent says nothing about the window, so nothing is written.
    payload = {"seven_day": {"utilization": float("nan"), "resets_at": reset}}
    assert auth.usage_cache_updates(payload) == {}


@pytest.mark.parametrize("reset", [None, "", "soon"])
def test_a_null_percent_with_no_reset_is_no_active_window(db, reset):
    """A null percent with no usable reset clears the window, and so does a
    null window object: in both, no window is open."""
    payload = {"seven_day": {"utilization": None, "resets_at": reset}}
    assert auth.usage_cache_updates(payload) == {"clear_seven_day": True}
    assert auth.usage_cache_updates({"seven_day": None}) == {"clear_seven_day": True}
    aid = _account(db, seven_day=40.0, seven_day_resets_at=OLD_RESET)

    row = _store_read(db, aid, payload)

    assert (row["cached_usage_7d"], row["cached_7d_resets_at"]) == (None, None)


@pytest.mark.parametrize(
    "value", [pytest.param(float("nan"), id="nan"), pytest.param(None, id="null")]
)
def test_a_five_hour_read_with_no_percent_beside_a_good_weekly_window(db, value):
    aid = _account(
        db,
        five_hour=100.0,
        five_hour_resets_at=OLD_RESET,
        seven_day=40.0,
        seven_day_resets_at=OLD_RESET,
    )

    row = _store_read(
        db,
        aid,
        {
            "five_hour": {"utilization": value, "resets_at": NEW_RESET},
            "seven_day": {"utilization": 41.0, "resets_at": NEW_RESET},
        },
    )

    assert (row["cached_usage_5h"], row["cached_5h_resets_at"]) == (100.0, OLD_RESET)
    assert (row["cached_usage_7d"], row["cached_7d_resets_at"]) == (41.0, NEW_RESET)


def test_a_numeric_string_percent_is_a_reading():
    updates = auth.usage_cache_updates(
        {"seven_day": {"utilization": "92", "resets_at": NEW_RESET}}
    )
    assert updates == {"seven_day": 92.0, "seven_day_resets_at": NEW_RESET}


def test_a_rejected_percent_is_logged_once_without_its_value(caplog):
    with caplog.at_level(logging.WARNING, logger="jacked.auth"):
        for _ in range(3):
            auth.usage_cache_updates(
                {"seven_day": {"utilization": "Infinity", "resets_at": "soon"}}
            )
            auth.usage_cache_updates({"seven_day": {"utilization": None, "resets_at": NEW_RESET}})
    lines = [r.getMessage() for r in caplog.records]
    percent_lines = [m for m in lines if "not a finite number" in m]
    assert len(percent_lines) == 1  # once, and a null utilization is no anomaly
    assert "seven_day" in percent_lines[0] and "str" in percent_lines[0]
    assert "Infinity" not in percent_lines[0]
    assert "keeps the last reading and reset" in percent_lines[0]
    # The reset of an unreadable window is not read, so no line says that
    # the cache stores it as no reset.
    assert not [m for m in lines if "not a timestamp" in m]


# ---------------------------------------------------------------------------
# M6 review wave 2 (X3): each window has its own read stamp. A rejected or
# kept window still advanced usage_cached_at (one stamp per row), and the
# window keeper read that stamp as evidence that a reset-less weekly window
# was open. cached_5h_read_at and cached_7d_read_at move only when the write
# ACCEPTED that window's reading: its percent was written, or the window was
# cleared. usage_cached_at stays the row's display age.
# ---------------------------------------------------------------------------

OLD_STAMP = 1_000.0


def _stamps(row: dict) -> tuple:
    return row["cached_5h_read_at"], row["cached_7d_read_at"]


@pytest.mark.parametrize("value", NO_PERCENT)
def test_a_weekly_reading_that_was_not_accepted_keeps_its_read_stamp(db, value):
    """X3: a rejected weekly percent (or none, beside a reset) beside a good
    5h reading moves the row stamp and the 5h stamp, never the 7d stamp."""
    aid = _account(db, five_hour=10.0, seven_day=40.0, seven_day_resets_at=OLD_RESET)
    db.update_account(aid, cached_5h_read_at=OLD_STAMP, cached_7d_read_at=OLD_STAMP)
    before = time.time()

    row = _store_read(
        db,
        aid,
        {
            "five_hour": {"utilization": 12.0, "resets_at": NEW_RESET},
            "seven_day": {"utilization": value, "resets_at": NEW_RESET},
        },
    )

    assert row["usage_cached_at"] >= int(before)  # the display age moves
    assert row["cached_5h_read_at"] >= before
    assert row["cached_7d_read_at"] == OLD_STAMP
    assert (row["cached_usage_7d"], row["cached_7d_resets_at"]) == (40.0, OLD_RESET)


@pytest.mark.parametrize(
    "seven_day",
    [
        pytest.param(None, id="null-window"),
        pytest.param({"utilization": None, "resets_at": None}, id="null-percent-no-reset"),
    ],
)
def test_a_cleared_window_is_an_accepted_read(db, seven_day):
    """X3 (c): a null window, or a null percent with no reset, clears the
    window: no window is open. That is a reading, so the stamp moves."""
    aid = _account(db, seven_day=40.0, seven_day_resets_at=OLD_RESET)
    db.update_account(aid, cached_5h_read_at=OLD_STAMP, cached_7d_read_at=OLD_STAMP)
    before = time.time()

    row = _store_read(db, aid, {"seven_day": seven_day})

    assert (row["cached_usage_7d"], row["cached_7d_resets_at"]) == (None, None)
    assert row["cached_7d_read_at"] >= before
    assert row["cached_5h_read_at"] == OLD_STAMP  # a missing key keeps the 5h window


def test_the_cache_writer_stamps_only_the_windows_it_accepted(db):
    """X3 (e): the writer itself, without the payload mapping."""
    aid = _account(db)
    db.update_account(aid, cached_5h_read_at=OLD_STAMP, cached_7d_read_at=OLD_STAMP)
    before = time.time()

    db.update_account_usage_cache(aid, raw={"pad": 1})
    assert _stamps(db.get_account(aid)) == (OLD_STAMP, OLD_STAMP)
    db.update_account_usage_cache(aid, five_hour=3.0, five_hour_resets_at=NEW_RESET)
    five, seven = _stamps(db.get_account(aid))
    assert five >= before and seven == OLD_STAMP
    db.update_account(aid, cached_5h_read_at=OLD_STAMP)
    db.update_account_usage_cache(aid, clear_seven_day=True)
    five, seven = _stamps(db.get_account(aid))
    assert five == OLD_STAMP and seven >= before
