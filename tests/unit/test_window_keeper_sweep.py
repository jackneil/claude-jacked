"""Window keeper sweep: wall-clock scheduling, pre-wake, recovery ladder,
fingerprinted backoff, persisted keeper state, and per-account isolation.

Runs the real keeper code against a real SQLite Database; only the network
edges (ping_account, token rotation, usage fetch) are mocked. Uses
asyncio.run() (project convention, no pytest-asyncio).
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jacked.api.usage_monitor as um
from jacked.web.auth import CCRefreshOutcome, CCRefreshResult
from jacked.web.database import Database
from jacked.web.window_keeper import PingResult, token_fingerprint

FUTURE_RESET_EPOCH = str(int(time.time()) + 4 * 3600)


def _ok(reset: str | None = FUTURE_RESET_EPOCH, request_id: str = "req_ok") -> PingResult:
    rate = {"anthropic-ratelimit-unified-5h-reset": reset} if reset else {}
    return PingResult(ok=True, status=200, request_id=request_id, rate_limit=rate)


def _err(status: int, error_type: str | None = None, **kw) -> PingResult:
    return PingResult(ok=False, status=status, error_type=error_type, **kw)


TRANSPORT = PingResult(ok=False, error_type="transport", message="ConnectError", transport_error=True)


@pytest.fixture(autouse=True)
def _reset_keeper_state():
    um._ping_backoff.clear()
    um._sweep_wake.clear()
    yield
    um._ping_backoff.clear()
    um._sweep_wake.clear()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "keeper.db"))


def _mk(db, email="a@test.com", cc="cc-tok", cc_rt="cc-rt", primary="pri-tok"):
    acct = db.create_account(
        email=email,
        access_token=primary,
        refresh_token="pri-rt",
        expires_at=int(time.time()) + 3600,
    )
    db.update_account(
        acct["id"],
        cc_access_token=cc,
        cc_refresh_token=cc_rt,
        cc_expires_at=int(time.time()) + 3600 if cc else None,
    )
    return acct["id"]


def _state(db, account_id) -> dict:
    return Database.parse_keeper_state(db.get_account(account_id)["keeper_state"])


class _Pings:
    """ping_account stand-in: answers by token, records every call."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, token, timeout=30, *, account_id=None):
        self.calls.append(token)
        answer = self.answers.get(token, _err(401, "authentication_error"))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list):
            return answer.pop(0)
        return answer


def _run_pass(db, pings, *, active_id=999, refresh=None, fetch=None, active=True):
    """One keeper pass with the network edges mocked."""
    refresh = refresh or AsyncMock(
        return_value=CCRefreshResult(CCRefreshOutcome.NO_REFRESH_TOKEN)
    )
    fetch = fetch or AsyncMock(return_value={"_cached": True})
    with (
        patch.object(um, "_KEEPER_PING_PACING_SECONDS", 0),
        patch("jacked.web.window_keeper.ping_account", new=pings),
        patch("jacked.web.window_keeper.is_active_hours", return_value=active),
        patch("jacked.web.auth.refresh_cc_token_result", refresh),
        patch("jacked.web.auth._try_refresh_primary_token", AsyncMock(return_value=None)),
        patch("jacked.web.auth.fetch_usage", fetch),
        patch(
            "jacked.api.credential_helpers.read_active_account_id",
            return_value=active_id,
        ),
    ):
        return asyncio.run(um._run_window_keeper(db, 300))


# ---------------------------------------------------------------------------
# A1 — wall-clock scheduling
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.sleeps = 0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        # Each 5 s slice "takes" 900 s of wall time: the laptop slept between
        # two dark wakes.
        self.sleeps += 1
        self.now += 900


class TestWallClockSleep:
    def test_sleep_until_exits_after_one_slice_when_wall_clock_jumps(self):
        clock = _Clock()
        with (
            patch.object(um.time, "time", clock.time),
            patch("jacked.api.usage_monitor.asyncio.sleep", clock.sleep),
        ):
            asyncio.run(um._sleep_until(clock.now + 300))
        assert clock.sleeps == 1

    def test_sleep_until_stops_on_wake(self):
        um._sweep_wake.set()
        sleeper = AsyncMock()
        with patch("jacked.api.usage_monitor.asyncio.sleep", sleeper):
            asyncio.run(um._sleep_until(time.time() + 300))
        sleeper.assert_not_called()

    def test_full_sweep_starts_next_iteration_after_one_slept_slice(self, caplog):
        """With the lid closed the next sweep must run on the first dark
        wake past the deadline, not after 60 nominal 5 s increments."""
        clock = _Clock()

        async def _sleep(seconds):
            await clock.sleep(seconds)
            if clock.sleeps >= 2:
                raise asyncio.CancelledError

        db = MagicMock()
        db.get_setting.side_effect = lambda k: {
            "window_keeper_enabled": "false",
            "usage_check_interval": "300",
        }.get(k)
        db.list_accounts.return_value = []
        app = MagicMock()
        app.state.db = db
        caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")
        with (
            patch.object(um.time, "time", clock.time),
            patch("jacked.api.usage_monitor.asyncio.sleep", _sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(um.full_sweep_loop(app))
        heartbeats = [
            r.getMessage() for r in caplog.records if "heartbeat" in r.getMessage()
        ]
        assert heartbeats[:2] == [
            "Full-sweep heartbeat: iter=1",
            "Full-sweep heartbeat: iter=2",
        ]

    def test_backoff_uses_wall_clock(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(400, "invalid_request_error")})
        _run_pass(db, pings)
        entry = um._ping_backoff[aid]
        assert time.time() < entry["skip_until"] <= time.time() + 3600


# ---------------------------------------------------------------------------
# A1 — pre-wake once per local date
# ---------------------------------------------------------------------------


def _fixed_now(dt: datetime):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return dt if tz is None else dt.astimezone(tz)

    return _DT


class TestPrewake:
    def _pass_at(self, db, dt, pings, **settings):
        for k, v in settings.items():
            db.set_setting(k, v)
        with (
            patch.object(um, "datetime", _fixed_now(dt)),
            patch.object(um, "_KEEPER_PING_PACING_SECONDS", 0),
            patch("jacked.web.window_keeper.ping_account", new=pings),
            patch("jacked.web.auth.fetch_usage", AsyncMock(return_value={"_cached": True})),
        ):
            return asyncio.run(um._run_window_keeper(db, 300))

    def test_prewake_fires_once_per_local_date(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _ok(reset=None)})
        day = datetime(2026, 10, 27)

        self._pass_at(db, day.replace(hour=3, minute=58), pings)
        assert pings.calls == []
        assert _state(db, aid)["skip_reason"] == "outside_active_hours"
        assert db.get_setting(um._PREWAKE_DONE_KEY) is None

        self._pass_at(db, day.replace(hour=4, minute=6), pings)
        assert pings.calls == ["cc-tok"]
        assert db.get_setting(um._PREWAKE_DONE_KEY) == "2026-10-27"

        # Same date, window still lapsed (no reset header): no second pre-wake.
        self._pass_at(db, day.replace(hour=4, minute=11), pings)
        self._pass_at(db, day.replace(hour=5, minute=30), pings)
        assert pings.calls == ["cc-tok"]

        # Next local date fires again. (Drop the no-window backoff the first
        # ping armed: only the pre-wake marker is under test here.)
        um._ping_backoff.clear()
        db.set_keeper_state(aid, next_attempt_at=None)
        self._pass_at(db, day.replace(day=28, hour=4, minute=6), pings)
        assert pings.calls == ["cc-tok", "cc-tok"]

    def test_prewake_catches_up_when_the_slice_was_slept_through(self, db):
        """Nothing ran since 04:00 and active hours start at 10:00: the 09:00
        sweep is the first one at or after pre-wake, so it fires."""
        _mk(db)
        pings = _Pings({"cc-tok": _ok()})
        self._pass_at(
            db,
            datetime(2026, 10, 27, 9, 0),
            pings,
            window_keeper_active_start="10:00",
        )
        assert pings.calls == ["cc-tok"]
        assert db.get_setting(um._PREWAKE_DONE_KEY) == "2026-10-27"

    def test_prewake_marker_not_used_inside_active_hours(self, db):
        """Inside active hours the normal pass runs; the pre-wake marker is
        neither consulted nor written, so it cannot add a second ping."""
        aid = _mk(db)
        pings = _Pings({"cc-tok": _ok()})
        self._pass_at(db, datetime(2026, 10, 27, 9, 0), pings)
        assert pings.calls == ["cc-tok"]
        assert db.get_setting(um._PREWAKE_DONE_KEY) is None
        # The window is now open: the next active-hours pass does not ping.
        self._pass_at(db, datetime(2026, 10, 27, 9, 5), pings)
        assert pings.calls == ["cc-tok"]
        assert _state(db, aid)["last_status"] == "ok"

    def test_prewake_does_not_fire_after_active_hours(self, db):
        _mk(db)
        pings = _Pings({"cc-tok": _ok()})
        self._pass_at(db, datetime(2026, 10, 27, 23, 30), pings)
        assert pings.calls == []


# ---------------------------------------------------------------------------
# A3 — recovery ladder
# ---------------------------------------------------------------------------


class TestRecoveryLadder:
    def test_401_force_rotates_cc_chain_and_repings(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401, "authentication_error"), "cc-new": _ok()})
        refresh = AsyncMock(
            return_value=CCRefreshResult(CCRefreshOutcome.ROTATED, access_token="cc-new")
        )
        fetch = AsyncMock(return_value={"_cached": True})
        assert _run_pass(db, pings, refresh=refresh, fetch=fetch) == 1
        refresh.assert_awaited_once_with(aid, db, force=True)
        assert pings.calls == ["cc-tok", "cc-new"]
        # The read-back uses the token that succeeded, with the bypass.
        fetch.assert_awaited_once_with(aid, db, access_token="cc-new", post_ping=True)
        st = _state(db, aid)
        assert st["last_status"] == "ok"
        assert st["token_fp"] == token_fingerprint("cc-new")
        assert aid not in um._ping_backoff

    def test_no_rotation_possible_falls_back_to_primary(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401), "pri-tok": _ok()})
        fetch = AsyncMock(return_value={"_cached": True})
        _run_pass(db, pings, fetch=fetch)
        assert pings.calls == ["cc-tok", "pri-tok"]
        fetch.assert_awaited_once_with(aid, db, access_token="pri-tok", post_ping=True)
        assert _state(db, aid)["last_status"] == "ok"

    @pytest.mark.parametrize(
        "outcome", [CCRefreshOutcome.FAILED, CCRefreshOutcome.NO_REFRESH_TOKEN]
    )
    def test_every_token_rejected_persists_needs_reauth_and_stops(self, db, outcome):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401, "authentication_error", request_id="req_1"), "pri-tok": _err(401)})
        refresh = AsyncMock(return_value=CCRefreshResult(outcome))
        _run_pass(db, pings, refresh=refresh)
        st = _state(db, aid)
        assert st["last_status"] == "needs_reauth"
        assert st["consecutive_auth_failures"] == 1
        assert st["token_fp"] == token_fingerprint("cc-tok")

        # Terminal: the next sweeps do not ping the dead tokens again.
        _run_pass(db, pings, refresh=refresh)
        _run_pass(db, pings, refresh=refresh)
        assert pings.calls == ["cc-tok", "pri-tok"]

        # A re-auth (new CC token) makes the keeper ping again at once.
        db.update_account(aid, cc_access_token="cc-reauthed")
        pings.answers["cc-reauthed"] = _ok()
        _run_pass(db, pings, refresh=refresh)
        assert pings.calls[-1] == "cc-reauthed"
        assert _state(db, aid)["last_status"] == "ok"

    def test_lock_held_rotation_is_retried_not_terminal(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401), "pri-tok": _err(401)})
        refresh = AsyncMock(return_value=CCRefreshResult(CCRefreshOutcome.LOCK_HELD))
        _run_pass(db, pings, refresh=refresh)
        st = _state(db, aid)
        assert st["last_status"] == "auth_failed"
        assert st["next_attempt_at"] > time.time()

    def test_active_account_reconciles_and_never_rotates_cc(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401), "cc-live": _ok()})
        refresh = AsyncMock()

        def _reconcile(account_id, _db):
            _db.update_account(account_id, cc_access_token="cc-live")

        with patch(
            "jacked.api.credential_helpers.reconcile_credentials_from_live_store",
            side_effect=_reconcile,
        ) as rec:
            _run_pass(db, pings, active_id=aid, refresh=refresh)
        rec.assert_called_once()
        refresh.assert_not_awaited()
        assert pings.calls == ["cc-tok", "cc-live"]
        assert _state(db, aid)["last_status"] == "ok"

    def test_active_account_falls_through_to_primary_then_needs_reauth(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(401), "pri-tok": _err(403, "permission_error")})
        refresh = AsyncMock()
        with patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"):
            _run_pass(db, pings, active_id=aid, refresh=refresh)
        refresh.assert_not_awaited()
        assert pings.calls == ["cc-tok", "pri-tok"]
        assert _state(db, aid)["last_status"] == "needs_reauth"

    def test_null_cc_token_pings_with_primary(self, db):
        aid = _mk(db, cc=None, cc_rt=None)
        pings = _Pings({"pri-tok": _ok()})
        _run_pass(db, pings)
        assert pings.calls == ["pri-tok"]
        assert _state(db, aid)["last_status"] == "ok"

    def test_dead_cc_chain_flag_skips_straight_to_primary(self, db):
        """Workstream B's per-chain failure column: invalid_grant on the CC
        chain means the CC token is dead; ping with the primary token."""
        aid = _mk(db)
        acct = dict(db.get_account(aid), cc_refresh_failure_type="invalid_grant")
        pings = _Pings({"pri-tok": _ok()})
        with (
            patch("jacked.web.window_keeper.ping_account", new=pings),
            patch("jacked.web.auth.fetch_usage", AsyncMock(return_value={"_cached": True})),
        ):
            assert asyncio.run(um._keeper_process_account(db, acct, 300)) == "pinged"
        assert pings.calls == ["pri-tok"]

    def test_api_rejected_cc_token_skips_straight_to_primary(self, db):
        """auth_rejected (the API refused the CC token and jacked could not
        replace it) is as unusable as a dead chain: ping with the primary."""
        aid = _mk(db)
        acct = dict(db.get_account(aid), cc_refresh_failure_type="auth_rejected")
        pings = _Pings({"pri-tok": _ok()})
        with (
            patch("jacked.web.window_keeper.ping_account", new=pings),
            patch("jacked.web.auth.fetch_usage", AsyncMock(return_value={"_cached": True})),
        ):
            assert asyncio.run(um._keeper_process_account(db, acct, 300)) == "pinged"
        assert pings.calls == ["pri-tok"]

    def test_429_schedules_from_retry_after_without_escalating(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(429, "rate_limit_error", retry_after=120.0)})
        before = time.time()
        _run_pass(db, pings)
        entry = um._ping_backoff[aid]
        assert entry["fails"] == 0
        assert before + 119 <= entry["skip_until"] <= time.time() + 121
        st = _state(db, aid)
        assert st["last_status"] == "rate_limited"
        assert st["error_type"] == "rate_limit_error"
        # Next sweep: skipped, reason recorded, no rotation attempted.
        _run_pass(db, pings)
        assert pings.calls == ["cc-tok"]
        assert _state(db, aid)["skip_reason"] == "backoff"

    def test_429_without_retry_after_uses_the_5h_reset_header(self, db):
        aid = _mk(db)
        reset = int(time.time()) + 1800
        pings = _Pings({
            "cc-tok": _err(
                429,
                rate_limit={"anthropic-ratelimit-unified-5h-reset": str(reset)},
            )
        })
        _run_pass(db, pings)
        assert um._ping_backoff[aid]["skip_until"] == pytest.approx(reset, abs=1)
        assert _state(db, aid)["error_type"] == "rate_limited"

    def test_429_with_no_hint_uses_the_escalating_backoff(self, db):
        aid = _mk(db)
        _run_pass(db, _Pings({"cc-tok": _err(429)}))
        assert um._ping_backoff[aid]["fails"] == 1

    def test_three_transport_errors_never_escalate(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": TRANSPORT})
        for _ in range(3):
            _run_pass(db, pings)
        assert pings.calls == ["cc-tok"] * 3
        assert um._ping_backoff.get(aid, {}).get("fails", 0) == 0
        assert _state(db, aid)["last_status"] == "transport"

    def test_5xx_is_transient(self, db):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _err(529, "overloaded_error")})
        _run_pass(db, pings)
        _run_pass(db, pings)
        assert pings.calls == ["cc-tok", "cc-tok"]
        assert um._ping_backoff.get(aid, {}).get("fails", 0) == 0


# ---------------------------------------------------------------------------
# A4 — backoff keyed by token identity
# ---------------------------------------------------------------------------


class TestBackoffTokenIdentity:
    def test_changed_token_drops_backoff_and_pings_at_once(self, db):
        aid = _mk(db)
        um._ping_backoff[aid] = {
            "fails": 6,
            "skip_until": time.time() + 3600,
            "token_fp": token_fingerprint("cc-tok"),
        }
        pings = _Pings({"cc-new": _ok()})
        _run_pass(db, pings)
        assert pings.calls == []
        db.update_account(aid, cc_access_token="cc-new")
        _run_pass(db, pings)
        assert pings.calls == ["cc-new"]
        assert aid not in um._ping_backoff

    def test_persisted_schedule_survives_a_restart(self, db):
        aid = _mk(db)
        db.set_keeper_state(
            aid,
            last_status="rate_limited",
            next_attempt_at=time.time() + 600,
            token_fp=token_fingerprint("cc-tok"),
        )
        pings = _Pings({"cc-tok": _ok()})
        _run_pass(db, pings)  # in-memory backoff is empty (fresh process)
        assert pings.calls == []
        assert _state(db, aid)["skip_reason"] == "backoff"

    def test_clear_ping_backoff_drops_entry_and_wakes_sweep(self):
        um._ping_backoff[7] = {"fails": 3, "skip_until": time.time() + 999, "token_fp": "x"}
        um.clear_ping_backoff(7)
        assert 7 not in um._ping_backoff
        assert um._sweep_wake.is_set()

    def test_storing_cc_tokens_clears_backoff_and_wakes(self, tmp_path):
        from jacked.web.oauth import OAuthFlow
        from tests.unit.test_account_identity_storage import (
            _make_db,
            _profile,
            _tokens_after_exchange,
        )

        db, acct = _make_db(tmp_path)
        um._ping_backoff[acct["id"]] = {
            "fails": 4,
            "skip_until": time.time() + 3600,
            "token_fp": "deadbeef",
        }
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        flow._store_cc_tokens(_tokens_after_exchange(), _profile())
        assert acct["id"] not in um._ping_backoff
        assert um._sweep_wake.is_set()

    def test_reauth_primary_store_clears_backoff(self, tmp_path):
        from jacked.web.oauth import OAuthFlow
        from tests.unit.test_account_identity_storage import (
            _make_db,
            _profile,
            _tokens_after_exchange,
        )

        db, acct = _make_db(tmp_path)
        um._ping_backoff[acct["id"]] = {"fails": 4, "skip_until": time.time() + 3600, "token_fp": "x"}
        flow = OAuthFlow(db, purpose="primary", target_account_id=acct["id"])
        flow._store_account(_tokens_after_exchange(), _profile(), {})
        assert acct["id"] not in um._ping_backoff
        assert um._sweep_wake.is_set()

    def test_reconcile_import_clears_backoff(self, db):
        from jacked.api import credential_helpers as ch

        aid = _mk(db)
        um._ping_backoff[aid] = {"fails": 2, "skip_until": time.time() + 3600, "token_fp": "x"}
        live = {
            "_jackedAccountId": aid,
            "claudeAiOauth": {
                "accessToken": "cc-live",
                "refreshToken": "cc-rt",
                "expiresAt": (int(time.time()) + 7200) * 1000,
            },
        }
        with (
            patch.object(ch, "_get_cached_live_credentials", return_value=live),
            patch.object(ch, "_live_tokens_belong_to", return_value=True),
        ):
            ch.reconcile_credentials_from_live_store(aid, db)
        assert db.get_account(aid)["cc_access_token"] == "cc-live"
        assert aid not in um._ping_backoff
        assert um._sweep_wake.is_set()

    def test_authority_guard_import_clears_backoff(self, db):
        from jacked.api.authority_guard import _import_rotated_tokens

        aid = _mk(db)
        um._ping_backoff[aid] = {"fails": 2, "skip_until": time.time() + 3600, "token_fp": "x"}
        # Ownership is proven by the profile the guard already fetched for
        # this payload (workstream B's shared ownership rule).
        payload = {"claudeAiOauth": {"accessToken": "cc-rotated", "refreshToken": "cc-rt2"}}
        profile = {"account": {"email": "a@test.com"}}
        assert _import_rotated_tokens(db, db.get_account(aid), payload, profile=profile) >= 1
        assert aid not in um._ping_backoff
        assert um._sweep_wake.is_set()


# ---------------------------------------------------------------------------
# A3/A6 — isolation and persisted skip reasons
# ---------------------------------------------------------------------------


class TestIsolationAndSkipReasons:
    def test_one_failing_account_does_not_skip_the_rest(self, db, caplog):
        a1 = _mk(db, email="one@test.com", cc="cc-1")
        a2 = _mk(db, email="two@test.com", cc="cc-2")
        pings = _Pings({"cc-1": RuntimeError("boom"), "cc-2": _ok()})
        with caplog.at_level(logging.ERROR, logger="jacked.api.usage_monitor"):
            assert _run_pass(db, pings) == 1
        assert pings.calls == ["cc-1", "cc-2"]
        assert _state(db, a2)["last_status"] == "ok"
        assert any(f"account {a1} failed" in r.getMessage() for r in caplog.records)

    def test_malformed_reset_value_is_pinged_not_raised(self, db):
        aid = _mk(db)
        db.update_account(aid, cached_5h_resets_at="garbage")
        pings = _Pings({"cc-tok": _ok()})
        assert _run_pass(db, pings) == 1

    def test_outside_active_hours_reason_is_written_once(self, db):
        aid = _mk(db)
        calls = []
        real = db.set_keeper_state

        def _spy(account_id, **fields):
            calls.append(fields)
            return real(account_id, **fields)

        with patch.object(db, "set_keeper_state", side_effect=_spy):
            _run_pass(db, _Pings({}), active=False)
            _run_pass(db, _Pings({}), active=False)
        assert len(calls) == 1
        assert _state(db, aid)["skip_reason"] == "outside_active_hours"

    def test_skip_reason_is_not_rewritten_when_an_old_schedule_lingers(self, db):
        aid = _mk(db)
        db.set_keeper_state(aid, last_status="transport", next_attempt_at=time.time() + 300)
        calls = []
        real = db.set_keeper_state

        def _spy(account_id, **fields):
            calls.append(fields)
            return real(account_id, **fields)

        with patch.object(db, "set_keeper_state", side_effect=_spy):
            for _ in range(3):
                _run_pass(db, _Pings({}), active=False)
        assert len(calls) == 1

    def test_keeper_disabled_reason(self, db):
        aid = _mk(db)
        um._note_keeper_disabled(db)
        assert _state(db, aid)["skip_reason"] == "keeper_disabled"

    def test_auto_swap_disabled_reason(self, db):
        aid = _mk(db)
        with db._writer() as conn:
            conn.execute("UPDATE accounts SET auto_swap_enabled = 0 WHERE id = ?", (aid,))
        pings = _Pings({})
        _run_pass(db, pings)
        assert pings.calls == []
        assert _state(db, aid)["skip_reason"] == "auto_swap_disabled"

    def test_no_token_reason(self, db):
        aid = _mk(db, cc=None, cc_rt=None, primary="")
        pings = _Pings({})
        _run_pass(db, pings)
        assert pings.calls == []
        assert _state(db, aid)["skip_reason"] == "no_token"

    def test_codex_account_is_not_kept(self, db):
        acct = db.create_account(
            email="c@test.com", access_token="codex-tok",
            expires_at=int(time.time()) + 3600, provider="codex",
        )
        pings = _Pings({})
        _run_pass(db, pings)
        assert pings.calls == []
        assert _state(db, acct["id"])["skip_reason"] == "codex_no_keeper"

    def test_open_window_clears_a_stale_skip_reason(self, db):
        aid = _mk(db)
        db.set_keeper_state(aid, skip_reason="outside_active_hours")
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        db.update_account_usage_cache(aid, five_hour_resets_at=future, seven_day_resets_at=future)
        _run_pass(db, _Pings({}))
        assert _state(db, aid)["skip_reason"] is None


# ---------------------------------------------------------------------------
# A5 — the window is persisted from the ping itself
# ---------------------------------------------------------------------------


class TestWindowPersistedFromPing:
    def test_reset_header_is_written_and_next_sweep_does_not_reping(self, db):
        aid = _mk(db)
        reset = int(time.time()) + 4 * 3600
        ok = PingResult(
            ok=True,
            status=200,
            rate_limit={
                "anthropic-ratelimit-unified-5h-reset": str(reset),
                "anthropic-ratelimit-unified-5h-utilization": "0.07",
            },
        )
        pings = _Pings({"cc-tok": ok})
        _run_pass(db, pings)
        row = db.get_account(aid)
        stored = datetime.fromisoformat(row["cached_5h_resets_at"]).timestamp()
        assert stored == pytest.approx(reset, abs=1)
        assert row["cached_usage_5h"] == pytest.approx(7.0)
        _run_pass(db, pings)
        assert pings.calls == ["cc-tok"]

    def test_ping_ok_without_any_window_is_recorded_and_backed_off(self, db, caplog):
        aid = _mk(db)
        pings = _Pings({"cc-tok": _ok(reset=None)})
        with caplog.at_level(logging.WARNING, logger="jacked.api.usage_monitor"):
            _run_pass(db, pings)
        st = _state(db, aid)
        assert st["last_status"] == "ping_ok_no_window"
        assert st["next_attempt_at"] > time.time()
        assert any("no open 5h window" in r.getMessage() for r in caplog.records)
        _run_pass(db, pings)
        assert pings.calls == ["cc-tok"]


# ---------------------------------------------------------------------------
# A6 — the state is JSON with only known keys
# ---------------------------------------------------------------------------


def test_keeper_state_rejects_unknown_fields(db):
    aid = _mk(db)
    with pytest.raises(ValueError):
        db.set_keeper_state(aid, nonsense=1)


def test_keeper_state_column_holds_json(db):
    aid = _mk(db)
    _run_pass(db, _Pings({"cc-tok": _ok()}))
    raw = db.get_account(aid)["keeper_state"]
    assert json.loads(raw)["last_status"] == "ok"


# ---------------------------------------------------------------------------
# refresh_cc_token_result — explicit outcomes, force bypasses only the clock
# ---------------------------------------------------------------------------


class TestCCRefreshResult:
    def _flow(self, **kw):
        from jacked.web.auth import TokenExchangeResult

        return AsyncMock(return_value=TokenExchangeResult(**kw))

    def test_not_due_without_force_is_skipped(self, db):
        from jacked.web import auth

        aid = _mk(db)  # cc_expires_at one hour out: not near expiry
        flow = self._flow(success=True, access_token="new", refresh_token="rt2")
        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(auth.refresh_cc_token_result(aid, db))
            assert asyncio.run(auth.refresh_cc_token(aid, db)) is True
        assert res.outcome is CCRefreshOutcome.SKIPPED_NOT_DUE
        flow.assert_not_awaited()

    def test_force_bypasses_the_clock_gate(self, db):
        from jacked.web import auth

        aid = _mk(db)
        flow = self._flow(success=True, access_token="new", refresh_token="rt2")
        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(auth.refresh_cc_token_result(aid, db, force=True))
        assert res.outcome is CCRefreshOutcome.ROTATED
        assert res.access_token == "new"

    def test_peer_rotation_is_reported(self, db):
        from jacked.web import auth

        aid = _mk(db)
        flow = self._flow(success=True, access_token="theirs")
        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(auth.refresh_cc_token_result(aid, db, force=True))
        assert res.outcome is CCRefreshOutcome.PEER_ROTATED
        assert res.new_token is True

    def test_force_keeps_the_no_refresh_token_guard(self, db):
        from jacked.web import auth

        aid = _mk(db, cc_rt=None)
        flow = self._flow(success=True)
        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(auth.refresh_cc_token_result(aid, db, force=True))
        assert res.outcome is CCRefreshOutcome.NO_REFRESH_TOKEN
        flow.assert_not_awaited()

    def test_force_keeps_the_lock_guard(self, db):
        from jacked.web import auth

        aid = _mk(db)
        flow = self._flow(success=True)

        async def _run():
            lock = auth._get_cc_refresh_lock(aid)
            await lock.acquire()
            try:
                return await auth.refresh_cc_token_result(aid, db, force=True)
            finally:
                lock.release()

        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(_run())
        assert res.outcome is CCRefreshOutcome.LOCK_HELD
        flow.assert_not_awaited()

    def test_failed_exchange_and_bool_wrapper(self, db):
        from jacked.web import auth

        aid = _mk(db)
        db.update_account(aid, cc_expires_at=0)  # due, so the wrapper exchanges
        flow = self._flow(success=False, error="invalid_grant")
        with patch.object(auth, "_refresh_token_flow", flow):
            res = asyncio.run(auth.refresh_cc_token_result(aid, db, force=True))
            assert asyncio.run(auth.refresh_cc_token(aid, db)) is False
        assert res.outcome is CCRefreshOutcome.FAILED
        assert res.error == "invalid_grant"

    def test_missing_account_fails(self, db):
        from jacked.web import auth

        res = asyncio.run(auth.refresh_cc_token_result(424242, db, force=True))
        assert res.outcome is CCRefreshOutcome.FAILED


def test_null_7d_reset_alone_does_not_reping_after_success(db):
    """The usage read can report no 7d window; a fresh successful ping must
    not be repeated every sweep just because cached_7d_resets_at is None."""
    aid = _mk(db)
    pings = _Pings({"cc-tok": _ok()})  # 5h header only, no 7d header
    _run_pass(db, pings)
    assert db.get_account(aid)["cached_7d_resets_at"] is None
    _run_pass(db, pings)
    assert pings.calls == ["cc-tok"]
