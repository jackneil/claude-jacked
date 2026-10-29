"""Claude Code's claudeAiOauth pass-through fields survive a switch (#174i).

Claude Code writes refreshTokenExpiresAt (and, when it applies, clientId and
subscriptionTypeSource) into its credential object. A switch rebuilds the
object from the account row, so jacked dropped them on every switch. They are
now kept per account in accounts.cc_oauth_extra.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jacked.api.credential_helpers import build_oauth_data
from jacked.credentials.canonical import CredentialPayload
from jacked.web.database import Database

RT_EXPIRES = 1_900_000_000_000


def _row(**extra):
    row = {
        "access_token": "primary-at",
        "refresh_token": "primary-rt",
        "expires_at": int(time.time()) + 3600,
        "cc_access_token": "cc-at",
        "cc_refresh_token": "cc-rt",
        "cc_expires_at": int(time.time()) + 3600,
        "subscription_type": "max",
        "rate_limit_tier": "t1",
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# build_oauth_data: emit when known, never invent
# ---------------------------------------------------------------------------


def test_known_fields_are_written_back():
    extra = json.dumps({"refreshTokenExpiresAt": RT_EXPIRES, "clientId": "c-1",
                        "subscriptionTypeSource": "sign_in"})
    oauth = build_oauth_data(_row(cc_oauth_extra=extra))
    assert oauth["refreshTokenExpiresAt"] == RT_EXPIRES
    assert oauth["clientId"] == "c-1"
    assert oauth["subscriptionTypeSource"] == "sign_in"


def test_unknown_fields_are_absent_never_invented():
    oauth = build_oauth_data(_row())
    for key in ("refreshTokenExpiresAt", "clientId", "subscriptionTypeSource"):
        assert key not in oauth


def test_refresh_expiry_goes_only_with_a_refresh_token():
    extra = json.dumps({"refreshTokenExpiresAt": RT_EXPIRES, "clientId": "c-1"})
    oauth = build_oauth_data(_row(cc_refresh_token=None, cc_oauth_extra=extra))
    assert "refreshTokenExpiresAt" not in oauth
    assert oauth["clientId"] == "c-1"


def test_the_primary_fallback_carries_none_of_them():
    extra = json.dumps({"refreshTokenExpiresAt": RT_EXPIRES})
    oauth = build_oauth_data(_row(cc_access_token=None, cc_oauth_extra=extra))
    assert "refreshTokenExpiresAt" not in oauth


def test_a_stored_value_cannot_smuggle_a_token_field():
    extra = json.dumps({"accessToken": "evil", "refreshToken": "evil", "clientId": "c"})
    oauth = build_oauth_data(_row(cc_oauth_extra=extra))
    assert oauth["accessToken"] == "cc-at" and oauth["refreshToken"] == "cc-rt"


# ---------------------------------------------------------------------------
# switch -> reconcile -> switch keeps the value
# ---------------------------------------------------------------------------


def _owner_profile(email):
    from jacked.api.authority_guard import ProfileLookup

    return ProfileLookup(200, {"account": {"email": email, "uuid": "uuid-a"},
                               "organization": {"uuid": "org-a"}})


def _reconcile(db, aid, live):
    from jacked.api.credential_helpers import (
        invalidate_live_cred_cache,
        reconcile_credentials_from_live_store,
    )

    invalidate_live_cred_cache()
    with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=live), \
         patch("jacked.api.credential_helpers._ownership_lookup",
               return_value=_owner_profile("owner@test.com")):
        return reconcile_credentials_from_live_store(aid, db)


def test_switch_reconcile_switch_keeps_refresh_token_expiry(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    aid = db.create_account("owner@test.com", "primary-at", int(time.time()) + 3600)["id"]
    db.update_account(aid, cc_access_token="cc-at", cc_refresh_token="cc-rt",
                      cc_expires_at=int(time.time()) + 3600,
                      organization_uuid="org-a", account_uuid="uuid-a")

    # 1. Switch to the account: jacked does not know the field yet.
    first = build_oauth_data(db.get_account(aid))
    assert "refreshTokenExpiresAt" not in first

    # 2. Claude Code rotates and writes the field; reconcile reads it.
    live_oauth = {**first, "accessToken": "cc-at-2", "refreshToken": "cc-rt-2",
                  "expiresAt": (int(time.time()) + 7200) * 1000,
                  "refreshTokenExpiresAt": RT_EXPIRES, "subscriptionTypeSource": "profile"}
    assert _reconcile(db, aid, {"_jackedAccountId": aid, "claudeAiOauth": live_oauth}) is True

    # 3. Switch back: the field is written again, unchanged.
    second = build_oauth_data(db.get_account(aid))
    assert second["refreshTokenExpiresAt"] == RT_EXPIRES
    assert second["subscriptionTypeSource"] == "profile"

    # 4. A reconcile of what jacked wrote changes nothing.
    assert _reconcile(db, aid, {"_jackedAccountId": aid, "claudeAiOauth": second}) is True
    assert build_oauth_data(db.get_account(aid)) == second


def test_the_drift_warning_no_longer_fires_for_carried_fields(caplog):
    from jacked.credentials.transaction import _warn_on_schema_drift

    stored = json.dumps({"refreshTokenExpiresAt": RT_EXPIRES, "subscriptionTypeSource": "sign_in"})
    target = build_oauth_data(_row(cc_oauth_extra=stored))
    authority = {**target, "refreshTokenExpiresAt": RT_EXPIRES - 5,
                 "subscriptionTypeSource": "sign_in"}
    before = SimpleNamespace(payload=CredentialPayload.from_mapping({"claudeAiOauth": authority}))
    request = SimpleNamespace(payload=CredentialPayload.from_mapping({"claudeAiOauth": target}))
    with caplog.at_level(logging.WARNING, logger="jacked.credentials.transaction"):
        _warn_on_schema_drift(before, request)
    assert "Credential schema drift" not in caplog.text

    # An unknown future key still warns (it is not carried).
    authority["someFutureKey"] = 1
    before = SimpleNamespace(payload=CredentialPayload.from_mapping({"claudeAiOauth": authority}))
    with caplog.at_level(logging.WARNING, logger="jacked.credentials.transaction"):
        _warn_on_schema_drift(before, request)
    assert "someFutureKey" in caplog.text


def test_a_dead_lineage_does_not_take_the_fields(tmp_path):
    """Under invalid_grant the live refresh token is not imported, so the
    expiry that describes it is not taken either."""
    db = Database(str(tmp_path / "t.db"))
    aid = db.create_account("owner@test.com", "primary-at", int(time.time()) + 3600)["id"]
    db.update_account(aid, cc_access_token="cc-at", cc_refresh_token="cc-rt",
                      cc_expires_at=int(time.time()) + 600, cc_refresh_failure_type="invalid_grant",
                      organization_uuid="org-a", account_uuid="uuid-a")
    live = {"_jackedAccountId": aid, "claudeAiOauth": {
        "accessToken": "cc-at-2", "refreshToken": "cc-rt-2",
        "expiresAt": (int(time.time()) + 7200) * 1000, "refreshTokenExpiresAt": RT_EXPIRES}}
    _reconcile(db, aid, live)
    assert db.get_account(aid)["cc_oauth_extra"] is None


# ---------------------------------------------------------------------------
# jacked's own refresh follows Claude Code's rule
# ---------------------------------------------------------------------------


def _refresh(db, aid, result):
    import asyncio

    from jacked.web import auth

    with patch.object(auth, "_exchange_refresh_token", AsyncMock(return_value=result)):
        return asyncio.run(auth._refresh_token_flow(aid, db, auth.RefreshMode.CC))


@pytest.mark.parametrize("sent, expected", [(3600, "from-response"), (None, "kept")])
def test_a_cc_refresh_takes_the_response_expiry_or_keeps_the_old_one(tmp_path, sent, expected):
    from jacked.web.auth import TokenExchangeResult

    db = Database(str(tmp_path / "t.db"))
    aid = db.create_account("owner@test.com", "primary-at", int(time.time()) + 3600)["id"]
    db.update_account(aid, cc_access_token="cc-at", cc_refresh_token="cc-rt",
                      cc_expires_at=int(time.time()) - 10,
                      cc_oauth_extra=json.dumps({"refreshTokenExpiresAt": 123, "clientId": "c"}))
    result = TokenExchangeResult(
        success=True, access_token="cc-at-2", refresh_token="cc-rt-2", expires_in=3600,
        refresh_token_expires_at_ms=(int(time.time() * 1000) + sent * 1000) if sent else None,
    )
    _refresh(db, aid, result)
    row = db.get_account(aid)
    assert row["cc_access_token"] == "cc-at-2"
    extra = json.loads(row["cc_oauth_extra"])
    assert extra["clientId"] == "c"
    if expected == "kept":
        assert extra["refreshTokenExpiresAt"] == 123
    else:
        assert extra["refreshTokenExpiresAt"] > int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Scoped file sync carries them
# ---------------------------------------------------------------------------


def test_the_scoped_file_sync_carries_the_fields(tmp_path):
    from jacked.launch import _sync_tokens_from_file

    db = Database(str(tmp_path / "test.db"))
    aid = db.create_account("owner@test.com", "primary-at", int(time.time()) + 3600)["id"]
    db.update_account(aid, cc_access_token="old")
    config_dir = tmp_path / "accounts" / str(aid)
    config_dir.mkdir(parents=True)
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "new", "refreshToken": "new-rt", "expiresAt": 9_999_999_999_000,
        "refreshTokenExpiresAt": RT_EXPIRES}}))
    _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))
    assert json.loads(db.get_account(aid)["cc_oauth_extra"]) == {"refreshTokenExpiresAt": RT_EXPIRES}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_the_column_is_added_to_an_existing_database_without_touching_rows(tmp_path):
    path = tmp_path / "old.db"
    db = Database(str(path))
    aid = db.create_account("owner@test.com", "primary-at", 9_999_999_999)["id"]
    db.update_account(aid, cc_access_token="cc-at", cc_refresh_token="cc-rt")
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE accounts DROP COLUMN cc_oauth_extra")  # the pre-migration schema
    conn.commit()
    before = conn.execute("SELECT id, email, cc_access_token, cc_refresh_token FROM accounts").fetchall()
    conn.close()

    Database(str(path))  # migrates
    Database(str(path))  # and is idempotent

    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    after = conn.execute("SELECT id, email, cc_access_token, cc_refresh_token FROM accounts").fetchall()
    conn.close()
    assert "cc_oauth_extra" in cols
    assert after == before
