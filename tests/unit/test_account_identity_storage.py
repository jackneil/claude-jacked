"""AC1: jacked stores the Claude account identity from the source.

Covers:
- the accounts table gains account_uuid / profile_json / profile_fetched_at
  on a fresh DB, a pre-provider DB and a pre-organization DB, and keeps rows
- create_account / update_account accept the new columns
- _update_profile_metadata applies NOTHING unless the profile email and org
  match the row, and on a match stores the uuid, the profile subset and the
  fetch time
- fetch_profile(allow_refresh=False) does not refresh on 401/403
- the token exchange carries account.uuid, and every persistence path (add,
  re-auth, org redirect, CC flow) stores it on the row it updates

Fixtures use the real key shapes: the profile endpoint returns
account.email, the token exchange returns account.email_address.
"""

import asyncio
import json
import logging
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web.database import Database

IDENTITY_COLS = {"account_uuid", "profile_json", "profile_fetched_at"}

ALICE_UUID = "4f1c2a9e-1111-4c3b-9d2e-aaaaaaaaaaaa"
ORG_A = "9b0e7d55-2222-4a1f-8c3d-bbbbbbbbbbbb"
ORG_B = "1d2c3b4a-3333-4e5f-9a8b-cccccccccccc"


def _profile(
    email="alice@test.com",
    uuid=ALICE_UUID,
    org_uuid=ORG_A,
    org_name="Acme",
    **org_extra,
) -> dict:
    """A profile response with the real key shape (key paths only)."""
    org = {
        "uuid": org_uuid,
        "name": org_name,
        "organization_type": "claude_max",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_20x",
        "seat_tier": None,
        "has_extra_usage_enabled": True,
        "subscription_created_at": "2025-03-01T10:00:00Z",
        "claude_code_trial_ends_at": None,
        "claude_code_trial_duration_days": None,
    }
    org.update(org_extra)
    return {
        "account": {
            "uuid": uuid,
            "email": email,
            "display_name": "Alice",
            "full_name": "Alice Example",
            "created_at": "2024-11-02T08:30:00Z",
        },
        "organization": org,
    }


def _token_response(
    email="alice@test.com", uuid=ALICE_UUID, org_uuid=ORG_A, org_name="Acme"
) -> dict:
    """A token-exchange response with the real key shape."""
    return {
        "access_token": "sk-ant-oat01-access",
        "refresh_token": "sk-ant-ort01-refresh",
        "expires_in": 28800,
        "scope": "user:inference user:profile",
        "account": {"uuid": uuid, "email_address": email},
        "organization": {"uuid": org_uuid, "name": org_name},
    }


def _columns(db: Database) -> set:
    with db._reader() as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}


def _make_db(tmp_path, org_uuid=ORG_A) -> tuple[Database, dict]:
    db = Database(str(tmp_path / "test.db"))
    acct = db.create_account(
        email="alice@test.com",
        access_token="sk-ant-oat01-alice",
        refresh_token="sk-ant-ort01-alice",
        expires_at=int(time.time()) + 3600,
        organization_uuid=org_uuid,
        organization_name="Old Name" if org_uuid else None,
    )
    return db, acct


# ---------------------------------------------------------------------------
# Schema + migration
# ---------------------------------------------------------------------------

# Accounts DDL as it shipped before the provider column (org columns present).
PRE_PROVIDER_DDL = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    organization_uuid TEXT DEFAULT '',
    organization_name TEXT,
    display_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,
    scopes TEXT,
    subscription_type TEXT,
    rate_limit_tier TEXT,
    has_extra_usage BOOLEAN DEFAULT FALSE,
    priority INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_deleted BOOLEAN DEFAULT FALSE,
    last_used_at TIMESTAMP,
    cached_usage_5h REAL,
    cached_usage_7d REAL,
    cached_5h_resets_at TEXT,
    cached_7d_resets_at TEXT,
    usage_cached_at INTEGER,
    cached_usage_raw TEXT,
    last_error TEXT,
    last_error_at TIMESTAMP,
    consecutive_failures INTEGER DEFAULT 0,
    last_validated_at INTEGER,
    validation_status TEXT DEFAULT 'unknown',
    cc_access_token TEXT,
    cc_refresh_token TEXT,
    cc_expires_at INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(email, organization_uuid)
);
"""

# Accounts DDL from before multi-org: no organization_uuid/name, no cc_*,
# no cached_usage_raw. Every later migration must still land on it.
PRE_ORG_DDL = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,
    scopes TEXT,
    subscription_type TEXT,
    rate_limit_tier TEXT,
    has_extra_usage BOOLEAN DEFAULT FALSE,
    priority INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_deleted BOOLEAN DEFAULT FALSE,
    last_used_at TIMESTAMP,
    cached_usage_5h REAL,
    cached_usage_7d REAL,
    cached_5h_resets_at TEXT,
    cached_7d_resets_at TEXT,
    usage_cached_at INTEGER,
    last_error TEXT,
    last_error_at TIMESTAMP,
    consecutive_failures INTEGER DEFAULT 0,
    last_validated_at INTEGER,
    validation_status TEXT DEFAULT 'unknown',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _seed_legacy(db_path: str, ddl: str, insert_sql: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(ddl)
    conn.execute(insert_sql)
    conn.commit()
    conn.close()


class TestIdentityColumns:
    def test_fresh_db_has_identity_columns(self, tmp_path):
        db = Database(str(tmp_path / "fresh.db"))
        try:
            assert IDENTITY_COLS <= _columns(db)
        finally:
            db.close()

    def test_pre_provider_db_gains_columns_and_keeps_rows(self, tmp_path):
        path = str(tmp_path / "legacy.db")
        _seed_legacy(
            path,
            PRE_PROVIDER_DDL,
            "INSERT INTO accounts (email, organization_uuid, access_token, "
            "expires_at) VALUES ('alice@test.com', 'org-1', 'tok', 9999999999)",
        )
        db = Database(path)
        try:
            assert IDENTITY_COLS <= _columns(db)
            row = db.get_account_by_email("alice@test.com", provider="claude")
            assert row is not None
            assert row["organization_uuid"] == "org-1"
            assert row["account_uuid"] is None
            assert row["profile_json"] is None
            assert row["profile_fetched_at"] is None
        finally:
            db.close()

    def test_pre_org_db_gains_columns_and_keeps_rows(self, tmp_path):
        """The explicit-column org rebuild must not drop the new columns."""
        path = str(tmp_path / "ancient.db")
        _seed_legacy(
            path,
            PRE_ORG_DDL,
            "INSERT INTO accounts (email, access_token, expires_at) "
            "VALUES ('alice@test.com', 'tok', 9999999999)",
        )
        db = Database(path)
        try:
            cols = _columns(db)
            assert IDENTITY_COLS <= cols
            assert {"organization_uuid", "provider"} <= cols
            row = db.get_account_by_email("alice@test.com", provider="claude")
            assert row is not None
            assert row["access_token"] == "tok"
            assert row["account_uuid"] is None
        finally:
            db.close()

    def test_reopen_is_idempotent_and_keeps_identity(self, tmp_path):
        path = str(tmp_path / "t.db")
        db = Database(path)
        acct = db.create_account(
            "alice@test.com", "tok", 9999999999, account_uuid=ALICE_UUID
        )
        db.update_account(acct["id"], profile_json='{"account": {}}', profile_fetched_at=1.5)
        db.close()
        db2 = Database(path)
        try:
            row = db2.get_account(acct["id"])
            assert row["account_uuid"] == ALICE_UUID
            assert row["profile_json"] == '{"account": {}}'
            assert row["profile_fetched_at"] == 1.5
        finally:
            db2.close()


class TestCreateAndUpdate:
    def test_create_account_stores_account_uuid(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        acct = db.create_account(
            "alice@test.com", "tok", 9999999999, account_uuid=ALICE_UUID
        )
        assert acct["account_uuid"] == ALICE_UUID

    def test_create_account_default_uuid_is_none(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        acct = db.create_account("alice@test.com", "tok", 9999999999)
        assert acct["account_uuid"] is None

    def test_upsert_keeps_existing_uuid_when_new_is_none(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.create_account("alice@test.com", "tok", 9999999999, account_uuid=ALICE_UUID)
        again = db.create_account("alice@test.com", "tok2", 9999999999)
        assert again["account_uuid"] == ALICE_UUID
        assert again["access_token"] == "tok2"

    def test_upsert_replaces_uuid_when_new_is_known(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.create_account("alice@test.com", "tok", 9999999999, account_uuid="old-uuid")
        again = db.create_account(
            "alice@test.com", "tok2", 9999999999, account_uuid=ALICE_UUID
        )
        assert again["account_uuid"] == ALICE_UUID

    def test_update_account_accepts_identity_columns(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        acct = db.create_account("alice@test.com", "tok", 9999999999)
        assert db.update_account(
            acct["id"],
            account_uuid=ALICE_UUID,
            profile_json="{}",
            profile_fetched_at=123.0,
        )
        row = db.get_account(acct["id"])
        assert row["account_uuid"] == ALICE_UUID
        assert row["profile_json"] == "{}"
        assert row["profile_fetched_at"] == 123.0

    def test_account_model_carries_identity_fields(self, tmp_path):
        from jacked.web.database import Account

        db = Database(str(tmp_path / "t.db"))
        acct = db.create_account(
            "alice@test.com", "tok", 9999999999, account_uuid=ALICE_UUID
        )
        model = Account(**acct)
        assert model.account_uuid == ALICE_UUID
        assert model.profile_json is None
        assert model.profile_fetched_at is None


class TestClaudeProfileHelpers:
    def test_lists_only_claude_rows_missing_profile(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        a = db.create_account("a@test.com", "tok", 9999999999)
        b = db.create_account("b@test.com", "tok", 9999999999)
        db.update_account(b["id"], profile_json="{}")
        c = db.create_account("c@test.com", "tok", 9999999999, provider="codex")
        d = db.create_account("d@test.com", "tok", 9999999999)
        db.delete_account(d["id"])
        e = db.create_account("e@test.com", "tok", 9999999999)
        db.update_account(e["id"], is_active=False)

        ids = [r["id"] for r in db.list_claude_accounts_missing_profile()]
        assert ids == [a["id"], e["id"]]
        assert c["id"] not in ids

    def test_coverage_counts(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        a = db.create_account("a@test.com", "tok", 9999999999, account_uuid="u-a")
        db.update_account(a["id"], profile_json="{}")
        db.create_account("b@test.com", "tok", 9999999999)
        db.create_account("c@test.com", "tok", 9999999999, provider="codex")
        cov = db.claude_identity_coverage()
        assert cov == {
            "total": 2,
            "with_uuid": 1,
            "without_uuid": 1,
            "with_profile": 1,
            "without_profile": 1,
        }


# ---------------------------------------------------------------------------
# _update_profile_metadata guard + storage
# ---------------------------------------------------------------------------


class TestProfileMetadataGuard:
    def test_match_stores_identity_and_metadata(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        data = _profile()
        before = time.time()
        _update_profile_metadata(acct["id"], data, db)
        row = db.get_account(acct["id"])

        assert row["account_uuid"] == ALICE_UUID
        stored = json.loads(row["profile_json"])
        assert stored == {"account": data["account"], "organization": data["organization"]}
        assert row["profile_fetched_at"] >= before
        assert row["subscription_type"] == "max"
        assert row["rate_limit_tier"] == "default_claude_max_20x"
        assert row["has_extra_usage"] == 1
        assert row["organization_name"] == "Acme"

    def test_profile_subset_drops_other_top_level_keys(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        data = _profile()
        data["application"] = {"uuid": "app-x"}
        _update_profile_metadata(acct["id"], data, db)
        stored = json.loads(db.get_account(acct["id"])["profile_json"])
        assert set(stored) == {"account", "organization"}

    def test_email_match_is_casefold_and_stripped(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        _update_profile_metadata(acct["id"], _profile(email="  ALICE@Test.com "), db)
        assert db.get_account(acct["id"])["account_uuid"] == ALICE_UUID

    def test_email_mismatch_applies_nothing(self, tmp_path, caplog):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        before = db.get_account(acct["id"])
        with caplog.at_level(logging.WARNING, logger="jacked.auth"):
            _update_profile_metadata(acct["id"], _profile(email="bob@test.com"), db)
        after = db.get_account(acct["id"])
        assert after == before
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "a mismatch must log a WARNING"
        text = " ".join(r.getMessage() for r in warnings)
        assert str(acct["id"]) in text
        assert "bob@test.com" not in text
        assert "alice@test.com" not in text
        assert ALICE_UUID not in text

    def test_org_mismatch_applies_nothing(self, tmp_path, caplog):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        before = db.get_account(acct["id"])
        with caplog.at_level(logging.WARNING, logger="jacked.auth"):
            _update_profile_metadata(
                acct["id"], _profile(org_uuid=ORG_B, org_name="Other"), db
            )
        assert db.get_account(acct["id"]) == before
        text = " ".join(r.getMessage() for r in caplog.records)
        assert str(acct["id"]) in text
        assert ORG_B not in text and ORG_A not in text

    def test_missing_org_on_org_row_applies_nothing(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        before = db.get_account(acct["id"])
        data = _profile()
        del data["organization"]
        _update_profile_metadata(acct["id"], data, db)
        assert db.get_account(acct["id"]) == before

    def test_empty_row_org_accepts_any_profile_org(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path, org_uuid="")
        _update_profile_metadata(acct["id"], _profile(org_uuid=ORG_B), db)
        row = db.get_account(acct["id"])
        assert row["account_uuid"] == ALICE_UUID
        assert json.loads(row["profile_json"])["organization"]["uuid"] == ORG_B
        # The row org stays the sentinel: the profile never re-keys the row.
        assert row["organization_uuid"] == ""

    def test_missing_account_object_applies_nothing(self, tmp_path, caplog):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        before = db.get_account(acct["id"])
        data = _profile()
        del data["account"]
        with caplog.at_level(logging.WARNING, logger="jacked.auth"):
            _update_profile_metadata(acct["id"], data, db)
        assert db.get_account(acct["id"]) == before
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_legacy_email_address_key_is_not_accepted(self, tmp_path):
        """The profile key is account.email; a token-exchange shaped dict
        (account.email_address) is not proof of the profile owner."""
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        before = db.get_account(acct["id"])
        data = _profile()
        data["account"] = {"email_address": "alice@test.com", "uuid": ALICE_UUID}
        _update_profile_metadata(acct["id"], data, db)
        assert db.get_account(acct["id"]) == before

    def test_missing_uuid_stores_profile_without_uuid(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        db.update_account(acct["id"], account_uuid="kept-uuid")
        data = _profile()
        del data["account"]["uuid"]
        _update_profile_metadata(acct["id"], data, db)
        row = db.get_account(acct["id"])
        assert row["account_uuid"] == "kept-uuid"
        assert row["profile_json"] is not None

    def test_unknown_row_is_a_noop(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, _ = _make_db(tmp_path)
        assert _update_profile_metadata(9999, _profile(), db) is False

    def test_returns_whether_the_profile_was_applied(self, tmp_path):
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path)
        assert _update_profile_metadata(acct["id"], _profile(email="bob@test.com"), db) is False
        assert _update_profile_metadata(acct["id"], _profile(), db) is True

    def test_personal_profile_without_org_stores_null_org(self, tmp_path):
        """profile_json holds the organization value exactly as returned: a
        profile with no organization stores null, not an invented {}."""
        from jacked.web.auth import _update_profile_metadata

        db, acct = _make_db(tmp_path, org_uuid="")
        data = _profile()
        del data["organization"]
        assert _update_profile_metadata(acct["id"], data, db) is True
        row = db.get_account(acct["id"])
        stored = json.loads(row["profile_json"])
        assert stored == {"account": data["account"], "organization": None}
        assert row["account_uuid"] == ALICE_UUID


# ---------------------------------------------------------------------------
# fetch_profile(allow_refresh=...)
# ---------------------------------------------------------------------------


def _client_returning(*responses):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=list(responses))
    return client


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    return r


class TestFetchProfileRefreshControl:
    @pytest.mark.parametrize("status", [401, 403])
    def test_no_refresh_when_disallowed(self, tmp_path, status):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        refresh = AsyncMock(return_value="fresh")
        with patch.object(auth.httpx, "AsyncClient", return_value=_client_returning(_resp(status))), \
             patch.object(auth, "_try_refresh_primary_token", refresh):
            result = asyncio.run(auth.fetch_profile(acct["id"], db, allow_refresh=False))
        assert result is None
        refresh.assert_not_awaited()

    def test_default_still_refreshes_on_401(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        refresh = AsyncMock(return_value="fresh")
        client = _client_returning(_resp(401), _resp(200, _profile()))
        with patch.object(auth.httpx, "AsyncClient", return_value=client), \
             patch.object(auth, "_try_refresh_primary_token", refresh):
            result = asyncio.run(auth.fetch_profile(acct["id"], db))
        assert result is not None
        refresh.assert_awaited_once()
        assert db.get_account(acct["id"])["account_uuid"] == ALICE_UUID

    def test_success_stores_identity(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(_resp(200, _profile()))):
            result = asyncio.run(auth.fetch_profile(acct["id"], db, allow_refresh=False))
        assert result["account"]["uuid"] == ALICE_UUID
        row = db.get_account(acct["id"])
        assert row["account_uuid"] == ALICE_UUID
        assert row["profile_json"] is not None

    def test_logs_applied_or_rejected(self, tmp_path, caplog):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        with caplog.at_level(logging.INFO, logger="jacked.auth"), \
             patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(_resp(200, _profile()))):
            asyncio.run(auth.fetch_profile(acct["id"], db, allow_refresh=False))
        text = " ".join(r.getMessage() for r in caplog.records)
        assert f"Profile applied for account {acct['id']}" in text
        assert "rejected" not in text

        caplog.clear()
        foreign = _profile(email="bob@test.com")
        with caplog.at_level(logging.INFO, logger="jacked.auth"), \
             patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(_resp(200, foreign))):
            result = asyncio.run(auth.fetch_profile(acct["id"], db, allow_refresh=False))
        # Public contract unchanged: the raw profile is still returned.
        assert result == foreign
        text = " ".join(r.getMessage() for r in caplog.records)
        assert f"Profile rejected for account {acct['id']}" in text
        assert "applied for" not in text
        assert "bob@test.com" not in text

    def test_internal_helper_reports_applied(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(
                              _resp(200, _profile(email="bob@test.com")),
                              _resp(200, _profile()),
                          )):
            rejected = asyncio.run(
                auth._fetch_profile_applied(acct["id"], db, allow_refresh=False)
            )
            applied = asyncio.run(
                auth._fetch_profile_applied(acct["id"], db, allow_refresh=False)
            )
        assert rejected[0] is not None and rejected[1] is False
        assert applied[0] is not None and applied[1] is True


class TestValidateStoresIdentity:
    def test_validate_success_stores_identity(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(_resp(200, _profile()))):
            result = asyncio.run(auth.validate_account(acct["id"], db))
        assert result["valid"] is True
        assert db.get_account(acct["id"])["account_uuid"] == ALICE_UUID

    def test_validate_foreign_profile_stores_nothing(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(
                              _resp(200, _profile(email="bob@test.com")))):
            asyncio.run(auth.validate_account(acct["id"], db))
        row = db.get_account(acct["id"])
        assert row["account_uuid"] is None
        assert row["profile_json"] is None


class TestValidateKeepsUsageErrors:
    """A 200 from the profile API proves the token, not that usage is
    readable. Validation must leave the usage path's error and failure
    counter alone; only a successful usage fetch clears them."""

    def test_success_keeps_usage_error_and_counter(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        db.record_account_error(acct["id"], "Usage fetch failed (HTTP 500)")
        db.record_account_error(acct["id"], "Usage fetch failed (HTTP 500)")
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(_resp(200, _profile()))):
            result = asyncio.run(auth.validate_account(acct["id"], db))
        assert result == {"valid": True, "error": None}
        row = db.get_account(acct["id"])
        assert row["validation_status"] == "valid"
        assert row["last_validated_at"]
        assert row["last_error"] == "Usage fetch failed (HTTP 500)"
        assert row["last_error_at"] is not None
        assert row["consecutive_failures"] == 2

        db.clear_account_errors(acct["id"])  # what a successful fetch_usage does
        row = db.get_account(acct["id"])
        assert row["last_error"] is None and row["consecutive_failures"] == 0

    def test_success_after_refresh_keeps_usage_error_and_counter(self, tmp_path):
        import jacked.web.auth as auth

        db, acct = _make_db(tmp_path)
        db.record_account_error(acct["id"], "Usage fetch failed (HTTP 500)")
        with patch.object(auth.httpx, "AsyncClient",
                          return_value=_client_returning(
                              _resp(401), _resp(200, _profile()))), \
             patch.object(auth, "_try_refresh_primary_token",
                          AsyncMock(return_value="fresh-token")):
            result = asyncio.run(auth.validate_account(acct["id"], db))
        assert result == {"valid": True, "error": None}
        row = db.get_account(acct["id"])
        assert row["validation_status"] == "valid"
        assert row["last_error"] == "Usage fetch failed (HTTP 500)"
        assert row["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# OAuth token exchange + persistence paths
# ---------------------------------------------------------------------------


class TestTokenExchangeUuid:
    def _exchange(self, body):
        from jacked.web.oauth import OAuthFlow

        flow = OAuthFlow(MagicMock(), purpose="primary")
        client = AsyncMock()
        client.post = AsyncMock(return_value=_resp(200, body))
        return asyncio.run(flow._exchange_code(client, "code"))

    def test_exchange_sets_account_uuid(self):
        tokens = self._exchange(_token_response())
        assert tokens["account_uuid"] == ALICE_UUID
        assert tokens["email"] == "alice@test.com"
        assert tokens["organization_uuid"] == ORG_A

    def test_exchange_without_account_sets_none(self):
        body = _token_response()
        del body["account"]
        tokens = self._exchange(body)
        assert tokens["account_uuid"] is None

    def test_exchange_null_account_sets_none(self):
        body = _token_response()
        body["account"] = None
        tokens = self._exchange(body)
        assert tokens["account_uuid"] is None


def _tokens_after_exchange(**kw) -> dict:
    """What _exchange_code hands to the store paths."""
    raw = _token_response(**kw)
    return {
        **raw,
        "email": raw["account"]["email_address"],
        "account_uuid": raw["account"]["uuid"],
        "organization_uuid": raw["organization"]["uuid"],
        "organization_name": raw["organization"]["name"],
    }


class TestStorePathsPersistUuid:
    def test_add_stores_uuid_and_profile(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db = Database(str(tmp_path / "t.db"))
        flow = OAuthFlow(db, purpose="primary")
        account = flow._store_account(_tokens_after_exchange(), _profile(), {})
        row = db.get_account(account["id"])
        assert row["account_uuid"] == ALICE_UUID
        assert json.loads(row["profile_json"])["account"]["uuid"] == ALICE_UUID
        assert row["profile_fetched_at"] is not None

    def test_add_with_foreign_profile_keeps_uuid_but_no_profile(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db = Database(str(tmp_path / "t.db"))
        flow = OAuthFlow(db, purpose="primary")
        tokens = _tokens_after_exchange()
        tokens["subscription_type"] = "pro"
        tokens["rate_limit_tier"] = "token_tier"
        # The foreign profile says max / 20x / extra usage on / "Alice".
        account = flow._store_account(tokens, _profile(email="bob@test.com"), {})
        row = db.get_account(account["id"])
        assert row["account_uuid"] == ALICE_UUID  # from the token exchange
        assert row["profile_json"] is None
        # Plan metadata comes from the token response, never the foreign
        # profile.
        assert row["subscription_type"] == "pro"
        assert row["rate_limit_tier"] == "token_tier"
        assert not row["has_extra_usage"]
        assert row["display_name"] is None

    def test_reauth_with_foreign_profile_keeps_token_metadata(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        tokens = _tokens_after_exchange()
        tokens["subscription_type"] = "pro"
        tokens["rate_limit_tier"] = "token_tier"
        flow = OAuthFlow(db, purpose="primary", target_account_id=acct["id"])
        flow._store_account(
            tokens, _profile(org_uuid=ORG_B, org_name="Other"), {}
        )
        row = db.get_account(acct["id"])
        assert row["subscription_type"] == "pro"
        assert row["rate_limit_tier"] == "token_tier"
        assert not row["has_extra_usage"]
        assert row["profile_json"] is None

    def test_org_redirect_log_uses_uuid_prefixes(self, tmp_path, caplog):
        from jacked.web.oauth import OAuthFlow

        db, a = _make_db(tmp_path, org_uuid=ORG_A)
        db.create_account(
            "alice@test.com", "tok-b", int(time.time()) + 3600,
            organization_uuid=ORG_B, organization_name="Beta",
        )
        flow = OAuthFlow(db, purpose="primary", target_account_id=a["id"])
        with caplog.at_level(logging.INFO, logger="jacked.oauth"):
            flow._store_account(
                _tokens_after_exchange(org_uuid=ORG_B, org_name="Beta"), {}, {}
            )
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "Re-auth org mismatch" in text
        assert ORG_A[:8] in text and ORG_B[:8] in text
        assert ORG_A not in text and ORG_B not in text


class TestOAuthErrorLogging:
    """A failed completion logs the exception TYPE only. The exception text
    can carry emails (the re-auth identity check names both), so it goes to
    the UI error field, never the log."""

    _MSG = "Re-auth email (bob@test.com) does not match target (alice@test.com)"

    def _flow(self):
        from jacked.web.oauth import OAuthFlow

        flow = OAuthFlow(MagicMock(), purpose="primary")
        flow._state = "state-1"
        flow._complete_auth = AsyncMock(side_effect=ValueError(self._MSG))
        return flow

    def _assert_values_free(self, flow, caplog):
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "the failure must still be logged"
        text = " ".join(r.getMessage() for r in errors)
        assert "ValueError" in text
        assert "bob@test.com" not in text and "alice@test.com" not in text
        assert flow._status == "error"
        assert flow._error == self._MSG

    def test_callback_failure_logs_type_only(self, caplog):
        flow = self._flow()
        req = MagicMock()
        req.query = {"code": "c", "state": "state-1"}
        with caplog.at_level(logging.ERROR, logger="jacked.oauth"):
            asyncio.run(flow._handle_callback(req))
        self._assert_values_free(flow, caplog)

    def test_manual_failure_logs_type_only(self, caplog):
        flow = self._flow()
        flow.manual = True
        with caplog.at_level(logging.ERROR, logger="jacked.oauth"):
            asyncio.run(flow.submit_code("c#state-1"))
        self._assert_values_free(flow, caplog)

    def test_reauth_stores_uuid(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="primary", target_account_id=acct["id"])
        flow._store_account(_tokens_after_exchange(), {}, {})
        assert db.get_account(acct["id"])["account_uuid"] == ALICE_UUID

    def test_reauth_without_uuid_keeps_existing(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        db.update_account(acct["id"], account_uuid="kept")
        tokens = _tokens_after_exchange()
        tokens["account_uuid"] = None
        flow = OAuthFlow(db, purpose="primary", target_account_id=acct["id"])
        flow._store_account(tokens, {}, {})
        assert db.get_account(acct["id"])["account_uuid"] == "kept"

    def test_org_redirect_stores_uuid_on_actual_target(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, a = _make_db(tmp_path, org_uuid=ORG_A)
        b = db.create_account(
            "alice@test.com", "tok-b", int(time.time()) + 3600,
            organization_uuid=ORG_B, organization_name="Beta",
        )
        flow = OAuthFlow(db, purpose="primary", target_account_id=a["id"])
        account = flow._store_account(
            _tokens_after_exchange(org_uuid=ORG_B, org_name="Beta"), {}, {}
        )
        assert account["id"] == b["id"]
        assert db.get_account(b["id"])["account_uuid"] == ALICE_UUID
        assert db.get_account(a["id"])["account_uuid"] is None

    def test_cc_flow_stores_uuid_after_identity_check(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        result = flow._store_account(_tokens_after_exchange(), {}, {})
        assert result["cc_access_token"] == "sk-ant-oat01-access"
        assert result["account_uuid"] == ALICE_UUID

    def test_cc_flow_email_mismatch_stores_nothing(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        with pytest.raises(ValueError):
            flow._store_account(_tokens_after_exchange(email="bob@test.com"), {}, {})
        assert db.get_account(acct["id"])["account_uuid"] is None

    def test_cc_flow_org_mismatch_stores_nothing(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        with pytest.raises(ValueError):
            flow._store_account(_tokens_after_exchange(org_uuid=ORG_B), {}, {})
        assert db.get_account(acct["id"])["account_uuid"] is None

    def test_cc_flow_reads_profile_email_key(self, tmp_path):
        """With no email in the token response, the profile's account.email
        (the real profile key) identifies the CC login."""
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        tokens = _tokens_after_exchange()
        del tokens["email"]
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        result = flow._store_cc_tokens(tokens, _profile())
        assert result["cc_access_token"] == "sk-ant-oat01-access"

    def test_cc_flow_profile_email_mismatch_raises(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        tokens = _tokens_after_exchange()
        del tokens["email"]
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])
        with pytest.raises(ValueError, match="does not match"):
            flow._store_cc_tokens(tokens, _profile(email="bob@test.com"))


class TestCcTokenIdentityGuards:
    """Review wave 2: the CC flow must not land a token the published
    identity would mislabel."""

    def test_empty_org_row_refuses_a_token_for_another_stored_org(self, tmp_path):
        import json as _json

        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path, org_uuid="")
        db.update_account(acct["id"], profile_json=_json.dumps(_profile(org_uuid=ORG_A)))
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])

        with pytest.raises(ValueError, match="organization stored"):
            flow._store_cc_tokens(_tokens_after_exchange(org_uuid=ORG_B), {})
        assert db.get_account(acct["id"])["cc_access_token"] is None

    def test_empty_org_row_accepts_the_stored_org(self, tmp_path):
        import json as _json

        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path, org_uuid="")
        db.update_account(acct["id"], profile_json=_json.dumps(_profile(org_uuid=ORG_A)))
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])

        result = flow._store_cc_tokens(_tokens_after_exchange(org_uuid=ORG_A), {})
        assert result["cc_access_token"] == "sk-ant-oat01-access"

    def test_empty_org_row_without_a_stored_profile_keeps_the_old_rule(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path, org_uuid="")
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])

        result = flow._store_cc_tokens(_tokens_after_exchange(org_uuid=ORG_B), {})
        assert result["cc_access_token"] == "sk-ant-oat01-access"

    def test_a_token_for_another_account_uuid_is_refused(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db, acct = _make_db(tmp_path)
        db.update_account(acct["id"], account_uuid="1f1f1f1f-0000-4000-8000-000000000001")
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=acct["id"])

        with pytest.raises(ValueError, match="account does not match"):
            flow._store_cc_tokens(_tokens_after_exchange(), {})


class TestProfileOwnerUuid:
    def test_a_known_row_uuid_must_match_the_profile(self, tmp_path):
        from jacked.web.auth import _profile_owner_mismatch

        row = {"email": "alice@test.com", "organization_uuid": ORG_A,
               "account_uuid": "1f1f1f1f-0000-4000-8000-000000000001"}

        assert _profile_owner_mismatch(row, _profile()) == "account.uuid"
        assert _profile_owner_mismatch({**row, "account_uuid": None}, _profile()) is None
