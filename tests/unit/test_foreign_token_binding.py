"""The cross-account verdict is bound to the primary refresh token (#154i).

A profile 200 that names another account marks the row invalid with
``Token belongs to a different account`` and writes ``foreign_token_fp``: the
fingerprint of the stored primary refresh token. The verdict is that binding
alone, like the revoked-token verdict (#152i):

- no error writer releases it: a usage 200 (``clear_account_errors``), the
  watchdog, or any other ``last_error`` writer leaves the binding, so the next
  pass of the refresh loop still sends no token;
- a new stored refresh token releases it (``trg_foreign_token_fp_reset``), and
  a validation that proves the row's own account clears it;
- the usage token chooser never picks a primary token under the verdict, so
  another account's usage is never cached on this row.

No test reaches the network, the Keychain or a real credential store.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web import auth
from jacked.web.auth import RefreshMode, TokenChoice, choose_usage_token
from jacked.web.database import Database
from jacked.web.refresh_verdict import FOREIGN_TOKEN_ERROR, token_fingerprint

OWNER = "owner@test.com"
OTHER = "other@test.com"
BOUND = token_fingerprint("rt-foreign")
USAGE = {"five_hour": {"utilization": 10.0}, "seven_day": {"utilization": 20.0}}


@pytest.fixture(autouse=True)
def _isolated():
    """Fresh locks and usage pacing; no live store; the probe says 'reachable'."""
    auth.reset_locks()
    auth._account_usage_state.clear()
    with patch("jacked.web.auth._network_reachable", AsyncMock(return_value=True)), \
         patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store",
               return_value=False):
        yield
    auth._account_usage_state.clear()
    auth.reset_locks()


def _resp(status, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.headers = {}
    return resp


def _client(*, token=(200, {"access_token": "at-2", "refresh_token": "rt-2",
                            "expires_in": 3600}), get=(401, {})):
    """One httpx client mock: POST is the token endpoint, GET the profile or usage."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_resp(*token))
    client.get = AsyncMock(return_value=_resp(*get))
    return client


def _profile(email):
    return {"account": {"email": email, "uuid": f"uuid-{email}"},
            "organization": {"uuid": None, "organization_type": "claude_max"}}


def _db(tmp_path):
    db = Database(str(tmp_path / "binding.db"))
    acct = db.create_account(OWNER, "at-foreign", int(time.time()) - 60,
                             refresh_token="rt-foreign")
    db.update_account(acct["id"], validation_status="unknown")
    return db, acct["id"]


def _bound(tmp_path, *, own_cc=False):
    """A row that validation marked: the verdict is bound to rt-foreign."""
    db, account_id = _db(tmp_path)
    updates = {"validation_status": "invalid", "last_error": FOREIGN_TOKEN_ERROR,
               "foreign_token_fp": BOUND}
    if own_cc:
        updates.update(cc_access_token="cc-own", cc_refresh_token="cc-rt",
                       cc_expires_at=int(time.time()) + 3600)
    db.update_account(account_id, **updates)
    return db, account_id


def _run(db, client, coro_factory):
    with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
        return asyncio.run(coro_factory())


def _loop_pass(db, client):
    """One pass of the background refresh loop (no active account known)."""
    with patch("jacked.api.credential_helpers.read_active_account_id", return_value=None), \
         patch("jacked.api.credential_helpers.live_holder_account_ids", return_value=set()):
        return _run(db, client, lambda: auth.refresh_all_expiring_tokens(db=db))


# ---------------------------------------------------------------------------
# The mark binds the verdict; a proof of the row's own account releases it
# ---------------------------------------------------------------------------


class TestTheMarkBindsTheVerdict:
    def test_a_foreign_profile_binds_the_stored_refresh_token(self, tmp_path):
        db, account_id = _db(tmp_path)
        db.update_account(account_id, expires_at=int(time.time()) + 3600)
        verdict = _run(db, _client(get=(200, _profile(OTHER))),
                       lambda: auth.validate_account(account_id, db))

        assert verdict == {"valid": False, "error": FOREIGN_TOKEN_ERROR}
        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_error"], row["foreign_token_fp"]) == (
            "invalid", FOREIGN_TOKEN_ERROR, BOUND)

    def test_a_reauth_during_the_check_is_not_bound(self, tmp_path):
        db, account_id = _db(tmp_path)
        real_apply = auth._apply_profile

        def a_reauth_lands(account_id_, data, db_):
            db_.update_account(account_id_, access_token="at-own", refresh_token="rt-own",
                               validation_status="valid", last_error=None)
            return real_apply(account_id_, data, db_)

        with patch("jacked.web.auth._apply_profile", side_effect=a_reauth_lands):
            auth._settle_profile_verdict(account_id, _profile(OTHER), "at-foreign", db)

        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_error"], row["foreign_token_fp"]) == (
            "valid", None, None)

    def test_a_validation_of_the_rows_own_account_releases_it(self, tmp_path):
        db, account_id = _bound(tmp_path)
        verdict = auth._settle_profile_verdict(account_id, _profile(OWNER), "at-foreign", db)

        assert verdict == {"valid": True, "error": None}
        row = db.get_account(account_id)
        assert (row["validation_status"], row["foreign_token_fp"]) == ("valid", None)

    def test_an_unreadable_profile_proves_nothing_and_keeps_it(self, tmp_path, caplog):
        """The profile carries no owner value: it proves no owner, so the
        verdict stands and the row is not marked valid over it."""
        db, account_id = _bound(tmp_path)
        with caplog.at_level("INFO", logger="jacked.auth"):
            verdict = auth._settle_profile_verdict(
                account_id, {"account": {"uuid": "u"}}, "at-foreign", db)

        assert verdict == {"valid": False, "error": FOREIGN_TOKEN_ERROR}
        row = db.get_account(account_id)
        assert (row["validation_status"], row["foreign_token_fp"], row["last_validated_at"]) == (
            "invalid", BOUND, None)
        assert f"Account {account_id}: the profile names no owner; the cross-account verdict stands" in caplog.text

    def test_an_unreadable_profile_still_marks_an_unbound_row_valid(self, tmp_path):
        db, account_id = _db(tmp_path)
        verdict = auth._settle_profile_verdict(
            account_id, {"account": {"uuid": "u"}}, "at-foreign", db)

        assert verdict == {"valid": True, "error": None}
        assert db.get_account(account_id)["validation_status"] == "valid"

    def test_the_validated_mark_needs_the_checked_token(self, tmp_path, caplog):
        """A 200 for an access token the row no longer holds proves nothing
        about the row's current tokens: no mark, and a new check is asked."""
        db, account_id = _db(tmp_path)
        with caplog.at_level("INFO", logger="jacked.auth"):
            verdict = auth._settle_profile_verdict(account_id, _profile(OWNER), "at-older", db)

        assert verdict == {"valid": False, "error": auth._RECHECK_ERROR, "recheck": True}
        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_validated_at"]) == ("unknown", None)
        assert f"Account {account_id}: validation verdict not recorded; the token changed" in caplog.text

    def test_the_release_needs_the_checked_token(self, tmp_path):
        """A proof about an older access token does not release a binding on
        the row's current tokens."""
        db, account_id = _bound(tmp_path)
        auth._settle_profile_verdict(account_id, _profile(OWNER), "at-older", db)
        assert db.get_account(account_id)["foreign_token_fp"] == BOUND


# ---------------------------------------------------------------------------
# No error writer releases the binding; a new refresh token does
# ---------------------------------------------------------------------------


class TestOnlyANewRefreshTokenReleasesIt:
    def test_a_usage_200_does_not_release_it(self, tmp_path):
        """A usage success through the row's own Claude Code token resets only
        the usage failure counter: the row stays invalid with the verdict's
        text (no status flip, no auto-swap flip), and the next loop pass
        still sends no token."""
        db, account_id = _bound(tmp_path, own_cc=True)
        db.update_account(account_id, consecutive_failures=2)
        usage_client = _client(get=(200, USAGE))
        assert _run(db, usage_client, lambda: auth.fetch_usage(account_id, db)) == USAGE
        # The usage request (the first GET; a profile piggyback may follow)
        # sends the row's own Claude Code token, never the bound primary one.
        sent = [c.kwargs["headers"]["Authorization"] for c in usage_client.get.await_args_list]
        assert sent and set(sent) == {"Bearer cc-own"}

        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_error"]) == ("invalid", FOREIGN_TOKEN_ERROR)
        assert row["consecutive_failures"] == 0
        assert row["foreign_token_fp"] == BOUND
        assert row["usage_cached_at"] is not None

        loop_client = _client()
        counts = _loop_pass(db, loop_client)
        loop_client.post.assert_not_awaited()
        assert counts["refreshed"] == 0
        assert db.get_account(account_id)["refresh_token"] == "rt-foreign"

    def test_a_usage_200_still_marks_an_unbound_row_valid(self, tmp_path):
        db, account_id = _bound(tmp_path, own_cc=True)
        db.update_account(account_id, foreign_token_fp=None, consecutive_failures=2)
        assert _run(db, _client(get=(200, USAGE)), lambda: auth.fetch_usage(account_id, db)) == USAGE

        row = db.get_account(account_id)
        assert (row["validation_status"], row["last_error"], row["consecutive_failures"]) == (
            "valid", None, 0)

    def test_another_error_writer_does_not_release_it(self, tmp_path):
        db, account_id = _bound(tmp_path)
        # The stuck-validation watchdog, for example, writes its own text.
        db.update_account(account_id, validation_status="unknown",
                          last_error="validation timed out after 130s. Reset by watchdog.")
        client = _client()
        result = _run(db, client,
                      lambda: auth._refresh_token_flow(account_id, db, RefreshMode.PRIMARY))

        assert (result.success, result.error) == (False, "foreign_token")
        client.post.assert_not_awaited()

    def test_a_new_refresh_token_releases_it(self, tmp_path):
        db, account_id = _bound(tmp_path)
        db.update_account(account_id, refresh_token="rt-own")
        assert db.get_account(account_id)["foreign_token_fp"] is None

        client = _client(get=(200, _profile(OWNER)))
        result = _run(db, client,
                      lambda: auth._refresh_token_flow(account_id, db, RefreshMode.PRIMARY))

        assert result.success is True
        assert client.post.await_count == 1
        assert client.post.await_args.kwargs["json"]["refresh_token"] == "rt-own"


# ---------------------------------------------------------------------------
# Usage never reads another account's numbers into this row
# ---------------------------------------------------------------------------


class TestUsageNeverUsesAForeignPrimaryToken:
    def test_the_chooser_skips_a_bound_primary_token(self):
        row = {"access_token": "p", "refresh_token": "rt", "foreign_token_fp": token_fingerprint("rt")}
        assert choose_usage_token(row) is None
        own_cc = {**row, "cc_access_token": "c", "cc_expires_at": 9_999_999_999}
        assert choose_usage_token(own_cc) == TokenChoice("c", "cc")
        assert choose_usage_token({**row, "foreign_token_fp": None}) == TokenChoice("p", "primary")

    @pytest.mark.parametrize("explicit", [None, "at-foreign"])
    def test_fetch_usage_sends_no_bound_primary_token(self, tmp_path, explicit):
        db, account_id = _bound(tmp_path)
        client = _client(get=(200, USAGE))
        result = _run(db, client,
                      lambda: auth.fetch_usage(account_id, db, access_token=explicit))

        assert result is None
        client.get.assert_not_awaited()
        client.post.assert_not_awaited()
        assert db.get_account(account_id)["usage_cached_at"] is None
