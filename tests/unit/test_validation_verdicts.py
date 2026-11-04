"""Validation verdicts hold only for the row and token they were about.

- F-4: a row that validation marked ``Token belongs to a different account``
  holds another account's primary tokens, and ``foreign_token_fp`` binds the
  verdict to the stored refresh token. No path exchanges that refresh token:
  the exchange would spend the other account's single-use token under this
  row, and the heal sweep would then mark the row valid. The heal sweep skips
  the exchange (one line for each pass) and the validation leg keeps the row
  invalid with the same text. A new stored refresh token releases the verdict
  (``test_foreign_token_binding.py`` covers the binding itself).
- F-5: the 401/403 verdict is a compare-and-swap on the refused access token,
  so a re-auth that landed between the 401 and the write keeps its status.
  The other validation writes (a 5xx or 429 answer, a timeout, an error) are
  guarded the same way.
- F-6: a profile 200 for a row whose identity changed meanwhile, or for a row
  that is gone, does not mark the row valid.

No test reaches the network, the Keychain or a real credential store.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jacked.web import auth
from jacked.web.auth import ProfileApply, RefreshMode
from jacked.web.database import Database
from jacked.web.refresh_verdict import (
    FOREIGN_TOKEN_ERROR,
    primary_tokens_foreign,
    token_fingerprint,
)

OWNER = "owner@test.com"
OTHER = "other@test.com"
FOREIGN_SKIP = "heal skips the exchange; the stored tokens belong to a different account"
RECHECK = "The account changed during validation. Validate the account again."


@pytest.fixture(autouse=True)
def _isolated():
    """Fresh refresh locks; no live store; the probe answers 'reachable'."""
    auth.reset_locks()
    with patch("jacked.web.auth._network_reachable", AsyncMock(return_value=True)), \
         patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store",
               return_value=False):
        yield
    auth.reset_locks()


def _resp(status, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.headers = {}
    return resp


def _client(*, token=(200, {"access_token": "at-2", "refresh_token": "rt-2",
                            "expires_in": 3600}), profile=(401, {})):
    """One httpx client mock: POST is the token endpoint, GET the profile."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_resp(*token))
    client.get = AsyncMock(return_value=_resp(*profile))
    return client


def _profile(email):
    return {"account": {"email": email, "uuid": f"uuid-{email}"},
            "organization": {"uuid": None, "organization_type": "claude_max"}}


def _foreign_row(tmp_path, *, access_expired=False):
    """A row that validation marked: its primary tokens are another account's."""
    db = Database(str(tmp_path / "verdicts.db"))
    expires = int(time.time()) + (-60 if access_expired else 3600)
    acct = db.create_account(OWNER, "at-foreign", expires, refresh_token="rt-foreign")
    db.update_account(acct["id"], validation_status="invalid",
                      last_error=FOREIGN_TOKEN_ERROR,
                      foreign_token_fp=token_fingerprint("rt-foreign"))
    return db, acct["id"]


def _heal(db, client):
    with patch("jacked.web.auth.Database", return_value=db), \
         patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
        return asyncio.run(auth.heal_invalid_accounts())


def _lines(caplog, text):
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def _state(db, account_id):
    row = db.get_account(account_id)
    return row["validation_status"], row["last_error"], row["refresh_token"]


# ---------------------------------------------------------------------------
# F-4: no path exchanges a foreign row's refresh token
# ---------------------------------------------------------------------------


class TestTheForeignVerdict:
    def test_the_verdict_is_the_binding_to_the_stored_refresh_token(self):
        bound = {"refresh_token": "rt", "foreign_token_fp": token_fingerprint("rt")}
        assert primary_tokens_foreign(bound) is True
        # No error text or status releases it.
        assert primary_tokens_foreign(
            {**bound, "last_error": None, "validation_status": "valid"}) is True
        # Only another stored refresh token does.
        assert primary_tokens_foreign({**bound, "refresh_token": "rt-new"}) is False
        assert primary_tokens_foreign(
            {"last_error": FOREIGN_TOKEN_ERROR, "validation_status": "invalid"}) is False

    @pytest.mark.parametrize("access_expired, profile", [
        (False, (200, _profile(OTHER))),  # the foreign access token still works
        (True, (401, {})),                # it expired: no renewal either
    ])
    def test_three_heal_passes_send_no_token_and_keep_the_verdict(
        self, tmp_path, caplog, access_expired, profile,
    ):
        db, account_id = _foreign_row(tmp_path, access_expired=access_expired)
        client = _client(profile=profile)
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            results = []
            for _ in range(3):
                caplog.clear()
                results.append(_heal(db, client))
                assert _lines(caplog, FOREIGN_SKIP) == [f"Account {account_id}: {FOREIGN_SKIP}"]

        client.post.assert_not_awaited()
        assert [r["confirmed_invalid"] for r in results] == [1, 1, 1]
        assert [r["healed"] for r in results] == [0, 0, 0]
        assert _state(db, account_id) == ("invalid", FOREIGN_TOKEN_ERROR, "rt-foreign")
        assert "rt-foreign" not in caplog.text and OWNER not in caplog.text

    def test_a_server_error_during_validation_keeps_the_verdict(self, tmp_path):
        db, account_id = _foreign_row(tmp_path)
        client = _client(profile=(503, {}))
        _heal(db, client)
        _heal(db, client)

        client.post.assert_not_awaited()
        assert db.get_account(account_id)["last_error"] == FOREIGN_TOKEN_ERROR

    @pytest.mark.parametrize("mode", [RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER])
    def test_the_refresh_flow_refuses_without_a_token_call(self, tmp_path, caplog, mode):
        db, account_id = _foreign_row(tmp_path, access_expired=True)
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = asyncio.run(auth._refresh_token_flow(account_id, db, mode))

        assert (result.success, result.error, result.chain) == (False, "foreign_token", "primary")
        client.post.assert_not_awaited()
        assert _lines(caplog, "not sending the primary refresh token; the stored tokens") == [
            f"Account {account_id}: not sending the primary refresh token; the stored "
            f"tokens belong to a different account (caller {mode.value})"
        ]

    def test_the_background_refresh_loop_does_not_send_it(self, tmp_path):
        db, account_id = _foreign_row(tmp_path, access_expired=True)
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.read_active_account_id", return_value=None), \
             patch("jacked.api.credential_helpers.live_holder_account_ids", return_value=set()):
            counts = asyncio.run(auth.refresh_all_expiring_tokens(db=db))

        client.post.assert_not_awaited()
        assert counts["failed"] == 1 and counts["refreshed"] == 0
        assert _state(db, account_id) == ("invalid", FOREIGN_TOKEN_ERROR, "rt-foreign")

    def test_a_usage_401_does_not_send_it_and_keeps_the_verdict(self, tmp_path):
        db, account_id = _foreign_row(tmp_path)
        client = _client(profile=(401, {}))  # every GET (the usage call) is a 401
        auth._account_usage_state.clear()
        try:
            with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
                assert asyncio.run(auth.fetch_usage(account_id, db)) is None
        finally:
            auth._account_usage_state.clear()

        client.post.assert_not_awaited()
        assert _state(db, account_id) == ("invalid", FOREIGN_TOKEN_ERROR, "rt-foreign")

    def test_a_reauthorization_releases_the_verdict(self, tmp_path):
        db, account_id = _foreign_row(tmp_path, access_expired=True)
        # The OAuth store writes the new pair, marks the row valid and clears
        # the error text.
        db.update_account(account_id, access_token="at-own", refresh_token="rt-own",
                          validation_status="valid", last_error=None)
        client = _client(profile=(200, _profile(OWNER)))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(auth._refresh_token_flow(account_id, db, RefreshMode.PRIMARY))

        assert result.success is True
        assert client.post.await_count == 1
        assert client.post.await_args.kwargs["json"]["refresh_token"] == "rt-own"


# ---------------------------------------------------------------------------
# F-5: the 401/403 verdict is bound to the refused access token
# ---------------------------------------------------------------------------


def _row(tmp_path, *, refresh_token="rt-1", status="unknown"):
    db = Database(str(tmp_path / "f5.db"))
    acct = db.create_account(OWNER, "at-1", int(time.time()) + 3600,
                             refresh_token=refresh_token)
    db.update_account(acct["id"], validation_status=status)
    return db, acct["id"]


class TestAuthRejectionIsGuarded:
    def test_the_row_that_still_holds_the_token_is_marked_invalid(self, tmp_path):
        db, account_id = _row(tmp_path)
        verdict = auth._record_auth_rejection(account_id, db, 401, "at-1")

        assert verdict == {"valid": False, "error": "Token invalid (HTTP 401)"}
        row = db.get_account(account_id)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == "Token invalid (HTTP 401), refresh failed"

    def test_a_reauth_that_landed_meanwhile_keeps_its_status(self, tmp_path):
        db, account_id = _row(tmp_path)
        db.update_account(account_id, access_token="at-reauth",
                          validation_status="valid", last_error=None)

        verdict = auth._record_auth_rejection(account_id, db, 401, "at-1")

        assert verdict == {"valid": False, "error": "Token invalid (HTTP 401)"}
        row = db.get_account(account_id)
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None

    def test_validate_guards_the_write_on_the_token_it_sent(self, tmp_path):
        """The 401 verdict is recorded for the token that the profile request
        sent: a re-auth that lands after the 401 keeps its status."""
        db, account_id = _row(tmp_path)

        def no_renewal_then_a_reauth(*args, **kwargs):
            db.update_account(account_id, access_token="at-reauth",
                              validation_status="valid", last_error=None)
            return None

        client = _client(profile=(401, {}))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._profile_after_refresh",
                   AsyncMock(side_effect=no_renewal_then_a_reauth)):
            verdict = asyncio.run(auth.validate_account(account_id, db))

        assert verdict == {"valid": False, "error": "Token invalid (HTTP 401)"}
        client.post.assert_not_awaited()
        row = db.get_account(account_id)
        assert row["access_token"] == "at-reauth"
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None


class TestTheOtherValidationWritesAreGuarded:
    """A 5xx or 429 answer, a timeout and an error are recorded only while the
    row still holds the access token that the request sent."""

    @staticmethod
    def _validate(db, account_id, answer):
        client = _client()
        client.get = AsyncMock(side_effect=answer)
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            return asyncio.run(auth.validate_account(account_id, db))

    @pytest.mark.parametrize("outcome", ["http_503", "http_429", "timeout", "error"])
    def test_a_reauth_during_the_request_keeps_its_status(self, tmp_path, outcome):
        db, account_id = _row(tmp_path)

        def a_reauth_lands_then(*args, **kwargs):
            db.update_account(account_id, access_token="at-reauth",
                              validation_status="valid", last_error=None)
            if outcome == "timeout":
                raise httpx.ReadTimeout("slow")
            if outcome == "error":
                raise RuntimeError("boom")
            return _resp(503 if outcome == "http_503" else 429)

        self._validate(db, account_id, a_reauth_lands_then)

        row = db.get_account(account_id)
        assert (row["access_token"], row["validation_status"], row["last_error"]) == (
            "at-reauth", "valid", None)

    @pytest.mark.parametrize("outcome, expected", [
        ("http_503", ("unknown", "Validation HTTP 503")),
        ("http_429", ("unknown", "Rate limited during validation")),
        ("timeout", ("unknown", None)),
        ("error", ("unknown", "boom")),
    ])
    def test_an_unchanged_row_is_recorded_as_before(self, tmp_path, outcome, expected):
        db, account_id = _row(tmp_path)

        def answer(*args, **kwargs):
            if outcome == "timeout":
                raise httpx.ReadTimeout("slow")
            if outcome == "error":
                raise RuntimeError("boom")
            return _resp(503 if outcome == "http_503" else 429)

        self._validate(db, account_id, answer)

        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_error"]) == expected


# ---------------------------------------------------------------------------
# F-6: a 200 for an old identity, or for a gone row, blesses nothing
# ---------------------------------------------------------------------------


class TestAProfileForAnotherRowStateBlessesNothing:
    def test_an_identity_change_asks_for_a_new_check(self, tmp_path):
        db, account_id = _row(tmp_path, status="checking")
        with patch.object(auth, "_apply_profile", return_value=ProfileApply.IDENTITY_CHANGED):
            verdict = auth._settle_profile_verdict(account_id, _profile(OWNER), "at-1", db)

        assert verdict == {"valid": False, "error": RECHECK, "recheck": True}
        row = db.get_account(account_id)
        assert row["validation_status"] == "checking"  # left alone
        assert row["last_validated_at"] is None

    def test_a_status_that_a_reauth_wrote_stays(self, tmp_path):
        db, account_id = _row(tmp_path, status="valid")
        with patch.object(auth, "_apply_profile", return_value=ProfileApply.IDENTITY_CHANGED):
            auth._settle_profile_verdict(account_id, _profile(OWNER), "at-1", db)
        assert db.get_account(account_id)["validation_status"] == "valid"

    def test_a_gone_row_is_not_found(self, tmp_path):
        db, account_id = _row(tmp_path, status="checking")
        with patch.object(db, "mark_account_validated") as marked:
            verdict = auth._settle_profile_verdict(9999, _profile(OWNER), "at-1", db)

        assert verdict == {"valid": False, "error": "Account not found"}
        marked.assert_not_called()

    def test_the_heal_sweep_counts_a_new_check_as_deferred(self, tmp_path):
        db, account_id = _row(tmp_path, refresh_token=None, status="invalid")
        recheck = {"valid": False, "error": RECHECK, "recheck": True}
        with patch("jacked.web.auth.validate_account", AsyncMock(return_value=recheck)):
            result = _heal(db, _client())

        assert result == {"checked": 1, "healed": 0, "confirmed_invalid": 0, "deferred": 1}

    def test_an_applied_profile_still_marks_the_row_valid(self, tmp_path):
        db, account_id = _row(tmp_path, status="checking")
        verdict = auth._settle_profile_verdict(account_id, _profile(OWNER), "at-1", db)

        assert verdict == {"valid": True, "error": None}
        assert db.get_account(account_id)["validation_status"] == "valid"
