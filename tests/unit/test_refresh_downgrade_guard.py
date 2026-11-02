"""No writer puts a null refresh token over this account's live one (#149i).

A credential writer builds its payload from the account row. After an
``invalid_grant`` the row can hold no refresh token while the live store holds
a working one that Claude Code rotated itself. ``build_oauth_data`` then
publishes ``refreshToken: null``, and a same-account activation (Use Account,
a reassert, ``jacked launch``) logs the running session out when its access
token expires. ``refresh_guard.refuses_refresh_downgrade`` is the one guard:
the switch engine asks it through ``SwitchRequest.refresh_downgrade_check``,
and the launch asks it before it rewrites the per-account file.

This module also holds holder discovery (``live_holder_account_ids``): a
holder that is missed lets jacked rotate the lineage a session holds.

The dead-chain adoption rule (reconcile and the launch sync) is in
test_launch_sync_dead_chain.py.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import click
import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import (
    HOLDER_WINDOW_MINUTES,
    build_oauth_data,
    clear_ownership_cache,
    live_holder_account_ids,
)
from jacked.credentials.models import (
    CredentialIdentity,
    ExecutableIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchContext,
    SwitchOutcome,
    SwitchResult,
)
from jacked.credentials.refresh_guard import (
    CC_REAUTH_REQUIRED_CODE,
    REFRESH_DOWNGRADE_REASON,
    refuses_refresh_downgrade,
)
from jacked.credentials.runtime import INSPECTED_CLAUDE_BUILD, activate_account
from jacked.web.database import Database
from tests.unit.launch_sync_helpers import (
    INTRUDER,
    OWNER,
    _Lookup,
    _account_dir,
    _make_row,
    _next_launch,
    _patched,
    _profile,
    _write_credentials,
)

FUTURE_S = int(time.time()) + 6 * 3600
FUTURE_MS = FUTURE_S * 1000
ORG = "org-own"
_LINUX = ExecutableIdentity(
    "/test/claude", "0" * 64, INSPECTED_CLAUDE_BUILD, "global", "linux", "x86_64"
)


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    clear_ownership_cache()
    yield
    clear_ownership_cache()


def _line(account_id: int) -> str:
    return f"Account {account_id}: {REFRESH_DOWNGRADE_REASON}"


def _dead_row(**changes) -> dict:
    row = {
        "id": 7,
        "email": OWNER,
        "organization_uuid": ORG,
        "access_token": "primary-at",
        "expires_at": FUTURE_S,
        "cc_access_token": "live-at",
        "cc_refresh_token": None,
        "cc_expires_at": FUTURE_S,
        "cc_refresh_failure_type": "invalid_grant",
    }
    row.update(changes)
    return row


def _live(access="live-at", refresh="live-rt", *, stamp=7) -> dict:
    oauth = {"accessToken": access, "expiresAt": FUTURE_MS}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    payload = {"claudeAiOauth": oauth}
    if stamp is not None:
        payload["_jackedAccountId"] = stamp
    return payload


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class TestTheGuard:
    def test_a_null_target_over_this_accounts_live_refresh_token_is_refused(self):
        row = _dead_row()
        target = build_oauth_data(row)
        assert target["refreshToken"] is None  # the dead chain publishes none

        assert refuses_refresh_downgrade(target, _live(), row) == REFRESH_DOWNGRADE_REASON

    @pytest.mark.parametrize("target", [{"accessToken": "a"}, {"refreshToken": ""}])
    def test_an_absent_or_empty_target_refresh_token_counts_as_null(self, target):
        assert refuses_refresh_downgrade(target, _live(), _dead_row()) == REFRESH_DOWNGRADE_REASON

    def test_a_target_that_carries_a_refresh_token_is_never_refused(self):
        lookup = _Lookup(_profile(OWNER))
        with _patched(lookup):
            verdict = refuses_refresh_downgrade(
                {"accessToken": "x", "refreshToken": "row-rt"}, _live("other-at"), _dead_row()
            )
        assert verdict is None
        assert lookup.calls == 0  # the cheap check runs before any profile request

    @pytest.mark.parametrize("refresh", [None, "", 5])
    def test_a_live_store_without_a_refresh_token_string_is_not_protected(self, refresh):
        lookup = _Lookup(_profile(OWNER))
        with _patched(lookup):
            verdict = refuses_refresh_downgrade(
                {"refreshToken": None}, _live("other-at", refresh), _dead_row()
            )
        assert verdict is None
        assert lookup.calls == 0

    @pytest.mark.parametrize("live", [None, {}, {"claudeAiOauth": "x"}, "not-a-dict"])
    def test_no_live_payload_is_not_protected(self, live):
        assert refuses_refresh_downgrade({"refreshToken": None}, live, _dead_row()) is None

    def test_another_owners_live_payload_is_not_protected(self):
        lookup = _Lookup(_profile(INTRUDER))
        with _patched(lookup):
            verdict = refuses_refresh_downgrade(
                {"refreshToken": None}, _live("intruder-at", "intruder-rt"), _dead_row()
            )
        assert verdict is None
        assert lookup.calls == 1

    def test_a_live_payload_whose_owner_cannot_be_proven_is_not_protected(self):
        lookup = _Lookup(ProfileLookup(None))  # the profile request failed
        with _patched(lookup):
            verdict = refuses_refresh_downgrade(
                {"refreshToken": None}, _live("unknown-at", "unknown-rt"), _dead_row()
            )
        assert verdict is None

    def test_a_profile_proof_protects_a_token_the_row_does_not_hold(self):
        lookup = _Lookup(_profile(OWNER, org=ORG, uuid=None))
        with _patched(lookup):
            verdict = refuses_refresh_downgrade(
                {"refreshToken": None}, _live("rotated-at", "rotated-rt"), _dead_row()
            )
        assert verdict == REFRESH_DOWNGRADE_REASON
        assert lookup.calls == 1

    @pytest.mark.parametrize("stamp", [None, 5])
    def test_the_stamp_is_not_required(self, stamp):
        """Claude Code keeps or drops the stamp on its own writes: the tokens decide."""
        verdict = refuses_refresh_downgrade({"refreshToken": None}, _live(stamp=stamp), _dead_row())
        assert verdict == REFRESH_DOWNGRADE_REASON


# ---------------------------------------------------------------------------
# The real activation path on a Linux file authority (the appendix acceptance)
# ---------------------------------------------------------------------------


def _authority() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _write_authority(account_id: int, access="live-at", refresh="live-rt") -> bytes:
    _authority().write_text(json.dumps(_live(access, refresh, stamp=account_id)))
    return _authority().read_bytes()


def _db_row(tmp_path, **cc) -> tuple[Database, dict]:
    db = Database(str(tmp_path / "guard.db"))
    account = db.create_account(
        OWNER, "primary-at", FUTURE_S, refresh_token="primary-rt", organization_uuid=ORG
    )
    if cc:
        db.update_account(account["id"], **cc)
    return db, db.get_account(account["id"])


_DEAD_CC = {
    "cc_access_token": "live-at",
    "cc_refresh_token": None,
    "cc_expires_at": FUTURE_S,
    "cc_refresh_failure_type": "invalid_grant",
    "cc_refresh_last_failed_at": 123,
}


def _activate(db, row, *, preflight_reconcile=True):
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("jacked.credentials.runtime.detect_claude_identity", return_value=_LINUX)
        )
        if not preflight_reconcile:
            stack.enter_context(
                mock.patch(
                    "jacked.credentials.runtime.reconcile_outgoing_before_activation",
                    return_value=True,
                )
            )
        return activate_account(db, row, SwitchContext.MANUAL, "op-guard")


def _assert_refused_and_untouched(result, before: bytes, account_id: int, caplog) -> None:
    assert result.outcome is SwitchOutcome.UNUSABLE
    assert result.code == CC_REAUTH_REQUIRED_CODE
    assert result.message == REFRESH_DOWNGRADE_REASON
    assert result.committed_authority == IdentityAxis(None, "unchanged")
    assert result.existing_session_activation is SessionActivationState.UNCHANGED
    after = _authority().read_bytes()
    assert after == before  # byte-identical, so the digest is unchanged too
    assert b'"refreshToken": null' not in after and b'"refreshToken":null' not in after
    assert caplog.text.count(_line(account_id)) == 1


class TestActivation:
    def test_the_engine_refuses_a_null_refresh_token_over_the_live_one(self, tmp_path, caplog):
        """Appendix acceptance: the row lost its refresh token to invalid_grant,
        the stamped live authority holds a working one with the row's own access
        token. The preflight adoption is held off here, so the engine guard is
        the only defense (as when the outgoing reconcile cannot run)."""
        db, row = _db_row(tmp_path, **_DEAD_CC)
        before = _write_authority(row["id"])

        with caplog.at_level(logging.WARNING):
            result = _activate(db, row, preflight_reconcile=False)

        _assert_refused_and_untouched(result, before, row["id"], caplog)

    def test_a_row_the_old_launch_sync_left_dead_beside_a_live_token_is_refused(
        self, tmp_path, caplog
    ):
        """The state #244i left: the row holds the live refresh token, but its
        breaker still says invalid_grant, so build_oauth_data publishes none.
        The token equals the row's own, so the adoption does not take it; the
        guard keeps it in the authority."""
        db, row = _db_row(tmp_path, **{**_DEAD_CC, "cc_refresh_token": "live-rt"})
        before = _write_authority(row["id"])

        with caplog.at_level(logging.WARNING):
            result = _activate(db, row)

        _assert_refused_and_untouched(result, before, row["id"], caplog)

    def test_the_preflight_adopts_the_live_lineage_and_publishes_it(self, tmp_path, caplog):
        """End to end: the outgoing reconcile adopts the proven live lineage, so
        the activation publishes the live refresh token instead of null."""
        db, row = _db_row(tmp_path, **_DEAD_CC)
        _write_authority(row["id"])

        with caplog.at_level(logging.INFO):
            result = _activate(db, row)

        assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
        oauth = json.loads(_authority().read_text())["claudeAiOauth"]
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("live-at", "live-rt")
        stored = db.get_account(row["id"])
        assert stored["cc_refresh_token"] == "live-rt"
        assert stored["cc_refresh_failure_type"] is None
        assert stored["cc_refresh_last_failed_at"] is None
        assert _line(row["id"]) not in caplog.text

    def test_a_new_account_with_an_empty_store_still_publishes(self, tmp_path):
        db, row = _db_row(tmp_path)  # no cc tokens: the primary-token fallback

        result = _activate(db, row)

        assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
        written = json.loads(_authority().read_text())
        assert written["_jackedAccountId"] == row["id"]
        assert written["claudeAiOauth"]["accessToken"] == "primary-at"
        assert written["claudeAiOauth"]["refreshToken"] is None

    def test_another_owners_live_payload_is_overwritten_as_before(self, tmp_path, caplog):
        db, row = _db_row(tmp_path, **{**_DEAD_CC, "cc_access_token": "row-at"})
        _write_authority(99, access="intruder-at", refresh="intruder-rt")
        lookup = _Lookup(_profile(INTRUDER))

        with _patched(lookup), caplog.at_level(logging.WARNING):
            result = _activate(db, row)

        assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
        written = json.loads(_authority().read_text())
        assert written["_jackedAccountId"] == row["id"]
        assert written["claudeAiOauth"]["accessToken"] == "row-at"
        assert lookup.calls == 1
        assert REFRESH_DOWNGRADE_REASON not in caplog.text


# ---------------------------------------------------------------------------
# The launch: the per-account file write and the global activation
# ---------------------------------------------------------------------------


class TestLaunchWrite:
    @pytest.mark.parametrize("scoped_authority", [True, False])
    def test_the_launch_keeps_the_files_live_refresh_token(
        self, tmp_path, caplog, scoped_authority
    ):
        """The per-account file holds the row's own refresh token while the
        row's chain is dead: the launch keeps the file's tokens and starts."""
        db = _make_row(tmp_path, cc_at="row-at-1", cc_rt="row-rt-1", failure="invalid_grant")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "row-at-1", "row-rt-1")

        with ExitStack() as stack, caplog.at_level(logging.WARNING):
            if scoped_authority:  # the file is what Claude reads (Linux, Windows)
                stack.enter_context(
                    mock.patch(
                        "jacked.launch.scoped_launch_needs_global_activation",
                        return_value=False,
                    )
                )
            prepared = _next_launch(db, tmp_path)

        assert prepared == config_dir
        oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("row-at-1", "row-rt-1")
        assert caplog.text.count(_line(1)) == 1

    def test_a_file_of_another_owner_is_replaced_as_before(self, tmp_path, caplog):
        db = _make_row(tmp_path, cc_at="row-at-1", cc_rt=None, failure="invalid_grant")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "intruder-at", "intruder-rt")

        with _patched(_Lookup(_profile(INTRUDER))), caplog.at_level(logging.WARNING):
            _next_launch(db, tmp_path)

        oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("row-at-1", None)
        assert REFRESH_DOWNGRADE_REASON not in caplog.text


def _refusal(observed_id: int, code: str = CC_REAUTH_REQUIRED_CODE) -> SwitchResult:
    return SwitchResult(
        operation_id="launch-refused",
        outcome=SwitchOutcome.UNUSABLE,
        desired_default=IdentityAxis(1, "desired"),
        storage=IdentityAxis(observed_id, "unusable"),
        committed_authority=IdentityAxis(None, "unchanged"),
        existing_session_activation=SessionActivationState.UNCHANGED,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=CredentialIdentity(account_id=observed_id),
        message=REFRESH_DOWNGRADE_REASON,
        code=code,
    )


class TestLaunchGlobalActivation:
    """The engine refusal reaches the launch on a Keychain build (macOS)."""

    def test_a_refusal_for_this_accounts_own_live_tokens_lets_claude_start(self, capsys):
        from jacked.launch import _activate_global_authority

        with mock.patch("jacked.launch._activate_launch_credentials", return_value=_refusal(1)):
            observed, evidence, _revision = _activate_global_authority({"id": 1}, None, 1)

        assert observed.account_id == 1
        assert "launch:global-authority:live-refresh-kept" in evidence
        assert REFRESH_DOWNGRADE_REASON in capsys.readouterr().err

    @pytest.mark.parametrize("observed_id, code", [(5, CC_REAUTH_REQUIRED_CODE), (1, "")])
    def test_any_other_refusal_still_stops_the_launch(self, observed_id, code):
        from jacked.launch import _activate_global_authority

        with (
            mock.patch(
                "jacked.launch._activate_launch_credentials",
                return_value=_refusal(observed_id, code),
            ),
            pytest.raises(click.ClickException, match="Could not establish"),
        ):
            _activate_global_authority({"id": 1}, None, 1)


class TestOtherConsumers:
    def test_the_dashboard_switch_answers_422_with_the_reason(self):
        from jacked.api.routes.auth import _switch_content, _switch_http_status

        result = _refusal(1)
        assert _switch_http_status(result.outcome) == 422
        content = _switch_content(result, "owner@test.com")
        assert content["status"] == "unusable"
        assert content["message"] == REFRESH_DOWNGRADE_REASON

    def test_the_authority_guard_does_not_count_the_refusal_as_a_committed_repair(self):
        from jacked.api.authority_guard import _repair_committed

        assert _repair_committed(SwitchOutcome.UNUSABLE) is False


# ---------------------------------------------------------------------------
# Holder discovery
# ---------------------------------------------------------------------------


def _session(db, session_id, account_id, *, idle_hours: float) -> None:
    db.record_session_account(session_id, account_id=account_id, detection_method="session_start")
    seen = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)).isoformat()
    with db._writer() as conn:
        conn.execute(
            "UPDATE session_accounts SET detected_at = ?, last_activity_at = ? "
            "WHERE session_id = ?",
            (seen, seen, session_id),
        )


_NO_TOKENS = {"claudeAiOauth": {}}


class _SessionsDb:
    """The two reads holder discovery makes; the window is honored."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.windows = []

    def list_accounts(self, include_inactive=False):
        return []

    def get_active_sessions(self, staleness_minutes=60):
        self.windows.append(staleness_minutes)
        return [s for s in self.sessions if s["idle_minutes"] < staleness_minutes]


class TestHolders:
    def test_a_session_with_no_account_id_makes_the_holders_unknown(self, tmp_path):
        db = Database(str(tmp_path / "holders.db"))
        _session(db, "s-known", 9, idle_hours=0)
        db.record_session_account("s-unknown", detection_method="session_start", pid=5151)

        assert live_holder_account_ids(db, _NO_TOKENS) is None

    def test_the_holder_window_is_the_access_token_life(self):
        db = _SessionsDb([
            {"account_id": 9, "idle_minutes": 120},
            {"account_id": 4, "idle_minutes": 9 * 60},
        ])

        assert live_holder_account_ids(db, _NO_TOKENS) == {9}
        assert db.windows == [HOLDER_WINDOW_MINUTES] and HOLDER_WINDOW_MINUTES == 480

    def test_a_session_idle_for_nine_hours_is_not_a_holder(self, tmp_path):
        db = Database(str(tmp_path / "holders.db"))
        _session(db, "s-old", 4, idle_hours=9)

        assert live_holder_account_ids(db, _NO_TOKENS) == set()

    def test_a_session_idle_for_two_hours_is_a_holder_in_the_real_database(self, tmp_path):
        db = Database(str(tmp_path / "holders.db"))
        _session(db, "s-idle", 9, idle_hours=2.5)

        assert live_holder_account_ids(db, _NO_TOKENS) == {9}
