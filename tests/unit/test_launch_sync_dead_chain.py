"""One dead-chain rule for every importer of live Claude Code tokens (#149i, #244i).

After an ``invalid_grant`` the row's Claude Code chain is dead. A live refresh
token that the shared ownership rule proves is the row's, and that is NOT the
token that failed, is a new lineage that Claude Code rotated itself: the row
adopts it (refresh token, pass-through fields, access token, expiry) and clears
the cc breaker in the same compare-and-swap write. Any other live refresh token
(none, empty, or the failed one) gives the access token and its expiry only.

``credential_helpers.dead_chain_adoption`` holds the rule. Reconcile
(``_reconcile_decision``) and the launch token sync (``_sync_tokens_from_file``)
both call it, and the parity tests below run the same cases through both, so
the two paths cannot disagree again (#244i).
"""
from __future__ import annotations

import logging
import sqlite3
from unittest import mock

import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import (
    CC_IMPORT_GUARD_COLS,
    _reconcile_decision,
    clear_ownership_cache,
    dead_chain_adoption,
    reconcile_credentials_from_live_store,
)
from jacked.credentials.oauth_extra import captured_tier
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
TIER_FIELDS = {
    "subscriptionType": "max",
    "subscriptionTypeSource": "profile",
    "subscriptionTypeVerifiedAt": VERIFIED_AT,
}


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    clear_ownership_cache()
    yield
    clear_ownership_cache()


def _dead(db, refresh, *, failed_at=123):
    """Mark row 1's chain dead with ``refresh`` as the token that failed."""
    db.update_account(
        1,
        cc_refresh_token=refresh,
        cc_refresh_failure_type="invalid_grant",
        cc_refresh_last_failed_at=failed_at,
    )


def _lineage(db) -> tuple:
    row = db.get_account(1)
    return (
        row["cc_access_token"],
        row["cc_refresh_token"],
        row["cc_refresh_failure_type"],
        row["cc_refresh_last_failed_at"],
    )


def _live_oauth(access, refresh, **fields) -> dict:
    oauth = {"accessToken": access, "expiresAt": FUTURE_MS, **fields}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    return oauth


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_a_new_live_lineage_is_adopted_with_its_fields_and_a_cleared_breaker(self):
        row = {"cc_refresh_failure_type": "invalid_grant", "cc_refresh_token": None}
        oauth = _live_oauth("at-2", "rt-2", **TIER_FIELDS)

        updates = dead_chain_adoption(row, oauth)

        assert updates["cc_refresh_token"] == "rt-2"
        assert captured_tier(updates["cc_oauth_extra"], "rt-2") == "max"
        assert updates["cc_refresh_failure_type"] is None
        assert updates["cc_refresh_last_failed_at"] is None
        assert set(updates) == {
            "cc_refresh_token",
            "cc_oauth_extra",
            "cc_refresh_failure_type",
            "cc_refresh_last_failed_at",
        }

    @pytest.mark.parametrize("refresh", [None, "", 7, "failed-rt", "primary-rt"])
    def test_no_token_the_failed_token_or_the_primary_token_is_never_adopted(self, refresh):
        """The primary lineage never becomes the Claude Code lineage: two chains
        never share one single-use refresh token."""
        row = {
            "cc_refresh_failure_type": "invalid_grant",
            "cc_refresh_token": "failed-rt",
            "refresh_token": "primary-rt",
        }
        assert dead_chain_adoption(row, _live_oauth("at-2", refresh)) is None

    @pytest.mark.parametrize("failure", [None, "auth_rejected", "network"])
    def test_a_chain_that_is_not_dead_has_nothing_to_adopt(self, failure):
        row = {"cc_refresh_failure_type": failure, "cc_refresh_token": None}
        assert dead_chain_adoption(row, _live_oauth("at-2", "rt-2")) is None


# ---------------------------------------------------------------------------
# Reconcile (item 4)
# ---------------------------------------------------------------------------


def _reconcile_row(**changes) -> dict:
    row = {
        "id": 1,
        "email": OWNER,
        "organization_uuid": "org-own",
        "account_uuid": "uuid-own",
        "access_token": "primary-at",
        "cc_access_token": "row-at-1",
        "cc_refresh_token": None,
        "cc_expires_at": 1_000,
        "cc_refresh_failure_type": "invalid_grant",
        "cc_oauth_extra": None,
    }
    row.update(changes)
    return row


def _stamped(oauth) -> dict:
    return {"_jackedAccountId": 1, "claudeAiOauth": oauth}


class TestReconcileDecision:
    def test_a_proven_new_lineage_is_adopted(self):
        live = _stamped(_live_oauth("rot-at-2", "rot-rt-2", **TIER_FIELDS))

        with _patched(_Lookup(_profile(OWNER))):
            updates, guard_cols = _reconcile_decision(1, _reconcile_row(), live, False)

        assert updates["cc_access_token"] == "rot-at-2"
        assert updates["cc_expires_at"] == FUTURE_MS // 1000
        assert updates["cc_refresh_token"] == "rot-rt-2"
        assert captured_tier(updates["cc_oauth_extra"], "rot-rt-2") == "max"
        assert updates["cc_refresh_failure_type"] is None
        assert updates["cc_refresh_last_failed_at"] is None
        assert guard_cols == CC_IMPORT_GUARD_COLS

    def test_the_failed_token_gives_the_access_token_and_expiry_only(self):
        row = _reconcile_row(cc_refresh_token="failed-rt")
        live = _stamped(_live_oauth("rot-at-2", "failed-rt", **TIER_FIELDS))

        updates, _cols = _reconcile_decision(1, row, live, False)

        assert updates == {"cc_access_token": "rot-at-2", "cc_expires_at": FUTURE_MS // 1000}

    def test_an_unproven_payload_adopts_nothing(self):
        live = _stamped(_live_oauth("rot-at-2", "rot-rt-2"))

        with _patched(_Lookup(ProfileLookup(None))):
            assert _reconcile_decision(1, _reconcile_row(), live, False) is None

    def test_the_adoption_lands_in_one_write_and_is_logged(self, tmp_path, caplog):
        db = _make_row(tmp_path)
        _dead(db, None)
        live = _stamped(_live_oauth("rot-at-2", "rot-rt-2"))

        with (
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials", return_value=live
            ),
            _patched(_Lookup(_profile(OWNER))),
            caplog.at_level(logging.INFO),
        ):
            assert reconcile_credentials_from_live_store(1, db, fresh=True) is True

        assert _lineage(db) == ("rot-at-2", "rot-rt-2", None, None)
        assert caplog.text.count(ADOPTED) == 1


# ---------------------------------------------------------------------------
# The launch token sync (item 4b)
# ---------------------------------------------------------------------------


class TestLaunchSync:
    def test_a_proven_rotation_over_a_dead_chain_is_adopted(self, tmp_path, caplog):
        db = _make_row(tmp_path)
        _dead(db, None)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2", **TIER_FIELDS)
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == ("rot-at-2", "rot-rt-2", None, None)
        assert captured_tier(db.get_account(1)["cc_oauth_extra"], "rot-rt-2") == "max"
        assert lookup.calls == 1
        assert caplog.text.count(ADOPTED) == 1

    def test_the_failed_token_gives_the_access_token_and_expiry_only(self, tmp_path, caplog):
        db = _make_row(tmp_path)
        _dead(db, "failed-rt")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "failed-rt", **TIER_FIELDS)

        with caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert _lineage(db) == ("rot-at-2", "failed-rt", "invalid_grant", 123)
        assert row["cc_expires_at"] == FUTURE_MS // 1000
        assert row["cc_oauth_extra"] is None
        assert ADOPTED not in caplog.text

    def test_a_file_without_a_refresh_token_keeps_the_rows_lineage_columns(self, tmp_path):
        db = _make_row(tmp_path)
        _dead(db, "failed-rt")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", None)

        with _patched(_Lookup(_profile(OWNER))):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == ("rot-at-2", "failed-rt", "invalid_grant", 123)

    def test_an_unproven_rotation_adopts_nothing(self, tmp_path):
        db = _make_row(tmp_path)
        _dead(db, None)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        with _patched(_Lookup(ProfileLookup(None))):
            assert _sync(config_dir, tmp_path) == "deferred"

        assert _lineage(db) == ("row-at-1", None, "invalid_grant", 123)

    def test_a_new_lineage_beside_the_rows_own_access_token_is_adopted(self, tmp_path, caplog):
        """The row holds the file's access token (an earlier import took it and
        skipped the refresh token): ownership is proven with no request."""
        db = _make_row(tmp_path)
        _dead(db, None)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "row-at-1", "rot-rt-2")
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == ("row-at-1", "rot-rt-2", None, None)
        assert lookup.calls == 0
        assert caplog.text.count(ADOPTED) == 1

    def test_the_failed_token_beside_the_rows_access_token_takes_no_fields(self, tmp_path):
        db = _make_row(tmp_path)
        _dead(db, "row-rt-1")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "row-at-1", "row-rt-1", **TIER_FIELDS)

        assert _sync(config_dir, tmp_path) == "imported"

        assert _lineage(db) == ("row-at-1", "row-rt-1", "invalid_grant", 123)
        assert db.get_account(1)["cc_oauth_extra"] is None

    def test_a_chain_that_dies_during_the_lookup_is_decided_again(self, tmp_path, caplog):
        """The write is a compare-and-swap on the columns the rule reads: a chain
        that died meanwhile (same access token, the lineage cleared) is never
        given a refresh token beside a standing invalid_grant (#244i)."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")

        def chain_dies():
            conn = sqlite3.connect(str(tmp_path / "test.db"), timeout=1.0)
            try:
                conn.execute(
                    "UPDATE accounts SET cc_refresh_token = NULL, "
                    "cc_refresh_failure_type = 'invalid_grant', "
                    "cc_refresh_last_failed_at = 123 WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()

        with _patched(_Lookup(_profile(OWNER), on_call=chain_dies)):
            assert _sync(config_dir, tmp_path) == "unchanged"
        assert _lineage(db) == ("row-at-1", None, "invalid_grant", 123)

        with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.INFO):
            assert _sync(config_dir, tmp_path) == "imported"
        assert _lineage(db) == ("rot-at-2", "rot-rt-2", None, None)
        assert caplog.text.count(ADOPTED) == 1


# ---------------------------------------------------------------------------
# Parity: both importers give the same row for the same live tokens
# ---------------------------------------------------------------------------


def _through_reconcile(db, tmp_path, oauth):
    with mock.patch(
        "jacked.api.credential_helpers.read_platform_credentials",
        return_value=_stamped(oauth),
    ):
        reconcile_credentials_from_live_store(1, db, fresh=True)


def _through_launch_sync(db, tmp_path, oauth):
    config_dir = _account_dir(tmp_path)
    _write_credentials(
        config_dir,
        oauth["accessToken"],
        oauth.get("refreshToken"),
        **{k: v for k, v in oauth.items() if k not in {"accessToken", "refreshToken", "expiresAt"}},
    )
    _sync(config_dir, tmp_path)


@pytest.mark.parametrize(
    "failed, live_refresh, expected",
    [
        (None, "rot-rt-2", ("rot-at-2", "rot-rt-2", None, None)),
        ("failed-rt", "rot-rt-2", ("rot-at-2", "rot-rt-2", None, None)),
        ("failed-rt", "failed-rt", ("rot-at-2", "failed-rt", "invalid_grant", 123)),
        ("failed-rt", None, ("rot-at-2", "failed-rt", "invalid_grant", 123)),
        # _make_row's primary refresh token: never adopted by either path.
        ("failed-rt", "primary-rt", ("rot-at-2", "failed-rt", "invalid_grant", 123)),
    ],
)
@pytest.mark.parametrize("importer", [_through_reconcile, _through_launch_sync])
def test_both_importers_apply_the_same_rule(tmp_path, importer, failed, live_refresh, expected):
    db = _make_row(tmp_path)
    _dead(db, failed)
    oauth = _live_oauth("rot-at-2", live_refresh, **TIER_FIELDS)

    with _patched(_Lookup(_profile(OWNER))):
        importer(db, tmp_path, oauth)

    assert _lineage(db) == expected
    adopted = expected[2] is None
    extra = db.get_account(1)["cc_oauth_extra"]
    assert (captured_tier(extra, live_refresh) == "max") if adopted else extra is None
