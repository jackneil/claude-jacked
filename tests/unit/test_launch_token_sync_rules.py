"""The launch token sync rules that need no profile request, and the loop (#158i).

A token that the row holds already is proven offline. The sync skips a file
with nothing to copy, and a compare-and-swap protects the write. A database
from before the cc_* columns gets the tokens in its primary columns. The
polling loop tries a deferred sync again, and both capture sites bind the
tier (#230i).

The ownership cases are in test_launch_token_sync.py. The shared helpers are
in launch_sync_helpers.py.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from jacked.api import credential_helpers
from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import clear_ownership_cache
from jacked.credentials.oauth_extra import captured_tier
from tests._platform import requires_symlinks
from tests.unit.launch_sync_helpers import (
    INTRUDER,
    ORG,
    OWNER,
    VERIFIED_AT,
    _account_dir,
    _cc,
    _Lookup,
    _make_row,
    _patched,
    _profile,
    _sync,
    _write_credentials,
)


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    """Verdicts are cached per (account id, token hash): start each test clean."""
    clear_ownership_cache()
    yield
    clear_ownership_cache()


# ---------------------------------------------------------------------------
# Offline proof, cheap skips, and the CAS
# ---------------------------------------------------------------------------


class TestNoLookupWhenNoneIsNeeded:
    def test_a_refresh_token_match_imports_with_zero_lookups(self, tmp_path):
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "row-rt-1")
        lookup = _Lookup(ProfileLookup(None))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _cc(db) == ("rot-at-2", "row-rt-1", "valid")
        assert lookup.calls == 0

    def test_the_rows_own_access_token_takes_its_fields_with_zero_lookups(self, tmp_path):
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(
            config_dir, "row-at-1", "row-rt-1", subscriptionType="max",
            subscriptionTypeSource="profile", subscriptionTypeVerifiedAt=VERIFIED_AT,
        )
        lookup = _Lookup(ProfileLookup(None))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert row["cc_access_token"] == "row-at-1"
        # capture_oauth_extra at the unchanged-token site too (#230i).
        assert captured_tier(row["cc_oauth_extra"], "row-rt-1") == "max"
        assert lookup.calls == 0

    def test_a_row_without_a_claude_code_token_is_skipped_with_zero_lookups(self, tmp_path):
        db = _make_row(tmp_path, cc_at=None, cc_rt=None)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert _cc(db)[:2] == (None, None)
        assert lookup.calls == 0

    @pytest.mark.parametrize(
        "case",
        [
            "no_file",
            "no_access_token",
            "not_an_account_dir",
            "no_row",
            pytest.param("symlinked_file", marks=requires_symlinks),
        ],
    )
    def test_nothing_to_copy_is_unchanged_with_zero_lookups(self, tmp_path, case):
        db = _make_row(tmp_path)
        name = {"not_an_account_dir": "not-an-id", "no_row": "42"}.get(case, "1")
        config_dir = _account_dir(tmp_path, name)
        if case == "no_access_token":
            (config_dir / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": {"refreshToken": "rot-rt-2"}})
            )
        elif case == "symlinked_file":
            target = tmp_path / "elsewhere-credentials.json"
            target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "rot-at-2"}}))
            (config_dir / ".credentials.json").symlink_to(target)
        elif case != "no_file":
            _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert _cc(db)[:2] == ("row-at-1", "row-rt-1")
        assert lookup.calls == 0

    def test_a_database_without_an_accounts_table_is_unchanged(self, tmp_path):
        """Nothing to copy into, so the loop does not try again every tick."""
        sqlite3.connect(tmp_path / "test.db").close()
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert lookup.calls == 0

    def test_a_writer_that_lands_during_the_lookup_wins(self, tmp_path):
        """The lookup is a network request. The CAS compares against the token
        read before it, so a concurrent refresh is never overwritten, and the
        sync holds no write lock while it waits."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        def concurrent_refresh():
            conn = sqlite3.connect(str(tmp_path / "test.db"), timeout=1.0)
            try:
                conn.execute(
                    "UPDATE accounts SET cc_access_token = 'other-at-3' WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()

        lookup = _Lookup(_profile(OWNER), on_call=concurrent_refresh)
        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert db.get_account(1)["cc_access_token"] == "other-at-3"
        assert lookup.calls == 1

    def test_an_unreadable_credentials_file_is_deferred(self, tmp_path):
        """A half-written file is read again on the next tick."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        (config_dir / ".credentials.json").write_text('{"claudeAiOauth": {"accessTo')

        assert _sync(config_dir, tmp_path) == "deferred"
        assert _cc(db)[:2] == ("row-at-1", "row-rt-1")


# ---------------------------------------------------------------------------
# A database from before the cc_* columns
# ---------------------------------------------------------------------------


class TestPreMigrationSchema:
    """The primary columns get the tokens, behind the same ownership rule."""

    @staticmethod
    def _legacy_db(tmp_path) -> Path:
        path = tmp_path / "test.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """CREATE TABLE accounts (
                       id INTEGER PRIMARY KEY, email TEXT,
                       organization_uuid TEXT DEFAULT '', access_token TEXT,
                       refresh_token TEXT, expires_at INTEGER,
                       validation_status TEXT, consecutive_failures INTEGER DEFAULT 0,
                       last_error TEXT, updated_at TEXT)"""
            )
            conn.execute(
                "INSERT INTO accounts (id, email, organization_uuid, access_token, "
                "refresh_token, validation_status) "
                "VALUES (1, ?, ?, 'legacy-at-1', 'legacy-rt-1', 'invalid')",
                (OWNER, ORG),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    @staticmethod
    def _row(path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(
                "SELECT access_token, refresh_token, validation_status "
                "FROM accounts WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

    def test_a_proven_rotation_goes_to_the_primary_columns(self, tmp_path):
        path = self._legacy_db(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        with _patched(_Lookup(_profile(OWNER))):
            assert _sync(config_dir, tmp_path) == "imported"

        assert self._row(path) == ("rot-at-2", "rot-rt-2", "valid")

    def test_a_foreign_rotation_is_refused(self, tmp_path):
        path = self._legacy_db(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "foreign-at-9", "foreign-rt-9")

        with _patched(_Lookup(_profile(INTRUDER))):
            assert _sync(config_dir, tmp_path) == "refused"

        assert self._row(path) == ("legacy-at-1", "legacy-rt-1", "invalid")


# ---------------------------------------------------------------------------
# The polling loop
# ---------------------------------------------------------------------------


class TestTheSyncLoop:
    @staticmethod
    def _run_loop(monkeypatch, tmp_path, outcomes):
        """Run the loop for two ticks. The file appears once, after the
        launch, and does not change again."""
        from jacked import launch

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        cred_path = config_dir / ".credentials.json"
        seen = []

        def fake_sync(_config_dir, _db_path):
            seen.append(cred_path.stat().st_mtime_ns)
            return outcomes[len(seen) - 1]

        def fake_sleep(_seconds):
            if not cred_path.exists():
                cred_path.write_text("{}")

        proc = mock.Mock()
        proc.poll.side_effect = [None, None, 0]
        monkeypatch.setattr(launch, "_sync_tokens_from_file", fake_sync)
        monkeypatch.setattr(
            launch, "_time", SimpleNamespace(sleep=fake_sleep, time=time.time)
        )
        launch._token_sync_loop(proc, config_dir, str(tmp_path / "test.db"))
        return seen

    def test_a_deferred_sync_is_tried_again_without_a_new_mtime(
        self, monkeypatch, tmp_path
    ):
        seen = self._run_loop(monkeypatch, tmp_path, ["deferred", "imported"])

        assert len(seen) == 2
        assert seen[0] == seen[1]  # the file did not change between the two tries

    @pytest.mark.parametrize("outcome", ["imported", "refused", "unchanged"])
    def test_a_settled_sync_waits_for_the_next_change(
        self, monkeypatch, tmp_path, outcome
    ):
        seen = self._run_loop(monkeypatch, tmp_path, [outcome, outcome])

        assert len(seen) == 1


# ---------------------------------------------------------------------------
# Both capture sites bind the tier (#230i)
# ---------------------------------------------------------------------------


class TestTheTierBoundCapture:
    """capture_oauth_extra at both launch sites: subscriptionTypeVerifiedAt
    goes back out only while the row publishes the tier that it verified."""

    @pytest.mark.parametrize("branch", ["same_token", "rotation"])
    def test_the_verified_at_follows_the_rows_tier(self, tmp_path, branch):
        from jacked.api.credential_helpers import build_oauth_data

        db = _make_row(tmp_path)
        with db._writer() as conn:
            conn.execute("UPDATE accounts SET subscription_type = 'max' WHERE id = 1")
        config_dir = _account_dir(tmp_path)
        access, refresh = {
            "same_token": ("row-at-1", "row-rt-1"),
            "rotation": ("rot-at-2", "rot-rt-2"),
        }[branch]
        _write_credentials(
            config_dir, access, refresh, subscriptionType="max",
            subscriptionTypeSource="profile", subscriptionTypeVerifiedAt=VERIFIED_AT,
        )
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "imported"

        assert lookup.calls == (0 if branch == "same_token" else 1)
        published = build_oauth_data(db.get_account(1))
        assert published["accessToken"] == access
        assert published["subscriptionTypeVerifiedAt"] == VERIFIED_AT
        assert published["subscriptionTypeSource"] == "profile"

        with db._writer() as conn:
            conn.execute("UPDATE accounts SET subscription_type = 'pro' WHERE id = 1")
        published = build_oauth_data(db.get_account(1))
        assert "subscriptionTypeVerifiedAt" not in published
        assert published["subscriptionTypeSource"] == "profile"


# ---------------------------------------------------------------------------
# A re-auth that lands during the profile request
# ---------------------------------------------------------------------------


class TestAReauthDuringTheLookup:
    """The profile request proves the token against the row's identity as
    the sync read it. A primary re-auth can move the row to another identity
    during the request and leave cc_access_token as it was. The CAS guards
    the identity too, and a lost CAS forgets the stale verdict."""

    @pytest.mark.parametrize(
        ("column", "new_value", "new_identity"),
        [
            ("organization_uuid", "org-new", {"org": "org-new"}),
            ("account_uuid", "uuid-new", {"uuid": "uuid-new"}),
            ("email", "new-owner@test.com", {"email": "new-owner@test.com"}),
        ],
        ids=["organization_uuid", "account_uuid", "email"],
    )
    def test_a_proof_against_the_old_identity_is_not_imported(
        self, tmp_path, column, new_value, new_identity
    ):
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        def reauth_lands():
            conn = sqlite3.connect(str(tmp_path / "test.db"), timeout=1.0)
            try:
                # The column name comes from the parametrize list above.
                conn.execute(f"UPDATE accounts SET {column} = ? WHERE id = 1", (new_value,))
                conn.commit()
            finally:
                conn.close()

        # The answer names the ORIGINAL identity: the proof is made against
        # the row as the sync read it, before the re-auth.
        old_identity = _Lookup(_profile(OWNER), on_call=reauth_lands)
        with _patched(old_identity):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert old_identity.calls == 1
        assert _cc(db) == ("row-at-1", "row-rt-1", "unknown")
        assert db.get_account(1)[column] == new_value
        assert [key for key in credential_helpers._ownership_cache if key[0] == 1] == []

        # The next sync proves the token again, against the new identity.
        new_owner = _Lookup(_profile(**{"email": OWNER, **new_identity}))
        with _patched(new_owner):
            assert _sync(config_dir, tmp_path) == "imported"

        assert new_owner.calls == 1
        assert _cc(db) == ("rot-at-2", "rot-rt-2", "valid")


# ---------------------------------------------------------------------------
# The first deferral of a row is a WARNING; its retries are INFO
# ---------------------------------------------------------------------------

DEFERRED = (
    "Token sync deferred for account {}: ownership lookup failed; jacked tries "
    "again on the next poll"
)


def test_only_the_first_deferral_of_each_row_is_a_warning(tmp_path, caplog, monkeypatch):
    """`jacked claude` logs at WARNING, so a user sees the first deferral of
    a row. The loop tries again every 30 s, and those tries stay at INFO."""
    from jacked import launch

    # A new, empty set for this test; monkeypatch puts the old set back.
    monkeypatch.setattr(launch, "_deferral_warned", set())
    _make_row(tmp_path)
    _make_row(tmp_path, account_id=2, email="second@test.com", cc_at="row-at-9",
              cc_rt="row-rt-9")
    first_dir, second_dir = _account_dir(tmp_path), _account_dir(tmp_path, 2)
    _write_credentials(first_dir, "rot-at-2", "rot-rt-2")
    _write_credentials(second_dir, "rot-at-3", "rot-rt-3")
    lookup = _Lookup(ProfileLookup(503))
    caplog.set_level(logging.INFO)

    with _patched(lookup):
        assert _sync(first_dir, tmp_path) == "deferred"
        assert _sync(first_dir, tmp_path) == "deferred"  # the cached failure
        assert _sync(second_dir, tmp_path) == "deferred"

    def levels(account_id):
        text = DEFERRED.format(account_id)
        return [r.levelno for r in caplog.records if r.getMessage() == text]

    assert levels(1) == [logging.WARNING, logging.INFO]
    assert levels(2) == [logging.WARNING]  # each row has its own first WARNING
    assert lookup.calls == 2  # the try inside the retry window sent no request
