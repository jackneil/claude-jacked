"""The "Swap on the 5-hour window" toggle on the swap-settings API (#224i).

The setting ``auto_swap_5h_rules_enabled`` is stored as the strings
"true"/"false"; a missing row means false, the settled default (jacked does
not leave an account for its 5-hour window). The monitor reads the setting
(tests/unit/test_usage_monitor.py); this module covers the route that exposes
it, the PUT rule that a save without ``auto_swap_paused_until`` or without
the toggle keeps the stored value, and the retired ``auto_swap_7d_threshold``
field (an old client can still send it; the route ignores it).

Driven through the real FastAPI app with a plain ``TestClient(app)`` that
never enters the context manager, so the lifespan (background sweeps, real
home dir) never runs. The PUT wakes the sweep loop; the wake is replaced with
a recorder so the module's asyncio.Event is never touched.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jacked.api.keeper.state as keeper_state
import jacked.api.usage_monitor as usage_monitor
from jacked.api.main import app
from jacked.api.routes.settings_swap import MAX_PAUSE_MINUTES
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
    monkeypatch.setattr(keeper_state, "_sweep_wake", SimpleNamespace(set=lambda: calls.append(1)))
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
    assert resp.json()[KEY] is False
    assert db.get_setting(KEY) == "false"
    assert client.get(URL).json()[KEY] is False


# The exact save of the dashboard from before the toggle (ad0ad63): every
# field but the toggle, plus the retired 7-day slider, and no pause. A tab
# opened before the upgrade keeps sending this payload until it reloads.
PRE_TOGGLE_DASHBOARD_SAVE = {
    "auto_swap_enabled": True,
    "auto_swap_5h_warning": 80,
    "auto_swap_5h_critical": 90,
    "auto_swap_7d_threshold": 85,
    "usage_check_interval": 300,
    "window_keeper_enabled": False,
    "window_keeper_active_start": "06:00",
    "window_keeper_active_end": "23:00",
    "window_keeper_prewake": "04:00",
}


def test_put_from_a_page_that_predates_the_toggle_keeps_a_stored_true(client, db):
    """A save that does not send the toggle keeps the stored value. Before
    this rule the model default (False) was written, so a stale tab turned
    the toggle off without a word."""
    db.set_setting(KEY, "true")
    resp = client.put(URL, json=PRE_TOGGLE_DASHBOARD_SAVE)
    assert resp.status_code == 200
    assert db.get_setting(KEY) == "true"
    # The response reads the kept value back, so it shows what is in effect.
    assert resp.json()[KEY] is True
    assert client.get(URL).json()[KEY] is True
    # The fields that the old page did send are saved.
    assert db.get_setting("auto_swap_enabled") == "true"


def test_put_from_a_page_that_predates_the_toggle_writes_no_toggle_row(client, db):
    """With no stored row the toggle stays absent, which reads as off."""
    resp = client.put(URL, json=PRE_TOGGLE_DASHBOARD_SAVE)
    assert resp.status_code == 200
    assert db.get_setting(KEY) is None
    assert resp.json()[KEY] is False


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


def _in(minutes: float) -> str:
    """An ISO pause time, ``minutes`` from now (UTC)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_put_that_sends_a_pause_stores_it(client, db):
    pause = _in(30)
    resp = client.put(URL, json={"auto_swap_paused_until": pause})
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == pause
    assert resp.json()["auto_swap_paused_until"] == pause


# POST /swap-pause takes at most MAX_PAUSE_MINUTES (24 hours). A PUT that sets
# the pause itself gets the same cap: a pause that ends later is refused, so
# no client can turn auto-swap off for days with a "pause".
PAUSE_TOO_LONG = {
    "error": {
        "message": "A pause can end at most 1440 minutes (24 hours) from now.",
        "code": "PAUSE_TOO_LONG",
    }
}


@pytest.mark.parametrize("minutes", [MAX_PAUSE_MINUTES - 1, -60])
def test_put_with_a_pause_inside_the_cap_stores_it(client, db, minutes):
    """The cap has no lower end: a pause that already ended is no pause."""
    pause = _in(minutes)
    resp = client.put(URL, json={"auto_swap_paused_until": pause})
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == pause


@pytest.mark.parametrize("far", ["just_past_the_cap", "year_2999"])
def test_put_with_a_pause_past_the_cap_is_422_and_writes_nothing(client, db, woken, far):
    pause = _in(MAX_PAUSE_MINUTES + 1) if far == "just_past_the_cap" else PAUSE
    resp = client.put(URL, json={"auto_swap_paused_until": pause, KEY: True})
    assert resp.status_code == 422
    assert resp.json() == PAUSE_TOO_LONG
    assert "—" not in resp.json()["error"]["message"]
    # The save is refused as a whole: the toggle it sent is not written either.
    assert db.get_setting(KEY) is None
    assert db.get_setting("auto_swap_paused_until") is None
    assert woken == []


def test_put_with_a_pause_without_a_time_zone_is_422(client, db):
    """The auto-swap tick ignores a pause time without a time zone (and logs a
    warning at each tick), while the dashboard shows it as local time. So the
    PUT refuses it instead of storing a pause that does nothing."""
    resp = client.put(URL, json={"auto_swap_paused_until": "2026-11-02T18:00:00"})
    assert resp.status_code == 422
    assert resp.json() == {
        "error": {
            "message": "The pause time must have a time zone, for example 2026-11-02T18:00:00Z.",
            "code": "PAUSE_NO_TIME_ZONE",
        }
    }
    assert db.get_setting("auto_swap_paused_until") is None


def test_post_swap_pause_takes_the_same_cap(client, db):
    assert client.post(f"/api/settings/swap-pause?minutes={MAX_PAUSE_MINUTES + 1}").status_code == 422
    assert db.get_setting("auto_swap_paused_until") is None
    resp = client.post(f"/api/settings/swap-pause?minutes={MAX_PAUSE_MINUTES}")
    assert resp.status_code == 200
    assert resp.json()["minutes"] == MAX_PAUSE_MINUTES


# The pause checks apply to a NEW pause only. A pause stored before the checks
# existed can end more than 24 hours ahead, or have no time zone. The menu bar
# saves with GET, then PUT of the same body, so the stored pause comes back
# unchanged, and that save must still work.
_FAR = 48 * 60


@pytest.mark.parametrize(
    "stored, sent",
    [
        ("far", "far"),                      # the same string
        ("far_offset", "far_z"),             # the same instant, written another way
        ("naive", "naive"),                  # an old row without a time zone
    ],
)
def test_a_round_trip_keeps_a_stored_pause_that_fails_the_checks(client, db, stored, sent):
    at = datetime.now(timezone.utc) + timedelta(minutes=_FAR)
    forms = {
        "far": at.isoformat(),
        "far_offset": at.isoformat(),
        "far_z": at.isoformat().replace("+00:00", "Z"),
        "naive": at.replace(tzinfo=None).isoformat(),
    }
    db.set_setting("auto_swap_paused_until", forms[stored])
    body = client.get(URL).json()
    assert body["auto_swap_paused_until"] == forms[stored]
    body["auto_swap_paused_until"] = forms[sent]
    body["auto_swap_enabled"] = not body["auto_swap_enabled"]
    resp = client.put(URL, json=body)
    assert resp.status_code == 200
    assert db.get_setting("auto_swap_enabled") == "true"
    assert db.get_setting("auto_swap_paused_until") == forms[sent]


def test_without_a_database_a_pause_past_the_cap_is_still_refused(monkeypatch, woken):
    """With no database there is no stored pause, so every pause that fails
    the checks is new. The answer is the same 422, not a silent 200."""
    monkeypatch.setattr(app.state, "db", None, raising=False)
    http = TestClient(app)
    resp = http.put(URL, json={"auto_swap_paused_until": _in(_FAR)})
    assert resp.status_code == 422
    assert resp.json() == PAUSE_TOO_LONG
    assert http.put(URL, json={"auto_swap_paused_until": _in(30)}).status_code == 200


def test_a_new_pause_past_the_cap_is_still_refused_when_one_is_stored(client, db):
    kept = _in(_FAR)
    db.set_setting("auto_swap_paused_until", kept)
    body = client.get(URL).json()
    body["auto_swap_paused_until"] = _in(_FAR + 5)
    resp = client.put(URL, json=body)
    assert resp.status_code == 422
    assert resp.json() == PAUSE_TOO_LONG
    assert db.get_setting("auto_swap_paused_until") == kept


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
