"""Tests for the /accounts/{id}/use endpoint (dashboard account switching)."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.credentials.models import (
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)
from jacked.credentials.resolver import ResolverObservation, ResolverState
from jacked.web.database import Database


@pytest.fixture(autouse=True)
def _restore_usage_monitor_swap_state():
    """Successful /use calls invoke usage_monitor.note_external_swap(),
    which arms module-level cooldown/residency clocks.  Restore them so
    these tests don't leak swap state into other test files."""
    from jacked.api import usage_monitor as um

    saved = (
        um._last_swap_time,
        um._last_committed_swap_time,
        dict(um._emerged_tier_streak),
    )
    yield
    um._last_swap_time, um._last_committed_swap_time = saved[0], saved[1]
    um._emerged_tier_streak.clear()
    um._emerged_tier_streak.update(saved[2])


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        # Account 1: fully valid with CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (1, 'alice@test.com', 'at_1', 'rt_1', 1900000000,
                       1, 0, 'valid', 'max', 't1',
                       'cc_at_1', 'cc_rt_1', 1900000000,
                       NULL, 0, NULL)"""
        )
        # Account 2: disabled
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (2, 'bob@test.com', 'at_2', 'rt_2', 1900000000,
                       0, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 3: valid but no CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (3, 'carol@test.com', 'at_3', 'rt_3', 1900000000,
                       1, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 4: invalid validation status
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (4, 'dave@test.com', 'at_4', 'rt_4', 1900000000,
                       1, 0, 'invalid', 'max', 't1',
                       'cc_at_4', 'cc_rt_4', 1900000000,
                       NULL, 2, 'Token revoked')"""
        )
        # Account 5: soft-deleted
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (5, 'eve@test.com', 'at_5', 'rt_5', 1900000000,
                       1, 1, 'valid', 'max', 't1',
                       'cc_at_5', 'cc_rt_5', 1900000000,
                       NULL, 0, NULL)"""
        )
    yield db
    db.close()


def _switch_result(outcome=SwitchOutcome.COMMITTED):
    committed = outcome in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
    }
    return SwitchResult(
        operation_id="test-operation",
        outcome=outcome,
        desired_default=IdentityAxis(1, "desired"),
        storage=IdentityAxis(1 if committed else None, outcome.value),
        committed_authority=IdentityAxis(
            1 if committed else None, "committed" if committed else "unchanged"
        ),
        existing_session_activation=SessionActivationState.PENDING_NEXT_ACTIVITY,
        provider_verification=ProviderVerificationState.UNVERIFIED,
    )


@pytest.fixture
def app(db, tmp_path):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    app.state.credential_switcher = mock.Mock(return_value=_switch_result())
    return app


@pytest.fixture
def client(app):
    return TestClient(
        app, headers={"X-Jacked-Page-Session": "page-session-123456"}
    )


def test_use_account_success(client, app, tmp_path):
    """Activating a valid account with CC tokens writes credentials to all stores."""
    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
    ):
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "committed"
    assert data["email"] == "alice@test.com"
    app.state.credential_switcher.assert_called_once()
    assert app.state.credential_switcher.call_args.args[1]["id"] == 1


def test_use_account_passes_distinct_action_and_operation_ids(client, app):
    headers = {
        "X-Jacked-Action-Id": "action-1234567890",
        "X-Jacked-Operation-Id": "operation-1234567890",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        response = client.post("/api/auth/accounts/1/use", headers=headers)

    assert response.status_code == 200
    assert app.state.credential_switcher.call_args.args[3] == "operation-1234567890"


def test_use_account_action_retry_is_idempotent(client, app):
    headers = {
        "X-Jacked-Action-Id": "action-idempotent-1234",
        "X-Jacked-Operation-Id": "operation-idempotent-1234",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        first = client.post("/api/auth/accounts/1/use", headers=headers)
        second = client.post("/api/auth/accounts/1/use", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    app.state.credential_switcher.assert_called_once()


def test_use_account_action_id_cannot_be_rebound(client):
    first_headers = {
        "X-Jacked-Action-Id": "action-mismatch-12345",
        "X-Jacked-Operation-Id": "operation-original-1234",
    }
    second_headers = {
        **first_headers,
        "X-Jacked-Operation-Id": "operation-changed-12345",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        assert client.post(
            "/api/auth/accounts/1/use", headers=first_headers
        ).status_code == 200
        response = client.post("/api/auth/accounts/1/use", headers=second_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CREDENTIAL_ACTION_MISMATCH"


def test_use_account_requires_page_session(app):
    unbound_client = TestClient(app)

    response = unbound_client.post("/api/auth/accounts/1/use")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CREDENTIAL_PAGE_SESSION_INVALID"


def test_credential_operation_status_replays_completed_result(client):
    headers = {
        "X-Jacked-Action-Id": "action-status-1234567",
        "X-Jacked-Operation-Id": "operation-status-1234",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        completed = client.post("/api/auth/accounts/1/use", headers=headers)

    status_response = client.get(
        "/api/auth/credential-operations/action-status-1234567"
    )

    assert completed.status_code == 200
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["state"] == "complete"
    assert status_data["operation_id"] == "operation-status-1234"
    assert status_data["result"]["status"] == "committed"
    assert status_response.headers["cache-control"] == "no-store"


def test_credential_operation_status_is_page_session_bound(client):
    headers = {
        "X-Jacked-Action-Id": "action-bound-12345678",
        "X-Jacked-Operation-Id": "operation-bound-12345",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        assert client.post(
            "/api/auth/accounts/1/use", headers=headers
        ).status_code == 200

    response = client.get(
        "/api/auth/credential-operations/action-bound-12345678",
        headers={"X-Jacked-Page-Session": "different-page-session-123"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CREDENTIAL_PAGE_SESSION_MISMATCH"


def test_credential_operation_status_reports_claimed_work(client, db):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    state, _ = db.claim_auth_action(
        "action-running-1234567",
        session_key="page-session-123456",
        action="use_account",
        request_digest="digest",
        operation_id="operation-running-1234",
        expires_at=expires,
    )

    response = client.get(
        "/api/auth/credential-operations/operation-running-1234"
    )

    assert state == "new"
    assert response.status_code == 202
    assert response.json() == {
        "action_id": "action-running-1234567",
        "operation_id": "operation-running-1234",
        "state": "claimed",
        "result": None,
    }
    assert response.headers["cache-control"] == "no-store"


def test_use_account_rejects_reused_operation_id(client, app):
    first = {
        "X-Jacked-Action-Id": "action-first-12345678",
        "X-Jacked-Operation-Id": "operation-reused-1234",
    }
    second = {
        "X-Jacked-Action-Id": "action-second-1234567",
        "X-Jacked-Operation-Id": "operation-reused-1234",
    }
    with mock.patch(
        "jacked.api.usage_monitor._read_active_account_id", return_value=None
    ):
        assert client.post("/api/auth/accounts/1/use", headers=first).status_code == 200
        response = client.post("/api/auth/accounts/1/use", headers=second)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CREDENTIAL_OPERATION_IN_PROGRESS"
    assert app.state.credential_switcher.call_count == 1


def test_use_account_marks_only_global_sessions_pending_after_readback(client, db):
    db.record_session_account(
        "global-session",
        account_id=1,
        credential_scope="global",
        observation_state="observed",
    )
    db.record_session_account(
        "scoped-session",
        account_id=1,
        credential_scope="scoped",
        observation_state="observed",
    )

    with (
        mock.patch(
            "jacked.api.credential_helpers.sync_credential_to_all_stores",
            return_value=True,
        ),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
    ):
        response = client.post("/api/auth/accounts/1/use")

    assert response.status_code == 200
    assert (
        db.get_session_accounts("global-session")[0]["observation_state"] == "pending"
    )
    assert (
        db.get_session_accounts("scoped-session")[0]["observation_state"] == "observed"
    )


def test_use_account_readback_failure_does_not_change_session_or_default(
    client, app, db
):
    db.record_session_account(
        "global-session",
        account_id=1,
        credential_scope="global",
        observation_state="observed",
    )

    app.state.credential_switcher.return_value = _switch_result(
        SwitchOutcome.INDETERMINATE
    )
    with (
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap") as note_swap,
    ):
        response = client.post("/api/auth/accounts/1/use")

    assert response.status_code == 503
    assert response.json()["status"] == "indeterminate"
    assert (
        db.get_session_accounts("global-session")[0]["observation_state"] == "observed"
    )
    assert db.get_setting("active_account_id") is None
    note_swap.assert_not_called()


def test_use_account_arms_auto_swap_pause(client, db):
    """Successful manual switch notifies the usage monitor (cooldown +
    residency + streak reset) and pauses auto-swap for 15 minutes so the
    loop cannot silently revert the user's choice."""
    before = datetime.now(timezone.utc)
    with (
        mock.patch("jacked.api.credential_helpers.sync_credential_to_all_stores"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap") as mock_note,
    ):
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    mock_note.assert_called_once_with()

    paused = db.get_setting("auto_swap_paused_until")
    assert paused, "manual switch must set auto_swap_paused_until"
    paused_dt = datetime.fromisoformat(paused)
    assert paused_dt.tzinfo is not None, "pause timestamp must be tz-aware UTC"
    delta = paused_dt - before
    assert (
        timedelta(minutes=14, seconds=50) <= delta <= timedelta(minutes=15, seconds=30)
    ), f"expected ~15min pause, got {delta}"


def test_use_account_does_not_shorten_longer_pause(client, db):
    """A manual switch must never shorten an explicit user-set pause —
    POST /api/settings/swap-pause supports up to 1440 minutes; the 15-min
    residency pause may only ever EXTEND the active pause."""
    existing = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
    db.set_setting("auto_swap_paused_until", existing)

    with (
        mock.patch("jacked.api.credential_helpers.sync_credential_to_all_stores"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    assert db.get_setting("auto_swap_paused_until") == existing, (
        "manual switch must not shorten an active longer pause"
    )


def test_use_account_extends_shorter_pause(client, db):
    """A manual switch extends a shorter active pause out to ~15 minutes."""
    db.set_setting(
        "auto_swap_paused_until",
        (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    )
    before = datetime.now(timezone.utc)

    with (
        mock.patch("jacked.api.credential_helpers.sync_credential_to_all_stores"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    paused_dt = datetime.fromisoformat(db.get_setting("auto_swap_paused_until"))
    delta = paused_dt - before
    assert (
        timedelta(minutes=14, seconds=50) <= delta <= timedelta(minutes=15, seconds=30)
    ), f"expected pause extended to ~15min, got {delta}"


def test_use_account_overwrites_unparseable_pause(client, db):
    """Garbage in the pause setting (the sweep loop ignores it anyway) is
    replaced by the standard 15-minute pause, not preserved."""
    db.set_setting("auto_swap_paused_until", "not-a-timestamp")
    before = datetime.now(timezone.utc)

    with (
        mock.patch("jacked.api.credential_helpers.sync_credential_to_all_stores"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    paused_dt = datetime.fromisoformat(db.get_setting("auto_swap_paused_until"))
    delta = paused_dt - before
    assert timedelta(minutes=14) <= delta <= timedelta(minutes=16)


def test_use_account_rejected_does_not_arm_pause(client, db):
    """A rejected switch (disabled account) must NOT pause auto-swap or
    note an external swap — nothing was actually switched."""
    with mock.patch("jacked.api.usage_monitor.note_external_swap") as mock_note:
        resp = client.post("/api/auth/accounts/2/use")

    assert resp.status_code == 400
    mock_note.assert_not_called()
    assert not db.get_setting("auto_swap_paused_until")


def test_use_account_not_found(client):
    """Returns 404 for non-existent account."""
    resp = client.post("/api/auth/accounts/999/use")
    assert resp.status_code == 404


def test_use_account_disabled(client):
    """Returns 400 for disabled account."""
    resp = client.post("/api/auth/accounts/2/use")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["error"]["message"].lower()


def test_use_account_no_cc_tokens(client):
    """Returns 400 for account without CC tokens (would be un-refreshable)."""
    resp = client.post("/api/auth/accounts/3/use")
    assert resp.status_code == 400
    assert (
        "cc" in resp.json()["error"]["message"].lower()
        or "authorize" in resp.json()["error"]["message"].lower()
    )


def test_use_account_invalid_status(client):
    """Returns 400 for account with invalid validation status."""
    resp = client.post("/api/auth/accounts/4/use")
    assert resp.status_code == 400
    assert (
        "invalid" in resp.json()["error"]["message"].lower()
        or "re-auth" in resp.json()["error"]["message"].lower()
    )


def test_use_account_deleted(client):
    """Returns 404 for soft-deleted account."""
    resp = client.post("/api/auth/accounts/5/use")
    assert resp.status_code == 404


def test_use_account_rejects_remote_client(db):
    """Reachable remote dashboards remain read-only for credential mutation."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    with TestClient(app, client=("100.64.0.9", 50000)) as remote:
        resp = remote.post("/api/auth/accounts/1/use")

    assert resp.status_code == 403
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["error"]["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"


def test_refresh_usage_uses_fresh_token_for_active_account(client, tmp_path):
    """Usage refresh reads fresh token from credential stores for active account.
    Only passes the fresh token when it differs from the DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="fresh_token_from_keychain",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={
                "five_hour": {"utilization": 0.5},
                "seven_day": {"utilization": 0.3},
            },
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    mock_fresh.assert_called_once_with(1)
    # DB token is "at_1", fresh token is different → should be passed
    assert (
        mock_fetch.call_args.kwargs.get("access_token") == "fresh_token_from_keychain"
    )


def test_refresh_usage_skips_fresh_when_unchanged(client, tmp_path):
    """When fresh token matches DB token, don't pass it (preserves cache guard)."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="at_1",  # same as DB token for account 1
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={
                "five_hour": {"utilization": 0.5},
                "seven_day": {"utilization": 0.3},
            },
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # Token unchanged → access_token should be None (cache guard intact)
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_usage_falls_back_to_db_token(client, tmp_path):
    """When credential stores don't have this account, falls back to DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value=None,
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={
                "five_hour": {"utilization": 0.5},
                "seven_day": {"utilization": 0.3},
            },
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # No fresh token → access_token should be None
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_all_usage_only_reads_fresh_for_active(client, db, tmp_path):
    """Bulk refresh uses canonical identity for the fresh-token account."""

    call_tokens = {}

    async def capture_fetch_usage(account_id, db_arg, access_token=None, manual=False):
        call_tokens[account_id] = access_token
        return {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.2}}

    with (
        mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
        mock.patch.dict(
            "jacked.api.routes.auth._active_account_cache",
            {"id": None, "expires_at": 0.0},
            clear=True,
        ),
        mock.patch(
            "jacked.credentials.runtime.resolve_active_identity",
            return_value=ResolverObservation(
                ResolverState.RESOLVED,
                CredentialIdentity(account_id=1),
                ("authority:ok",),
            ),
        ),
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="cc_refreshed_token",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            side_effect=capture_fetch_usage,
        ),
    ):
        resp = client.post("/api/auth/accounts/refresh-all-usage")

    assert resp.status_code == 200
    # Account 1 is active — should get fresh token (differs from DB "at_1")
    assert call_tokens.get(1) == "cc_refreshed_token"
    # Account 3 is not active — should get None (DB token used internally)
    assert call_tokens.get(3) is None
    # read_fresh_active_token called only for the active account
    mock_fresh.assert_called_once_with(1)


def test_active_credential_uses_canonical_resolver(client, app):
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED,
        CredentialIdentity(account_id=1),
        ("authority:macOS Keychain:ok", "required_mirror:file:ok"),
    )
    resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == 1
    assert data["email"] == "alice@test.com"
    assert data["state"] == "resolved"
    assert len(data["evidence"]) == 2


def test_active_credential_never_guesses_from_email(client, app):
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.UNSUPPORTED,
        CredentialIdentity(email="alice@test.com"),
        ("exact build/config capability is not certified",),
    )
    resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] is None
    assert data["email"] is None
    assert data["state"] == "unsupported"


def test_active_credential_account_stamp_disambiguates_org(client, app, db):
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (10, 'shared@test.com', 'org-aaa', 'at_10', 'rt_10',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (11, 'shared@test.com', 'org-bbb', 'at_11', 'rt_11',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )

    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED,
        CredentialIdentity(account_id=11, organization_id="org-bbb"),
        ("authority:ok",),
    )
    resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == 11
    assert data["email"] == "shared@test.com"


def test_active_credential_reports_org_conflict(client, app, db):
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (20, 'same@test.com', '', 'at_20', 'rt_20',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (21, 'same@test.com', 'org-xyz', 'at_21', 'rt_21',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )

    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED,
        CredentialIdentity(account_id=20, organization_id="org-xyz"),
        ("authority:ok",),
    )
    resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] is None
    assert data["state"] == "conflict"
    assert "account-organization-conflict" in data["evidence"]


def test_active_credential_no_match(client, app):
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.MISSING, CredentialIdentity(), ("authority:missing",)
    )
    resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] is None
    assert data["state"] == "missing"


def test_session_states_never_claims_runtime_identity(client, db):
    """Configuration evidence stays distinct from provider runtime proof."""
    db.record_session_account(
        "session-qualified",
        account_id=1,
        email="alice@test.com",
        detection_method="resolver_observation",
        credential_scope="global",
        observed_at=datetime.now(timezone.utc).isoformat(),
        evidence="resolver_observation",
        observation_state="observed",
        credential_revision="rev-1",
    )

    resp = client.get("/api/auth/session-states")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    session = next(s for s in resp.json()["sessions"] if s["session_id"] == "ualified")
    assert session["observed_configuration"]["account_id"] == 1
    assert session["runtime_verified"] is None
    assert session["scope"] == "global"


def test_session_states_prefers_desired_observed_pointer(client, db):
    db.set_setting("active_account_id", "1")
    db.set_setting("desired_account_id", "2")

    response = client.get("/api/auth/session-states")

    assert response.status_code == 200
    assert response.json()["desired_global"]["account_id"] == 2
    assert response.json()["desired_global"]["email"] == "bob@test.com"

    db.set_setting("desired_account_id", "999")
    fallback = client.get("/api/auth/session-states")
    assert fallback.json()["desired_global"]["account_id"] == 1


def test_legacy_session_is_unknown_not_observed(client, db):
    db.record_session_account(
        "session-legacy",
        account_id=1,
        email="alice@test.com",
        detection_method="session_start",
    )

    resp = client.get("/api/auth/session-states")

    session = next(s for s in resp.json()["sessions"] if s["session_id"] == "n-legacy")
    assert session["scope"] == "legacy"
    assert session["state"] == "unknown"
    assert session["observed_configuration"] is None


def test_active_sessions_compatibility_route_declares_semantics(client, db):
    db.record_session_account("session-old", account_id=1, email="alice@test.com")

    resp = client.get("/api/auth/active-sessions")

    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["deprecated"] is True
    assert resp.json()["identity_semantics"] == "historical_observation"


def test_session_states_preserves_earliest_started_identity(client, db):
    db.record_session_account(
        "session-change",
        account_id=1,
        email="alice@test.com",
        detection_method="session_start",
        credential_scope="global",
        observation_state="observed",
    )
    db.record_session_account(
        "session-change",
        account_id=2,
        email="bob@test.com",
        detection_method="resolver_observation",
        credential_scope="global",
        observed_at=datetime.now(timezone.utc).isoformat(),
        evidence="resolver_observation",
        observation_state="observed",
        credential_revision="rev-2",
    )

    session = client.get("/api/auth/session-states").json()["sessions"][0]

    assert session["started_as"]["account_id"] == 1
    assert session["observed_configuration"]["account_id"] == 2


def test_use_account_unfenced_switch_gets_residency(client, app, db):
    """On macOS every switch reports observed_target_unfenced. That outcome
    still names the account the authority holds, so the DB pointer, the swap
    notice and the auto-swap pause must all follow it."""
    app.state.credential_switcher.return_value = _switch_result(
        SwitchOutcome.OBSERVED_TARGET_UNFENCED
    )
    before = datetime.now(timezone.utc)
    with (
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap") as mock_note,
    ):
        response = client.post("/api/auth/accounts/1/use")

    assert response.status_code == 202
    assert response.json()["status"] == "observed_target_unfenced"
    assert db.get_setting("active_account_id") == "1"
    mock_note.assert_called_once_with()
    paused = datetime.fromisoformat(db.get_setting("auto_swap_paused_until"))
    assert paused - before >= timedelta(minutes=14, seconds=50)


def test_use_account_committed_switch_records_the_active_pointer(client, db):
    with (
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        response = client.post("/api/auth/accounts/1/use")

    assert response.status_code == 200
    assert db.get_setting("active_account_id") == "1"


# ---------------------------------------------------------------------------
# Remote-access scope gate on credential mutation
#
# Credential switching is no longer loopback-only: it is allowed from a remote
# browser exactly when the persisted remote-access setting is on AND the client
# address falls inside the enabled scope. These tests drive the real route with
# Starlette's ``client=`` kwarg, which populates ``scope["client"]``.
# ---------------------------------------------------------------------------

_PAGE_SESSION_HEADERS = {"X-Jacked-Page-Session": "page-session-123456"}

TAILNET_IP = "100.116.47.72"
LAN_IP = "192.168.42.5"

# (host, remote_access_enabled, remote_access_scope, gate_passes)
_SCOPE_MATRIX = [
    ("127.0.0.1", False, "tailscale", True),
    (TAILNET_IP, False, "tailscale", False),
    (TAILNET_IP, True, "all", True),
    (LAN_IP, True, "all", True),
    (TAILNET_IP, True, "tailscale", True),
    (LAN_IP, True, "tailscale", False),
]
_SCOPE_IDS = [
    "loopback-off",
    "tailnet-off",
    "tailnet-all",
    "lan-all",
    "tailnet-tailscale",
    "lan-tailscale",
]


def _set_remote_access(db, enabled, scope):
    db.set_setting("remote_access_enabled", "true" if enabled else "false")
    db.set_setting("remote_access_scope", scope)


def _scoped_client(app, host):
    return TestClient(app, client=(host, 50000), headers=_PAGE_SESSION_HEADERS)


@pytest.mark.parametrize(
    "host,enabled,scope,allowed", _SCOPE_MATRIX, ids=_SCOPE_IDS
)
def test_use_account_honours_the_remote_access_scope(app, db, host, enabled, scope, allowed):
    _set_remote_access(db, enabled, scope)
    scoped = _scoped_client(app, host)

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = scoped.post("/api/auth/accounts/1/use")

    if allowed:
        assert resp.status_code == 200, resp.text
        app.state.credential_switcher.assert_called_once()
    else:
        assert resp.status_code == 403
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json()["error"]["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"
        app.state.credential_switcher.assert_not_called()


@pytest.mark.parametrize(
    "host,enabled,scope,allowed", _SCOPE_MATRIX, ids=_SCOPE_IDS
)
def test_credential_operation_status_honours_the_remote_access_scope(
    app, db, host, enabled, scope, allowed
):
    _set_remote_access(db, enabled, scope)
    scoped = _scoped_client(app, host)

    resp = scoped.get("/api/auth/credential-operations/operation-scope-test-1234")

    if allowed:
        # Past the scope gate: a well-formed but unknown id is a plain 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "CREDENTIAL_OPERATION_NOT_FOUND"
    else:
        assert resp.status_code == 403
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json()["error"]["code"] == "CREDENTIAL_STATUS_LOCAL_ONLY"


def test_credential_denial_copy_is_user_facing_clean():
    """Every denial sentence must name where to change the policy and carry
    no em-dash (these strings are shown to a user)."""
    from jacked.api.routes.auth import (
        _STATUS_DENIED_MESSAGES,
        _SWITCH_DENIED_MESSAGES,
    )

    messages = list(_SWITCH_DENIED_MESSAGES.values()) + list(
        _STATUS_DENIED_MESSAGES.values()
    )
    assert len(messages) == 4
    for message in messages:
        assert "—" not in message
        assert "Settings > Advanced" in message
        assert "host machine" in message


@pytest.mark.parametrize(
    "host,enabled,scope,allowed", _SCOPE_MATRIX, ids=_SCOPE_IDS
)
def test_active_credential_reports_activation_permission_when_resolved(
    app, db, host, enabled, scope, allowed
):
    """The dashboard reads this flag to decide whether to offer Use Account."""
    _set_remote_access(db, enabled, scope)
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED,
        CredentialIdentity(account_id=1),
        ("authority:macOS Keychain:ok",),
    )

    resp = _scoped_client(app, host).get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "resolved"
    assert data["account_id"] == 1
    assert data["allow_credential_activation"] is allowed


@pytest.mark.parametrize(
    "host,enabled,scope,allowed", _SCOPE_MATRIX, ids=_SCOPE_IDS
)
def test_active_credential_reports_activation_permission_when_unresolved(
    app, db, host, enabled, scope, allowed
):
    """The unresolved early-return path carries the flag too, or a remote
    dashboard with an unreadable keychain would silently re-enable the button."""
    _set_remote_access(db, enabled, scope)
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.UNSUPPORTED,
        CredentialIdentity(email="alice@test.com"),
        ("exact build/config capability is not certified",),
    )

    resp = _scoped_client(app, host).get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "unsupported"
    assert data["allow_credential_activation"] is allowed


def test_active_credential_without_a_db_still_reports_permission():
    """No DB means no setting to read, so the answer is loopback-only."""
    from fastapi import FastAPI

    bare = FastAPI()
    bare.include_router(router, prefix="/api/auth")
    bare.state.db = None

    local = TestClient(bare, client=("127.0.0.1", 50000))
    remote = TestClient(bare, client=(TAILNET_IP, 50000))

    assert local.get("/api/auth/active-credential").json()[
        "allow_credential_activation"
    ] is True
    assert remote.get("/api/auth/active-credential").json()[
        "allow_credential_activation"
    ] is False


# ---------------------------------------------------------------------------
# Reason-specific denial copy
# ---------------------------------------------------------------------------

SWITCH_DENIED = {
    "remote_access_off": (
        "Account switching from this browser is off. Turn on remote access "
        "under Settings > Advanced on the host machine."
    ),
    "outside_scope": (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}
STATUS_DENIED = {
    "remote_access_off": (
        "Credential operation status from this browser is off. Turn on remote "
        "access under Settings > Advanced on the host machine."
    ),
    "outside_scope": (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}


@pytest.mark.parametrize(
    "host,enabled,scope,reason",
    [
        (TAILNET_IP, False, "tailscale", "remote_access_off"),
        (LAN_IP, True, "tailscale", "outside_scope"),
    ],
)
def test_use_403_message_matches_the_reason(app, db, host, enabled, scope, reason):
    """The two denials have different fixes (flip a toggle vs widen a scope),
    so they must not share one sentence."""
    _set_remote_access(db, enabled, scope)

    resp = _scoped_client(app, host).post("/api/auth/accounts/1/use")

    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"
    assert body["message"] == SWITCH_DENIED[reason]
    assert "—" not in body["message"]


@pytest.mark.parametrize(
    "host,enabled,scope,reason",
    [
        (TAILNET_IP, False, "tailscale", "remote_access_off"),
        (LAN_IP, True, "tailscale", "outside_scope"),
    ],
)
def test_status_403_message_matches_the_reason(app, db, host, enabled, scope, reason):
    _set_remote_access(db, enabled, scope)

    resp = _scoped_client(app, host).get(
        "/api/auth/credential-operations/operation-scope-test-1234"
    )

    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["code"] == "CREDENTIAL_STATUS_LOCAL_ONLY"
    assert body["message"] == STATUS_DENIED[reason]
    assert "—" not in body["message"]


@pytest.mark.parametrize(
    "host,enabled,scope,reason",
    [
        ("127.0.0.1", False, "tailscale", None),
        (TAILNET_IP, False, "tailscale", "remote_access_off"),
        (LAN_IP, True, "tailscale", "outside_scope"),
        (TAILNET_IP, True, "tailscale", None),
        (LAN_IP, True, "all", None),
    ],
)
def test_active_credential_publishes_the_reason(app, db, host, enabled, scope, reason):
    _set_remote_access(db, enabled, scope)
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED, CredentialIdentity(account_id=1), ("authority:ok",)
    )

    data = _scoped_client(app, host).get("/api/auth/active-credential").json()

    assert data["credential_activation_reason"] == reason
    assert data["allow_credential_activation"] is (reason is None)


def test_allow_credential_activation_is_a_required_field():
    """No default, so a forgotten kwarg on a new return path is a construction
    error here rather than a silently-enabled button in a remote browser."""
    from pydantic import ValidationError

    from jacked.api.routes.auth import ActiveCredentialResponse

    with pytest.raises(ValidationError):
        ActiveCredentialResponse()
    ok = ActiveCredentialResponse(allow_credential_activation=False)
    assert ok.credential_activation_reason is None


def test_active_credential_stamp_not_found_path_carries_the_flag(app, db):
    """Mid-function return: the resolver resolved to an account row that is
    gone."""
    _set_remote_access(db, False, "tailscale")
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED, CredentialIdentity(account_id=999), ("authority:ok",)
    )

    data = _scoped_client(app, TAILNET_IP).get("/api/auth/active-credential").json()

    assert data["state"] == "unusable"
    assert "account-stamp-not-found" in data["evidence"]
    assert data["allow_credential_activation"] is False
    assert data["credential_activation_reason"] == "remote_access_off"


def test_active_credential_org_conflict_path_carries_the_flag(app, db):
    """Mid-function return: the observed org does not match the account row."""
    _set_remote_access(db, True, "tailscale")
    with db._writer() as conn:
        conn.execute("UPDATE accounts SET organization_uuid='org-aaa' WHERE id=1")
    app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED,
        CredentialIdentity(account_id=1, organization_id="org-bbb"),
        ("authority:ok",),
    )

    denied = _scoped_client(app, LAN_IP).get("/api/auth/active-credential").json()
    allowed = _scoped_client(app, TAILNET_IP).get("/api/auth/active-credential").json()

    assert denied["state"] == "conflict"
    assert "account-organization-conflict" in denied["evidence"]
    assert denied["allow_credential_activation"] is False
    assert denied["credential_activation_reason"] == "outside_scope"
    assert allowed["allow_credential_activation"] is True
    assert allowed["credential_activation_reason"] is None


# ---------------------------------------------------------------------------
# A settings DB that cannot be read must deny, not 500
# ---------------------------------------------------------------------------


class _LockedSettingsDB:
    """Wraps the real DB but raises on the two remote-access reads, the shape
    a locked or corrupt settings file takes."""

    def __init__(self, inner):
        self._inner = inner

    def get_setting(self, key):
        if key.startswith("remote_access_"):
            raise sqlite3.OperationalError("database is locked")
        return self._inner.get_setting(key)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def locked_settings_app(app, db):
    app.state.db = _LockedSettingsDB(db)
    return app


def test_locked_settings_db_denies_a_remote_switch_without_a_500(locked_settings_app):
    resp = _scoped_client(locked_settings_app, TAILNET_IP).post(
        "/api/auth/accounts/1/use"
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"


def test_locked_settings_db_still_allows_a_local_switch(locked_settings_app):
    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = _scoped_client(locked_settings_app, "127.0.0.1").post(
            "/api/auth/accounts/1/use"
        )

    assert resp.status_code == 200


def test_locked_settings_db_keeps_active_credential_a_200(locked_settings_app):
    """This route is polled; a raising settings read must not turn it into a
    500 storm."""
    locked_settings_app.state.credential_resolver = lambda: ResolverObservation(
        ResolverState.RESOLVED, CredentialIdentity(account_id=1), ("authority:ok",)
    )

    resp = _scoped_client(locked_settings_app, TAILNET_IP).get(
        "/api/auth/active-credential"
    )

    assert resp.status_code == 200
    assert resp.json()["allow_credential_activation"] is False
    assert resp.json()["credential_activation_reason"] == "remote_access_off"


# ---------------------------------------------------------------------------
# CSRF: being in scope is not a licence for a cross-site POST
# ---------------------------------------------------------------------------


def test_in_scope_remote_switch_still_refuses_a_foreign_origin(app, db):
    from jacked.api.security import HostValidationMiddleware

    _set_remote_access(db, True, "tailscale")
    guarded = TestClient(
        HostValidationMiddleware(app),
        client=(TAILNET_IP, 50000),
        headers={
            **_PAGE_SESSION_HEADERS,
            "Origin": "http://evil.example",
        },
    )

    resp = guarded.post("/api/auth/accounts/1/use")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"
    app.state.credential_switcher.assert_not_called()


# ---------------------------------------------------------------------------
# Proxy headers: the forwarded address is the client, and only from loopback
# ---------------------------------------------------------------------------


def _proxied_client(app, peer, forwarded_for):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    return TestClient(
        ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1"),
        client=(peer, 1),
        headers={**_PAGE_SESSION_HEADERS, "X-Forwarded-For": forwarded_for},
    )


def test_tailscale_serve_viewer_is_not_treated_as_loopback(app, db):
    """`tailscale serve` proxies from loopback and rewrites X-Forwarded-For to
    the real tailnet source. Trusting that rewrite is what stops a serve
    viewer from inheriting loopback's unconditional permission."""
    _set_remote_access(db, False, "tailscale")

    resp = _proxied_client(app, "127.0.0.1", TAILNET_IP).post(
        "/api/auth/accounts/1/use"
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"
    app.state.credential_switcher.assert_not_called()


def test_forwarded_header_from_an_untrusted_peer_is_ignored(app, db):
    """A LAN client cannot claim loopback by sending its own X-Forwarded-For:
    the header is only honored from the trusted loopback proxy."""
    _set_remote_access(db, False, "tailscale")

    resp = _proxied_client(app, LAN_IP, "127.0.0.1").post("/api/auth/accounts/1/use")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CREDENTIAL_MUTATION_LOCAL_ONLY"
    app.state.credential_switcher.assert_not_called()


def test_in_scope_forwarded_client_is_allowed_when_remote_access_is_on(app, db):
    _set_remote_access(db, True, "tailscale")

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        resp = _proxied_client(app, "127.0.0.1", TAILNET_IP).post(
            "/api/auth/accounts/1/use"
        )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limit on remote switches
# ---------------------------------------------------------------------------


def _switch_headers(n):
    return {
        **_PAGE_SESSION_HEADERS,
        "X-Jacked-Action-Id": f"action-ratelimit-{n:06d}",
        "X-Jacked-Operation-Id": f"operation-ratelimit-{n:06d}",
    }


def test_remote_switches_are_rate_limited(app, db):
    """Each accepted switch drives a Keychain write on the host, and a fresh
    action id per attempt defeats the idempotency replay, so an in-scope peer
    could otherwise thrash the live credential store."""
    _set_remote_access(db, True, "tailscale")
    remote = _scoped_client(app, TAILNET_IP)

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        codes = [
            remote.post("/api/auth/accounts/1/use", headers=_switch_headers(i))
            for i in range(7)
        ]

    assert [r.status_code for r in codes[:6]] == [200] * 6
    limited = codes[6]
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "CREDENTIAL_SWITCH_RATE_LIMITED"
    assert limited.json()["error"]["message"] == (
        "Too many account switches from this browser. Wait a minute and try again."
    )
    assert int(limited.headers["retry-after"]) >= 1


def test_loopback_switches_are_never_rate_limited(app, db):
    _set_remote_access(db, False, "tailscale")
    local = _scoped_client(app, "127.0.0.1")

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        codes = [
            local.post("/api/auth/accounts/1/use", headers=_switch_headers(i)).status_code
            for i in range(9)
        ]

    assert codes == [200] * 9


def test_a_denied_remote_client_never_consumes_switch_budget(app, db):
    """The limiter runs AFTER the mutation gate, so a peer that is not allowed
    to switch cannot burn the budget of one that is."""
    _set_remote_access(db, False, "tailscale")
    remote = _scoped_client(app, TAILNET_IP)

    for i in range(10):
        assert remote.post(
            "/api/auth/accounts/1/use", headers=_switch_headers(i)
        ).status_code == 403

    # Now turn remote access on: the full budget is still available.
    _set_remote_access(db, True, "tailscale")
    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        after = [
            remote.post(
                "/api/auth/accounts/1/use", headers=_switch_headers(100 + i)
            ).status_code
            for i in range(6)
        ]
    assert after == [200] * 6


# ---------------------------------------------------------------------------
# Observability + audit trail
# ---------------------------------------------------------------------------


def test_denied_remote_mutation_is_logged_with_its_reason(app, db, caplog):
    _set_remote_access(db, True, "tailscale")

    with caplog.at_level(logging.INFO, logger="jacked.api.routes.auth"):
        _scoped_client(app, LAN_IP).post("/api/auth/accounts/1/use")

    assert LAN_IP in caplog.text
    assert "outside_scope" in caplog.text


def test_allowed_remote_mutation_is_logged(app, db, caplog):
    _set_remote_access(db, True, "tailscale")

    with (
        caplog.at_level(logging.INFO, logger="jacked.api.routes.auth"),
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        _scoped_client(app, TAILNET_IP).post("/api/auth/accounts/1/use")

    assert TAILNET_IP in caplog.text
    # Never the credentials themselves.
    assert "cc_at_1" not in caplog.text
    assert "at_1" not in caplog.text


def test_loopback_mutation_is_not_logged_per_request(app, db, caplog):
    """Only non-loopback decisions are worth a line; the local dashboard
    switches accounts all day."""
    _set_remote_access(db, False, "tailscale")

    with (
        caplog.at_level(logging.INFO, logger="jacked.api.routes.auth"),
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        _scoped_client(app, "127.0.0.1").post("/api/auth/accounts/1/use")

    assert "credential mutation" not in caplog.text.lower()


def test_remote_switch_is_attributed_in_the_swap_broadcast(app, db):
    """Swap History must distinguish a switch made from another machine."""
    _set_remote_access(db, True, "tailscale")
    broadcasts = []

    class _Registry:
        async def broadcast(self, event, payload):
            broadcasts.append((event, payload))

    app.state.ws_registry = _Registry()

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        _scoped_client(app, TAILNET_IP).post("/api/auth/accounts/1/use")

    reasons = [p["reason"] for e, p in broadcasts if e == "auto_swap_triggered"]
    assert reasons and reasons[0].endswith(f" from {TAILNET_IP}")


def test_local_switch_reason_is_unchanged(app, db):
    broadcasts = []

    class _Registry:
        async def broadcast(self, event, payload):
            broadcasts.append((event, payload))

    app.state.ws_registry = _Registry()

    with (
        mock.patch("jacked.api.credential_helpers.reconcile_outgoing_credentials"),
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
        mock.patch("jacked.api.usage_monitor.note_external_swap"),
    ):
        _scoped_client(app, "127.0.0.1").post("/api/auth/accounts/1/use")

    reasons = [p["reason"] for e, p in broadcasts if e == "auto_swap_triggered"]
    assert reasons and " from " not in reasons[0]
