"""The launch token sync never re-adopts a condemned Claude Code refresh token.

After an ``invalid_grant`` the clean-up removes the failed refresh token from
the row (``cc_refresh_token`` is NULL), so the fingerprint in
``cc_refresh_failed_token_fp`` is the only record of which token died
(#152i). The launch sync reads that column with the row
(``launch._sync_row``) and guards its compare-and-swap writes on it
(``launch_sync_writes._sync_guards``), so the shared dead-chain rule
(``credential_helpers.dead_chain_adoption``) can refuse the failed token when
a per-account file still holds it. A cleared breaker does not release a
condemned token either: a chain that adopted a new lineage still refuses the
token that died before it.

No test reaches the network, the Keychain or a real credential store.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from jacked.api.credential_helpers import clear_ownership_cache
from jacked.credentials.oauth_extra import captured_tier
from jacked.web.refresh_verdict import token_fingerprint
from tests.unit.launch_sync_helpers import (
    FUTURE_MS,
    OWNER,
    VERIFIED_AT,
    _account_dir,
    _Lookup,
    _make_row,
    _patched,
    _profile,
    _sync,
    _write_credentials,
)

ADOPTED = (
    "Account 1: adopted the live Claude Code refresh lineage after invalid_grant "
    "(ownership proven)"
)
CONDEMNED_LINE = (
    "Account 1: the per-account file holds a Claude Code refresh token that an "
    "earlier invalid_grant condemned; only the access token is imported"
)
TIER_FIELDS = {
    "subscriptionType": "max",
    "subscriptionTypeSource": "profile",
    "subscriptionTypeVerifiedAt": VERIFIED_AT,
}
# The verdict the clean-up leaves behind for the failed token "row-rt-1".
CONDEMNED = (None, "invalid_grant", 123, token_fingerprint("row-rt-1"))


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    clear_ownership_cache()
    yield
    clear_ownership_cache()


def _cleaned_up(db, failed="row-rt-1"):
    """Row 1 as the invalid_grant clean-up leaves it: the failed refresh token
    is gone, and only its fingerprint names it."""
    db.update_account(
        1,
        cc_refresh_token=None,
        cc_refresh_failure_type="invalid_grant",
        cc_refresh_last_failed_at=123,
        cc_refresh_failed_token_fp=token_fingerprint(failed),
    )


def _verdict(db) -> tuple:
    row = db.get_account(1)
    return (
        row["cc_refresh_token"],
        row["cc_refresh_failure_type"],
        row["cc_refresh_last_failed_at"],
        row["cc_refresh_failed_token_fp"],
    )


def _lineage(db) -> tuple:
    row = db.get_account(1)
    return (
        row["cc_access_token"],
        row["cc_refresh_token"],
        row["cc_refresh_failure_type"],
        row["cc_refresh_last_failed_at"],
    )


def _set_fingerprints(tmp_path, value):
    """Write the fingerprint column the way a concurrent writer would."""
    conn = sqlite3.connect(str(tmp_path / "test.db"), timeout=1.0)
    try:
        conn.execute("UPDATE accounts SET cc_refresh_failed_token_fp = ? WHERE id = 1", (value,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A dead chain after the clean-up (F-1)
# ---------------------------------------------------------------------------


class TestTheFingerprintCondemnsTheFailedToken:
    def test_the_failed_token_beside_the_rows_access_token_is_not_adopted(
        self, tmp_path, caplog
    ):
        """The held-access path: the row holds the file's access token."""
        db = _make_row(tmp_path)
        _cleaned_up(db)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "row-at-1", "row-rt-1", **TIER_FIELDS)
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert _verdict(db) == CONDEMNED
        assert row["cc_access_token"] == "row-at-1"
        assert row["cc_oauth_extra"] is None
        assert lookup.calls == 0
        assert ADOPTED not in caplog.text

    def test_the_failed_token_beside_a_rotated_access_token_moves_the_access_token_only(
        self, tmp_path, caplog
    ):
        """The rotation path: the access token and its expiry move, the
        condemned refresh token does not."""
        db = _make_row(tmp_path)
        _cleaned_up(db)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "row-rt-1", **TIER_FIELDS)

        with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert _verdict(db) == CONDEMNED
        assert row["cc_access_token"] == "rot-at-2"
        assert row["cc_expires_at"] == FUTURE_MS // 1000
        assert row["cc_oauth_extra"] is None
        assert ADOPTED not in caplog.text

    @pytest.mark.parametrize("file_access", ["row-at-1", "rot-at-2"])
    def test_a_different_new_token_is_adopted_and_clears_the_breaker(
        self, tmp_path, caplog, file_access
    ):
        db = _make_row(tmp_path)
        _cleaned_up(db)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, file_access, "rot-rt-2", **TIER_FIELDS)

        with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == (file_access, "rot-rt-2", None, None)
        assert captured_tier(db.get_account(1)["cc_oauth_extra"], "rot-rt-2") == "max"
        assert caplog.text.count(ADOPTED) == 1

    def test_a_condemnation_recorded_during_the_lookup_is_decided_again(self, tmp_path):
        """The fingerprint is part of the compare-and-swap guard: the decision
        read it, so a write decided against an older value never lands."""
        db = _make_row(tmp_path)
        _cleaned_up(db)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        both = f"{token_fingerprint('rot-rt-2')} {token_fingerprint('row-rt-1')}"

        lookup = _Lookup(_profile(OWNER), on_call=lambda: _set_fingerprints(tmp_path, both))
        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"
        assert _lineage(db) == ("row-at-1", None, "invalid_grant", 123)

        with _patched(_Lookup(_profile(OWNER))):
            assert _sync(config_dir, tmp_path) == "imported"
        assert _lineage(db) == ("rot-at-2", None, "invalid_grant", 123)
        assert db.get_account(1)["cc_refresh_failed_token_fp"] == both

    def test_a_schema_without_the_fingerprint_column_still_syncs(self, tmp_path):
        db = _make_row(tmp_path)
        db.update_account(
            1,
            cc_refresh_token=None,
            cc_refresh_failure_type="invalid_grant",
            cc_refresh_last_failed_at=123,
        )
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        try:
            conn.execute("ALTER TABLE accounts DROP COLUMN cc_refresh_failed_token_fp")
            conn.commit()
        finally:
            conn.close()
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        with _patched(_Lookup(_profile(OWNER))):
            assert _sync(config_dir, tmp_path) == "imported"

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        try:
            row = conn.execute(
                "SELECT cc_access_token, cc_refresh_token, cc_refresh_failure_type, "
                "cc_refresh_last_failed_at FROM accounts WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("rot-at-2", "rot-rt-2", None, None)


# ---------------------------------------------------------------------------
# A live chain keeps the condemnation (the class of F-3)
# ---------------------------------------------------------------------------


class TestALiveChainKeepsTheCondemnation:
    def _revived(self, tmp_path):
        db = _make_row(tmp_path)
        _cleaned_up(db)
        # The shared rule adopted a new lineage: the breaker is clear and the
        # fingerprint of the dead token stays.
        db.update_account(
            1,
            cc_access_token="row-at-2",
            cc_refresh_token="row-rt-2",
            cc_refresh_failure_type=None,
            cc_refresh_last_failed_at=None,
        )
        return db

    def test_a_rotation_beside_the_condemned_token_moves_the_access_token_only(
        self, tmp_path, caplog
    ):
        db = self._revived(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-3", "row-rt-1", **TIER_FIELDS)

        with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert _lineage(db) == ("rot-at-3", "row-rt-2", None, None)
        assert row["cc_expires_at"] == FUTURE_MS // 1000
        assert row["cc_refresh_failed_token_fp"] == token_fingerprint("row-rt-1")
        assert row["cc_oauth_extra"] is None
        lines = [r.getMessage() for r in caplog.records if "condemned" in r.getMessage()]
        assert lines == [CONDEMNED_LINE]
        assert "row-rt-1" not in caplog.text

    def test_a_rotation_with_a_new_token_is_imported_as_before(self, tmp_path):
        db = self._revived(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-3", "rot-rt-3", **TIER_FIELDS)

        with _patched(_Lookup(_profile(OWNER))):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == ("rot-at-3", "rot-rt-3", None, None)
        assert captured_tier(db.get_account(1)["cc_oauth_extra"], "rot-rt-3") == "max"

    def test_write_rotation_refuses_only_the_refresh_token_part(self, tmp_path):
        """F-8: write_rotation's live branch asks refresh_token_importable."""
        from jacked.launch import _sync_row
        from jacked.launch_sync_writes import write_rotation

        self._revived(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
            account = _sync_row(conn, 1, cols)
            oauth = {"accessToken": "rot-at-3", "refreshToken": "row-rt-1",
                     "expiresAt": FUTURE_MS}
            assert write_rotation(conn, account, oauth, cols, 1) == "imported"
            row = conn.execute(
                "SELECT cc_access_token, cc_refresh_token, cc_expires_at, "
                "cc_refresh_failed_token_fp FROM accounts WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("rot-at-3", "row-rt-2", FUTURE_MS // 1000, token_fingerprint("row-rt-1"))
