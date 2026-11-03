"""The "Swap on the 5-hour window" toggle on the swap-settings API (#224i).

The setting ``auto_swap_5h_rules_enabled`` is stored as the strings
"true"/"false"; a missing row means false, the settled default (jacked leaves
an account only at its weekly or model wall). The monitor reads the setting
(tests/unit/test_usage_monitor.py); this module covers the route that exposes
it, the PUT rule that a save without ``auto_swap_paused_until`` keeps an
active pause, and the retired ``auto_swap_7d_threshold`` field (an old client
can still send it; the route ignores it).

Driven through the real FastAPI app with a plain ``TestClient(app)`` that
never enters the context manager, so the lifespan (background sweeps, real
home dir) never runs. The PUT wakes the sweep loop; the wake is replaced with
a recorder so the module's asyncio.Event is never touched.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jacked.api.usage_monitor as usage_monitor
from jacked.api.main import app
from jacked.web.database import Database

KEY = "auto_swap_5h_rules_enabled"
URL = "/api/settings/swap-settings"
PAUSE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    """A throwaway file-backed settings DB wired onto app.state for the test.

    File-backed, not ``:memory:``: TestClient serves handlers on a worker
    thread and Database keeps a thread-local connection, so a ``:memory:``
    DB would be a fresh, schema-less DB on the handler thread."""
    database = Database(str(tmp_path / "jacked.db"))
    prev = getattr(app.state, "db", None)
    app.state.db = database
    yield database
    if prev is not None:
        app.state.db = prev
    else:
        try:
            del app.state.db
        except AttributeError:
            pass


@pytest.fixture
def woken(monkeypatch):
    calls = []
    monkeypatch.setattr(usage_monitor, "_sweep_wake", SimpleNamespace(set=lambda: calls.append(1)))
    return calls


@pytest.fixture
def client(db, woken):
    return TestClient(app)


def test_get_reads_off_when_the_setting_is_absent(client, db):
    assert db.get_setting(KEY) is None
    resp = client.get(URL)
    assert resp.status_code == 200
    assert resp.json()[KEY] is False


def test_put_true_persists_the_string_true_and_get_reads_it_back(client, db, woken):
    resp = client.put(URL, json={KEY: True})
    assert resp.status_code == 200
    assert resp.json()[KEY] is True
    assert db.get_setting(KEY) == "true"
    assert client.get(URL).json()[KEY] is True
    assert woken == [1]


def test_put_false_persists_the_string_false(client, db):
    db.set_setting(KEY, "true")
    resp = client.put(URL, json={KEY: False})
    assert resp.status_code == 200
    assert db.get_setting(KEY) == "false"
    assert client.get(URL).json()[KEY] is False


@pytest.mark.parametrize("key", ["auto_swap_enabled", KEY, "window_keeper_enabled"])
@pytest.mark.parametrize("stored", ["true", "True", "1", "yes", "false", "", "0", "no"])
def test_get_reads_each_toggle_the_way_the_monitor_does(client, db, key, stored):
    """The dashboard must show what the auto-swap tick and the window keeper
    will do, so GET reads each stored boolean through the monitor's reader."""
    db.set_setting(key, stored)
    assert client.get(URL).json()[key] is usage_monitor._setting_bool(db, key, False)


def test_put_without_the_pause_keeps_an_active_pause(client, db):
    """The dashboard form never sends ``auto_swap_paused_until``: the pause has
    its own endpoints, and a manual switch arms a 15-minute hold. A settings
    save (a flip of the 5-hour toggle, a slider) must not clear that hold, or
    auto-swap moves off the account the user just chose."""
    db.set_setting("auto_swap_paused_until", PAUSE)
    resp = client.put(URL, json={KEY: True})
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == PAUSE
    assert client.get(URL).json()["auto_swap_paused_until"] == PAUSE
    # The response says what is now stored, so it reports the kept pause.
    assert resp.json()["auto_swap_paused_until"] == PAUSE


@pytest.mark.parametrize("sent", [None, ""])
def test_put_that_sends_an_empty_pause_clears_it(client, db, sent):
    """A client that sends the field controls it: the menu bar sends back what
    GET returned, so a GET with no pause, then a PUT, clears the pause."""
    db.set_setting("auto_swap_paused_until", PAUSE)
    resp = client.put(URL, json={"auto_swap_paused_until": sent})
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == ""
    assert client.get(URL).json()["auto_swap_paused_until"] is None
    assert resp.json()["auto_swap_paused_until"] is None


def test_put_that_sends_a_pause_stores_it(client, db):
    resp = client.put(URL, json={"auto_swap_paused_until": PAUSE})
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == PAUSE
    assert resp.json()["auto_swap_paused_until"] == PAUSE


# The retired "7-Day Threshold" slider: no decision code read it, so the
# swap-settings API no longer has the field. A dashboard page or a menu bar
# from before the removal still sends it, and its save must still work.
RETIRED_7D = "auto_swap_7d_threshold"


@pytest.mark.parametrize("sent", [70, 150, "junk"])
def test_put_with_the_retired_7d_field_saves_and_does_not_store_it(client, db, sent):
    resp = client.put(URL, json={RETIRED_7D: sent, KEY: True})
    assert resp.status_code == 200
    assert db.get_setting(KEY) == "true"
    assert db.get_setting(RETIRED_7D) is None
    assert RETIRED_7D not in resp.json()


def test_get_does_not_report_a_stored_7d_row(client, db):
    db.set_setting(RETIRED_7D, "70")  # a row that an old jacked wrote
    resp = client.get(URL)
    assert resp.status_code == 200
    assert RETIRED_7D not in resp.json()
