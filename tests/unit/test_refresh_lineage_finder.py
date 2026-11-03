"""The refresh downgrade guard asks the database who stores the live refresh
token before it costs a profile request (#149i).

A refresh token is issued to one account. When a row stores the refresh
token the live store holds, that row names the owner of the live tokens:
the target row means the write is a downgrade of its own lineage (refused),
another row means the live tokens are another account's (the write goes
ahead, as before). The ownership rule, with its cached profile verdict, runs
only when no row stores the token. A bound primary token
(``foreign_token_fp``) belongs to another account and names no owner.
"""

from __future__ import annotations

import time
from dataclasses import replace
from unittest import mock

import pytest

from jacked.api import usage_monitor
from jacked.credentials import refresh_guard
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.refresh_guard import (
    REFRESH_DOWNGRADE_REASON,
    REFRESH_DOWNGRADE_UNCHECKED_REASON,
    downgrade_check_for,
    lineage_finder,
    refuses_refresh_downgrade,
)
from jacked.credentials.runtime import _activation_preflight, _row_payload
from jacked.web.database import Database
from jacked.web.refresh_verdict import token_fingerprint
from tests.unit.test_credential_transactions import _request

FUTURE_S = int(time.time()) + 6 * 3600

ROW = {
    "id": 7,
    "email": "owner@test.com",
    "cc_access_token": "cc-at",
    "cc_refresh_token": None,
    "cc_expires_at": FUTURE_S,
    "cc_refresh_failure_type": "invalid_grant",
}
TARGET = {"accessToken": "cc-at", "refreshToken": None}
LIVE = {"claudeAiOauth": {"accessToken": "other-at", "refreshToken": "live-rt"}}


def _no_profile_request():
    return mock.patch(
        "jacked.api.credential_helpers.live_payload_ownership",
        side_effect=AssertionError("the ownership rule must not run"),
    )


class TestTheFinderDecidesFirst:
    def test_another_rows_token_lets_the_write_through_without_a_request(self):
        with _no_profile_request():
            assert refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=lambda rt: 9) is None

    def test_the_target_rows_token_refuses_without_a_request(self):
        with _no_profile_request():
            assert refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=lambda rt: 7) == (
                REFRESH_DOWNGRADE_REASON)

    def test_the_finder_is_asked_for_the_live_refresh_token(self):
        asked = []

        def finder(rt):
            asked.append(rt)
            return 9

        refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=finder)
        assert asked == ["live-rt"]

    @pytest.mark.parametrize("finder", [None, lambda rt: None, lambda rt: "7", lambda rt: True],
                             ids=["absent", "no-row", "not-an-int", "a-bool"])
    def test_without_a_row_the_ownership_rule_decides(self, finder):
        with mock.patch("jacked.api.credential_helpers.live_payload_ownership", return_value=True):
            assert refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=finder) == (
                REFRESH_DOWNGRADE_REASON)
        with mock.patch("jacked.api.credential_helpers.live_payload_ownership", return_value=None):
            assert refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=finder) == (
                REFRESH_DOWNGRADE_UNCHECKED_REASON)

    def test_a_failing_finder_costs_the_request_never_the_write(self):
        def boom(rt):
            raise RuntimeError("database is locked")

        with mock.patch("jacked.api.credential_helpers.live_payload_ownership", return_value=False):
            assert refuses_refresh_downgrade(TARGET, LIVE, ROW, lineage_known=boom) is None

    def test_the_two_local_checks_still_run_first(self):
        asked = []

        def finder(rt):
            asked.append(rt)
            return 7

        target_with_refresh = {"accessToken": "cc-at", "refreshToken": "rt"}
        assert refuses_refresh_downgrade(target_with_refresh, LIVE, ROW, lineage_known=finder) is None
        empty_live = {"claudeAiOauth": {"accessToken": "other-at"}}
        assert refuses_refresh_downgrade(TARGET, empty_live, ROW, lineage_known=finder) is None
        assert asked == []


class TestTheDatabaseFinder:
    def _db(self, tmp_path):
        db = Database(str(tmp_path / "finder.db"))
        a = db.create_account("a@t.com", "at-a", FUTURE_S, refresh_token="rt-a")
        b = db.create_account("b@t.com", "at-b", FUTURE_S, refresh_token="rt-b")
        db.update_account(b["id"], cc_access_token="cc-b", cc_refresh_token="cc-rt-b",
                          cc_expires_at=FUTURE_S)
        return db, a["id"], b["id"]

    def test_a_primary_and_a_claude_code_token_name_their_rows(self, tmp_path):
        db, a, b = self._db(tmp_path)
        assert db.account_id_for_refresh_token("rt-a") == a
        assert db.account_id_for_refresh_token("rt-b") == b
        assert db.account_id_for_refresh_token("cc-rt-b") == b

    @pytest.mark.parametrize("token", [None, "", 5, "rt-x"])
    def test_an_unknown_or_malformed_token_names_no_row(self, tmp_path, token):
        db, _a, _b = self._db(tmp_path)
        assert db.account_id_for_refresh_token(token) is None

    def test_a_bound_primary_token_names_no_row(self, tmp_path):
        db, a, _b = self._db(tmp_path)
        db.update_account(a, foreign_token_fp=token_fingerprint("rt-a"))
        assert db.account_id_for_refresh_token("rt-a") is None

    def test_a_deleted_row_names_nothing(self, tmp_path):
        db, a, _b = self._db(tmp_path)
        assert db.delete_account(a) is True
        assert db.account_id_for_refresh_token("rt-a") is None

    def test_the_finder_rides_on_the_database(self, tmp_path):
        db, a, _b = self._db(tmp_path)
        assert lineage_finder(db)("rt-a") == a
        assert lineage_finder(object()) is None


class _Db:
    """A database double that stores ``live-rt`` on account 9."""

    def __init__(self, row):
        self.row = row

    def get_account(self, account_id):
        return dict(self.row)

    def account_id_for_refresh_token(self, refresh_token):
        return 9 if refresh_token == "live-rt" else None


def _held():
    return CredentialPayload.from_mapping(LIVE)


class TestEveryWriterPassesTheFinder:
    def test_the_engine_preflight(self):
        db = _Db(ROW)
        request = replace(_request(_row_payload(7, ROW)), account_id=7)
        with mock.patch(
            "jacked.credentials.runtime.reconcile_outgoing_before_activation", return_value=True
        ):
            prepared = _activation_preflight(db, request)()
        with _no_profile_request():
            assert prepared.refresh_downgrade_check(_held()) is None

    def test_the_auto_swap_request(self):
        request = usage_monitor._auto_swap_request(ROW, _Db(ROW))
        with _no_profile_request():
            assert request.refresh_downgrade_check(_held()) is None

    def test_the_auto_swap_request_without_a_database_keeps_the_rule(self):
        request = usage_monitor._auto_swap_request(ROW)
        with mock.patch("jacked.api.credential_helpers.live_payload_ownership", return_value=True):
            assert request.refresh_downgrade_check(_held()) == REFRESH_DOWNGRADE_REASON

    def test_the_launchs_scoped_check(self, caplog):
        from jacked.launch import _keeps_scoped_lineage

        with _no_profile_request():
            assert _keeps_scoped_lineage(ROW, LIVE, _Db(ROW)) is False
        own = {**LIVE, "claudeAiOauth": {"accessToken": "cc-at", "refreshToken": "live-rt"}}
        # The row's own access token: owned with no request, finder or not.
        assert _keeps_scoped_lineage(ROW, own, None) is True

    def test_downgrade_check_for_carries_the_finder(self):
        check = downgrade_check_for(ROW, lineage_known=lambda rt: 9)
        with _no_profile_request():
            assert check(_held()) is None
        assert refresh_guard._known_owner(lambda rt: 9, "live-rt") == 9
