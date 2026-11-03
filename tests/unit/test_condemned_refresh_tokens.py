"""A condemned refresh token stays condemned (#152i, DCR wave 1 F-2 and F-3).

An ``invalid_grant`` condemns one refresh token. The verdict is the
fingerprint binding alone, never the breaker columns beside it:

- The primary chain: while the row holds a refresh token whose fingerprint is
  ``refresh_failed_token_fp``, no path sends it, whatever
  ``refresh_failure_type`` says. A writer that clears the breaker (the heal
  sweep did, from a stale snapshot) cannot release the token. The heal
  sweep's breaker reset is a compare-and-swap on the columns it read, so a
  revocation recorded while the sweep waited for the network probe stays.
- The Claude Code chain: ``cc_refresh_failed_token_fp`` keeps the
  fingerprints of every condemned token, newest first. A second failure does
  not forget the first token, the live recovery evaluates the shared
  dead-chain rule against the stored fingerprints, and the cc exchange never
  sends a condemned token that an importer brought back.

No test reaches the network, the Keychain or a real credential store.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api.credential_helpers import dead_chain_adoption
from jacked.web import auth
from jacked.web.auth import RefreshMode
from jacked.web.database import Database
from jacked.web.refresh_verdict import (
    SIGN_IN_REQUIRED_ERROR,
    condemned_fingerprints,
    primary_refresh_revoked,
    standing_verdict_or,
    token_fingerprint,
)

EMAIL = "condemned@test.com"
SKIP_LINE = "heal skips the exchange; the refresh token was revoked (sign-in required)"
DEFER_LINE = "heal deferred; the refresh state changed while the heal checks ran"


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


def _client(*, token=(400, {"error": "invalid_grant"}), profile=(401, {})):
    """One httpx client mock: POST is the token endpoint, GET the profile."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_resp(*token))
    client.get = AsyncMock(return_value=_resp(*profile))
    return client


def _heal(db, client):
    with patch("jacked.web.auth.Database", return_value=db), \
         patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
        return asyncio.run(auth.heal_invalid_accounts())


def _primary_verdict(db, account_id):
    row = db.get_account(account_id)
    return (row["refresh_failure_type"], row["refresh_last_failed_at"],
            row["refresh_failed_token_fp"])


def _invalid_account(tmp_path, **updates):
    """An invalid account with an expired access token and refresh token rt-1."""
    db = Database(str(tmp_path / "heal.db"))
    acct = db.create_account(EMAIL, "at-1", int(time.time()) - 60, refresh_token="rt-1")
    db.update_account(acct["id"], validation_status="invalid", **updates)
    return db, acct["id"]


def _lines(caplog, text):
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


# ---------------------------------------------------------------------------
# F-3 (1): the primary verdict is the fingerprint binding alone
# ---------------------------------------------------------------------------


class TestThePrimaryVerdictIsTheFingerprint:
    @pytest.mark.parametrize("failure_type", [None, "http_503", "invalid_grant"])
    def test_the_verdict_ignores_the_breaker(self, failure_type):
        row = {"refresh_token": "rt", "refresh_failure_type": failure_type,
               "refresh_failed_token_fp": token_fingerprint("rt")}
        assert primary_refresh_revoked(row) is True
        assert standing_verdict_or(row, "Token invalid (HTTP 401)") == SIGN_IN_REQUIRED_ERROR

    @pytest.mark.parametrize("mode", [RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER])
    def test_a_cleared_breaker_does_not_release_the_token(self, tmp_path, caplog, mode):
        db, account_id = _invalid_account(
            tmp_path, refresh_failed_token_fp=token_fingerprint("rt-1"))
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = asyncio.run(auth._refresh_token_flow(account_id, db, mode))

        assert (result.success, result.error, result.chain) == (False, "invalid_grant", "primary")
        client.post.assert_not_awaited()
        assert _primary_verdict(db, account_id) == (None, None, token_fingerprint("rt-1"))
        assert len(_lines(caplog, "not sending the primary refresh token")) == 1

    def test_the_heal_sweep_skips_a_bound_token_without_a_breaker(self, tmp_path, caplog):
        db, account_id = _invalid_account(
            tmp_path, refresh_failed_token_fp=token_fingerprint("rt-1"))
        client = _client(profile=(401, {}))
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        client.post.assert_not_awaited()
        assert result["confirmed_invalid"] == 1
        assert len(_lines(caplog, SKIP_LINE)) == 1
        assert _primary_verdict(db, account_id)[2] == token_fingerprint("rt-1")
        assert db.get_account(account_id)["last_error"] == SIGN_IN_REQUIRED_ERROR


# ---------------------------------------------------------------------------
# F-3 (2): the heal sweep's breaker reset is guarded by its snapshot
# ---------------------------------------------------------------------------


class TestTheHealResetIsGuarded:
    def test_a_revocation_recorded_during_the_probe_stays(self, tmp_path, caplog):
        db, account_id = _invalid_account(
            tmp_path, refresh_failure_type="http_503",
            refresh_last_failed_at=int(time.time()) - 3600)
        revoked_at = int(time.time())

        def a_refresh_fails_during_the_probe():
            db.update_account(account_id, refresh_failure_type="invalid_grant",
                              refresh_last_failed_at=revoked_at,
                              refresh_failed_token_fp=token_fingerprint("rt-1"))
            return True

        client = _client()
        probe = AsyncMock(side_effect=a_refresh_fails_during_the_probe)
        with patch("jacked.web.auth._network_reachable", probe), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        probe.assert_awaited_once()
        client.post.assert_not_awaited()
        client.get.assert_not_awaited()
        assert result["deferred"] == 1 and result["healed"] == 0
        assert _primary_verdict(db, account_id) == (
            "invalid_grant", revoked_at, token_fingerprint("rt-1"))
        assert _lines(caplog, DEFER_LINE) == [f"Account {account_id}: {DEFER_LINE}"]
        assert not _lines(caplog, "clearing circuit breaker")

    def test_the_next_pass_reads_the_revocation(self, tmp_path, caplog):
        db, account_id = _invalid_account(
            tmp_path, refresh_failure_type="invalid_grant",
            refresh_last_failed_at=int(time.time()),
            refresh_failed_token_fp=token_fingerprint("rt-1"))
        client = _client(profile=(401, {}))
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        client.post.assert_not_awaited()
        assert result["confirmed_invalid"] == 1
        assert len(_lines(caplog, SKIP_LINE)) == 1

    def test_a_new_refresh_token_stored_during_the_probe_is_decided_next_pass(self, tmp_path):
        db, account_id = _invalid_account(tmp_path)

        def an_import_stores_a_new_token():
            db.update_account(account_id, refresh_token="rt-2")
            return True

        client = _client()
        with patch("jacked.web.auth._network_reachable",
                   AsyncMock(side_effect=an_import_stores_a_new_token)):
            result = _heal(db, client)

        client.post.assert_not_awaited()
        assert result["deferred"] == 1
        assert db.get_account(account_id)["refresh_token"] == "rt-2"

    def test_an_unchanged_snapshot_clears_the_breaker_and_exchanges(self, tmp_path, caplog):
        db, account_id = _invalid_account(
            tmp_path, refresh_failure_type="http_503",
            refresh_last_failed_at=int(time.time()) - 3600)
        client = _client(
            token=(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600}),
            profile=(200, {"account": {"email": EMAIL, "uuid": "acct-uuid-1"},
                           "organization": {"uuid": None,
                                            "organization_type": "claude_max"}}))
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        assert result["healed"] == 1
        assert client.post.await_count == 1
        assert client.post.await_args.kwargs["json"]["refresh_token"] == "rt-1"
        assert len(_lines(caplog, "clearing circuit breaker for heal attempt")) == 1
        assert not _lines(caplog, DEFER_LINE)
        assert _primary_verdict(db, account_id) == (None, None, None)
        assert db.get_account(account_id)["refresh_token"] == "rt-2"


# ---------------------------------------------------------------------------
# F-2: successive failures of the Claude Code chain
# ---------------------------------------------------------------------------


def _cc_account(tmp_path):
    db = Database(str(tmp_path / "cc.db"))
    acct = db.create_account(EMAIL, "pat", int(time.time()) + 3600, refresh_token="prt")
    db.update_account(acct["id"], cc_access_token="cc_at_1", cc_refresh_token="cc_rt_1",
                      cc_expires_at=int(time.time()) + 600)
    return db, acct["id"]


def _live(refresh):
    return {"accessToken": "live_at", "refreshToken": refresh,
            "expiresAt": (int(time.time()) + 3600) * 1000}


def _died(db, account_id, failed, live):
    with patch("jacked.web.auth._live_recovery_payload", return_value=live):
        return asyncio.run(auth._cc_chain_died(
            account_id, db, db.get_account(account_id), failed))


def _second_failure(db, account_id, live_refresh):
    """cc_rt_1 dies, the chain adopts cc_rt_2, then cc_rt_2 dies too."""
    _died(db, account_id, "cc_rt_1", None)
    # The shared rule adopted a new lineage: the breaker clears and the
    # fingerprint of the first dead token stays.
    db.update_account(account_id, cc_access_token="cc_at_2", cc_refresh_token="cc_rt_2",
                      cc_refresh_failure_type=None, cc_refresh_last_failed_at=None)
    return _died(db, account_id, "cc_rt_2", _live(live_refresh))


BOTH = (token_fingerprint("cc_rt_2"), token_fingerprint("cc_rt_1"))


class TestSuccessiveCcFailures:
    def test_the_older_condemned_token_is_refused_after_a_second_failure(self, tmp_path):
        db, account_id = _cc_account(tmp_path)
        result = _second_failure(db, account_id, "cc_rt_1")

        assert result.success is True  # the access-only recovery
        row = db.get_account(account_id)
        assert row["cc_access_token"] == "live_at"
        assert row["cc_refresh_token"] is None
        assert row["cc_refresh_failure_type"] == "invalid_grant"
        assert condemned_fingerprints(row["cc_refresh_failed_token_fp"]) == BOTH
        # The next importer still refuses both dead tokens.
        assert dead_chain_adoption(row, _live("cc_rt_1")) is None
        assert dead_chain_adoption(row, _live("cc_rt_2")) is None

    def test_the_just_failed_token_is_refused(self, tmp_path):
        db, account_id = _cc_account(tmp_path)
        assert _second_failure(db, account_id, "cc_rt_2").success is True

        row = db.get_account(account_id)
        assert row["cc_refresh_token"] is None
        assert row["cc_refresh_failure_type"] == "invalid_grant"
        assert condemned_fingerprints(row["cc_refresh_failed_token_fp"]) == BOTH

    def test_a_genuinely_new_token_is_adopted_and_the_dead_ones_stay_condemned(self, tmp_path):
        db, account_id = _cc_account(tmp_path)
        assert _second_failure(db, account_id, "cc_rt_3").success is True

        row = db.get_account(account_id)
        assert row["cc_refresh_token"] == "cc_rt_3"
        assert row["cc_refresh_failure_type"] is None
        assert row["cc_refresh_last_failed_at"] is None
        assert condemned_fingerprints(row["cc_refresh_failed_token_fp"]) == BOTH

    def test_the_first_failure_records_one_fingerprint(self, tmp_path):
        db, account_id = _cc_account(tmp_path)
        _died(db, account_id, "cc_rt_1", None)
        assert db.get_account(account_id)["cc_refresh_failed_token_fp"] == (
            token_fingerprint("cc_rt_1"))

    def test_an_adoption_records_the_failed_token_too(self, tmp_path):
        """F-7: every branch records the failed token's fingerprint; the
        adoption branch still clears the breaker."""
        from jacked.api.credential_helpers import refresh_token_importable

        db, account_id = _cc_account(tmp_path)
        assert _died(db, account_id, "cc_rt_1", _live("cc_rt_9")).success is True

        row = db.get_account(account_id)
        assert row["cc_refresh_token"] == "cc_rt_9"
        assert row["cc_refresh_failure_type"] is None
        assert row["cc_refresh_last_failed_at"] is None
        assert row["cc_refresh_failed_token_fp"] == token_fingerprint("cc_rt_1")
        # The revived (live) chain still refuses the token that died.
        assert refresh_token_importable(row, "cc_rt_1") is False
        assert refresh_token_importable(row, "cc_rt_9") is True


# ---------------------------------------------------------------------------
# The class: the cc exchange never sends a condemned token
# ---------------------------------------------------------------------------


class TestNoPathSendsACondemnedCcToken:
    def _brought_back(self, tmp_path, *, condemned="cc_rt_1"):
        """A live chain whose refresh token an earlier invalid_grant condemned
        (an importer brought it back and cleared the breaker)."""
        db, account_id = _cc_account(tmp_path)
        db.update_account(account_id, cc_expires_at=int(time.time()) - 60,
                          cc_refresh_failed_token_fp=token_fingerprint(condemned))
        return db, account_id

    def test_the_clean_up_runs_without_a_token_call(self, tmp_path, caplog):
        db, account_id = self._brought_back(tmp_path)
        client = _client(token=(200, {"access_token": "x", "refresh_token": "y",
                                      "expires_in": 3600}))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._live_recovery_payload", return_value=None), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            assert asyncio.run(auth.refresh_cc_token(account_id, db)) is False

        client.post.assert_not_awaited()
        row = db.get_account(account_id)
        assert row["cc_refresh_token"] is None and row["cc_access_token"] is None
        assert row["cc_refresh_failure_type"] == "invalid_grant"
        assert row["cc_refresh_failed_token_fp"] == token_fingerprint("cc_rt_1")
        assert _lines(caplog, "not sending the Claude Code refresh token") == [
            f"Account {account_id}: not sending the Claude Code refresh token; an "
            "earlier invalid_grant condemned it (caller cc)"
        ]
        assert "cc_rt_1" not in caplog.text

    def test_the_clean_up_still_adopts_a_new_live_lineage(self, tmp_path):
        db, account_id = self._brought_back(tmp_path)
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._live_recovery_payload", return_value=_live("cc_rt_9")):
            assert asyncio.run(auth.refresh_cc_token(account_id, db)) is True

        client.post.assert_not_awaited()
        row = db.get_account(account_id)
        assert row["cc_refresh_token"] == "cc_rt_9"
        assert row["cc_refresh_failure_type"] is None
        assert row["cc_refresh_failed_token_fp"] == token_fingerprint("cc_rt_1")

    def test_a_token_that_was_never_condemned_is_exchanged(self, tmp_path):
        db, account_id = self._brought_back(tmp_path, condemned="cc_rt_0")
        client = _client(token=(200, {"access_token": "cc_at_new",
                                      "refresh_token": "cc_rt_new", "expires_in": 3600}))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            assert asyncio.run(auth.refresh_cc_token(account_id, db)) is True

        assert client.post.await_count == 1
        assert client.post.await_args.kwargs["json"]["refresh_token"] == "cc_rt_1"
        assert db.get_account(account_id)["cc_refresh_token"] == "cc_rt_new"
