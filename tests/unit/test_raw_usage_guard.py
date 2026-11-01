"""The stored raw usage payload is never older than the readings beside it.

``update_account_usage_cache`` used to skip a raw payload over 10240
characters without a word, while the percentages and ``usage_cached_at``
still updated. The plan resolver reads the stored payload, so an old
"no session window" list could then stand as evidence against fresh numbers
that say otherwise. Now the bound is generous (a guard against a runaway
payload; live payloads are 1.2-2.8 KB), a payload over it clears the stored
one in the same write, and the drop is logged with the account and the size.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from jacked import usage_plans
from jacked.web import database as database_module
from jacked.web.database import Database

ULTRA_TIER = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}

# Verified Ultra: an authoritative limits list with no session entry.
NO_SESSION_RAW = {
    "five_hour": None,
    "seven_day": {"utilization": 30.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 30}],
}


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch):
    monkeypatch.setattr(usage_plans, "_catalog_cache", None)
    monkeypatch.setattr(usage_plans, "_drift_logged", set())


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jacked.db"))
    yield database
    database.close()


@pytest.fixture
def acct(db):
    return db.create_account("u@x.com", "tok", int(time.time()) + 3600, **ULTRA_TIER)


def _padded(size: int, **payload) -> dict:
    """A payload whose JSON text is exactly *size* characters."""
    payload = dict(payload)
    payload["padding"] = ""
    base = len(json.dumps(payload))
    payload["padding"] = "x" * (size - base)
    assert len(json.dumps(payload)) == size
    return payload


def test_the_bound_is_256_kib():
    assert database_module._RAW_USAGE_MAX_CHARS == 256 * 1024


def test_a_12_kb_payload_is_stored(db, acct):
    payload = _padded(12 * 1024, limits=[{"kind": "weekly_all", "percent": 4}])
    assert db.update_account_usage_cache(acct["id"], seven_day=4.0, raw=payload)
    assert json.loads(db.get_account(acct["id"])["cached_usage_raw"]) == payload


def test_a_payload_at_the_bound_is_stored(db, acct):
    payload = _padded(database_module._RAW_USAGE_MAX_CHARS, limits=[])
    db.update_account_usage_cache(acct["id"], raw=payload)
    assert db.get_account(acct["id"])["cached_usage_raw"] is not None


def test_an_oversize_payload_clears_the_stored_one_and_warns(db, acct, caplog):
    db.update_account_usage_cache(acct["id"], seven_day=30.0, raw=NO_SESSION_RAW)
    assert json.loads(db.get_account(acct["id"])["cached_usage_raw"]) == NO_SESSION_RAW

    huge = _padded(300 * 1024, five_hour={"utilization": 52.0})
    with caplog.at_level(logging.WARNING, logger="jacked.web.database"):
        assert db.update_account_usage_cache(
            acct["id"], five_hour=52.0, seven_day=31.0, raw=huge,
        )

    row = db.get_account(acct["id"])
    assert row["cached_usage_raw"] is None
    # The readings of the same write still land.
    assert (row["cached_usage_5h"], row["cached_usage_7d"]) == (52.0, 31.0)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"account {acct['id']}" in message
    assert str(300 * 1024) in message
    assert "xxxx" not in message and "utilization" not in message


def test_one_character_over_the_bound_is_cleared(db, acct):
    db.update_account_usage_cache(acct["id"], raw=NO_SESSION_RAW)
    over = _padded(database_module._RAW_USAGE_MAX_CHARS + 1, limits=[])
    db.update_account_usage_cache(acct["id"], raw=over)
    assert db.get_account(acct["id"])["cached_usage_raw"] is None


def test_the_resolver_never_reads_an_old_payload_against_new_readings(db, acct):
    """The reason for the change: the old "no session" list must not veto a
    5h reading that a newer (oversize) payload carried."""
    db.update_account_usage_cache(acct["id"], seven_day=30.0, raw=NO_SESSION_RAW,
                                  clear_five_hour=True)
    assert usage_plans.account_view(db.get_account(acct["id"])).session.state == "absent"

    huge = _padded(300 * 1024, five_hour={"utilization": 52.0},
                   limits=[{"kind": "session", "percent": 52}])
    db.update_account_usage_cache(acct["id"], five_hour=52.0, raw=huge)

    view = usage_plans.account_view(db.get_account(acct["id"]))
    assert view.session.state == "present"
    assert view.five_hour_reading() == 52.0


def test_no_raw_keeps_the_stored_payload(db, acct):
    db.update_account_usage_cache(acct["id"], raw=NO_SESSION_RAW)
    db.update_account_usage_cache(acct["id"], seven_day=12.0)
    assert json.loads(db.get_account(acct["id"])["cached_usage_raw"]) == NO_SESSION_RAW
