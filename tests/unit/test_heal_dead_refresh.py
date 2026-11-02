"""#152i: a revoked primary refresh token is never sent again.

The heal sweep used to clear the invalid_grant breaker and re-exchange the
SAME dead refresh token every 5 minutes (about 288 doomed token-endpoint
calls a day per dead account), because the verdict was not bound to the
token it was about. The verdict is now bound to the token's fingerprint
(``refresh_failed_token_fp``):

- the heal sweep skips the exchange and never clears the breaker;
- no refresh path sends a token whose fingerprint carries the verdict
  (the background loop and the 401 recovery used to retry it once the
  600 s cooldown ran out);
- a new stored refresh token (a re-auth, an imported rotation) clears the
  fingerprint at the storage layer, so the new token is tried once.

No test reaches the network, the Keychain or a real credential store.
"""

import asyncio
import logging
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web import auth
from jacked.web.auth import RefreshMode, _token_fingerprint
from jacked.web.database import Database

SKIP_LINE = "heal skips the exchange; the refresh token was revoked (sign-in required)"
SIGN_IN = "Sign-in required: the refresh token was revoked"
EMAIL = "heal@test.com"


@pytest.fixture(autouse=True)
def _isolated():
    """No probe, no live store, fresh refresh locks for every test."""
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


def _owned_profile():
    return {"account": {"email": EMAIL, "uuid": "acct-uuid-1"},
            "organization": {"uuid": None, "organization_type": "claude_max"}}


def _dead_account(tmp_path, *, failed_at=None, bound=True, access_expired=True):
    """An invalid account whose primary refresh token got invalid_grant."""
    db = Database(str(tmp_path / "heal.db"))
    expires = int(time.time()) + (-60 if access_expired else 3600)
    acct = db.create_account(EMAIL, "dead-at", expires, refresh_token="rt-dead")
    updates = {
        "validation_status": "invalid",
        "refresh_failure_type": "invalid_grant",
        # An old failure: the 600 s cooldown is long over, so only the
        # bound verdict can keep the token from being sent.
        "refresh_last_failed_at": failed_at or int(time.time()) - 3600,
    }
    if bound:
        updates["refresh_failed_token_fp"] = _token_fingerprint("rt-dead")
    db.update_account(acct["id"], **updates)
    return db, acct["id"]


def _verdict(db, account_id):
    row = db.get_account(account_id)
    return (row["refresh_failure_type"], row["refresh_last_failed_at"],
            row["refresh_failed_token_fp"])


def _heal(db, client):
    with patch("jacked.web.auth.Database", return_value=db), \
         patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
        return asyncio.run(auth.heal_invalid_accounts())


def _skip_lines(caplog):
    return [r for r in caplog.records if SKIP_LINE in r.getMessage()]


# ---------------------------------------------------------------------------
# The verdict is recorded against the token that failed
# ---------------------------------------------------------------------------


class TestVerdictBinding:
    @pytest.mark.parametrize("mode", [RefreshMode.PRIMARY,
                                      RefreshMode.PRIMARY_CIRCUIT_BREAKER])
    def test_invalid_grant_binds_the_fingerprint_of_the_failed_token(self, tmp_path, mode):
        db = Database(str(tmp_path / "bind.db"))
        acct = db.create_account(EMAIL, "at", int(time.time()) - 60, refresh_token="rt-1")
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(auth._refresh_token_flow(acct["id"], db, mode))

        assert result.success is False and result.error == "invalid_grant"
        assert client.post.await_count == 1
        row = db.get_account(acct["id"])
        assert row["refresh_failure_type"] == "invalid_grant"
        assert row["refresh_failed_token_fp"] == _token_fingerprint("rt-1")
        assert row["refresh_token"] == "rt-1"

    def test_the_recorded_error_is_terminal(self, tmp_path):
        db = Database(str(tmp_path / "text.db"))
        acct = db.create_account(EMAIL, "at", int(time.time()) - 60, refresh_token="rt-1")
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=_client()):
            asyncio.run(auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY))

        last_error = db.get_account(acct["id"])["last_error"]
        assert last_error == SIGN_IN
        assert "will retry" not in last_error
        assert "—" not in last_error

    def test_revoked_helper_needs_the_type_and_the_matching_fingerprint(self):
        fp = _token_fingerprint("rt")
        row = {"refresh_token": "rt", "refresh_failure_type": "invalid_grant",
               "refresh_failed_token_fp": fp}
        assert auth._primary_refresh_revoked(row) is True
        assert auth._primary_refresh_revoked({**row, "refresh_token": "rt-2"}) is False
        assert auth._primary_refresh_revoked({**row, "refresh_failed_token_fp": None}) is False
        assert auth._primary_refresh_revoked({**row, "refresh_failure_type": "http_503"}) is False
        assert auth._primary_refresh_revoked({**row, "refresh_token": None,
                                              "refresh_failed_token_fp": None}) is False


# ---------------------------------------------------------------------------
# Brief test (1): the sweep never re-sends a revoked token
# ---------------------------------------------------------------------------


class TestHealSkipsRevokedToken:
    def test_three_passes_make_no_token_call_and_keep_the_breaker(self, tmp_path, caplog):
        db, account_id = _dead_account(tmp_path)
        before = _verdict(db, account_id)
        client = _client()
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            results = []
            for _ in range(3):
                caplog.clear()
                results.append(_heal(db, client))
                # Exactly one skip line per account per pass.
                assert len(_skip_lines(caplog)) == 1
                assert not any("clearing circuit breaker" in r.getMessage()
                               for r in caplog.records)

        client.post.assert_not_awaited()
        assert _verdict(db, account_id) == before
        assert [r["confirmed_invalid"] for r in results] == [1, 1, 1]
        assert [r["healed"] for r in results] == [0, 0, 0]
        row = db.get_account(account_id)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == SIGN_IN

    def test_skip_line_names_the_account_and_no_value(self, tmp_path, caplog):
        db, account_id = _dead_account(tmp_path)
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            _heal(db, _client())
        (line,) = _skip_lines(caplog)
        message = line.getMessage()
        assert message.startswith(f"Account {account_id}: ")
        assert "rt-dead" not in message and EMAIL not in message
        assert _token_fingerprint("rt-dead") not in message
        assert "—" not in message

    def test_a_legacy_unbound_verdict_is_tried_once_then_bound(self, tmp_path):
        """A row recorded before the fingerprint column: one exchange binds it."""
        db, account_id = _dead_account(tmp_path, bound=False)
        client = _client()
        _heal(db, client)
        assert client.post.await_count == 1
        assert _verdict(db, account_id)[2] == _token_fingerprint("rt-dead")

        _heal(db, client)
        _heal(db, client)
        assert client.post.await_count == 1


# ---------------------------------------------------------------------------
# Brief test (2): a new stored refresh token gets exactly one exchange
# ---------------------------------------------------------------------------


class TestNewRefreshTokenClearsTheVerdict:
    def test_reauth_token_is_exchanged_once_and_the_fingerprint_rebinds(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        _heal(db, _client())

        # A write that changes the refresh token and nothing else (an
        # import, an older writer) clears the fingerprint by itself.
        db.update_account(account_id, refresh_token="rt-new")
        assert db.get_account(account_id)["refresh_failed_token_fp"] is None

        client = _client()  # the new token is revoked too
        _heal(db, client)
        assert client.post.await_count == 1
        sent = client.post.await_args.kwargs["json"]["refresh_token"]
        assert sent == "rt-new"
        assert _verdict(db, account_id)[2] == _token_fingerprint("rt-new")

        _heal(db, client)
        assert client.post.await_count == 1  # bound again: no second call

    def test_reauth_token_that_works_heals_and_clears_everything(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        # The OAuth re-auth store writes the new pair and clears the breaker.
        db.update_account(account_id, refresh_token="rt-new", access_token="at-new",
                          refresh_failure_type=None, refresh_last_failed_at=None)
        client = _client(token=(200, {"access_token": "at-2", "refresh_token": "rt-2",
                                      "expires_in": 3600}),
                         profile=(200, _owned_profile()))
        result = _heal(db, client)

        assert client.post.await_count == 1
        assert result["healed"] == 1
        assert _verdict(db, account_id) == (None, None, None)
        assert db.get_account(account_id)["refresh_token"] == "rt-2"

    def test_the_upsert_path_clears_the_fingerprint(self, tmp_path):
        """Re-adding the account (create_account upsert) is a new token too."""
        db, account_id = _dead_account(tmp_path)
        again = db.create_account(EMAIL, "at-new", int(time.time()) + 3600,
                                  refresh_token="rt-new")
        assert again["id"] == account_id
        assert db.get_account(account_id)["refresh_failed_token_fp"] is None

    def test_a_write_that_keeps_the_token_keeps_the_fingerprint(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        db.update_account(account_id, refresh_token="rt-dead", last_error="x")
        assert db.get_account(account_id)["refresh_failed_token_fp"] == (
            _token_fingerprint("rt-dead"))


# ---------------------------------------------------------------------------
# Brief test (3): a network breaker still defers as today
# ---------------------------------------------------------------------------


class TestNetworkBreakerStillDefers:
    def test_reply_lost_breaker_in_cooldown_defers(self, tmp_path, caplog):
        db = Database(str(tmp_path / "net.db"))
        acct = db.create_account(EMAIL, "at", int(time.time()) + 3600, refresh_token="rt")
        db.update_account(acct["id"], validation_status="invalid",
                          refresh_failure_type="reply_lost",
                          refresh_last_failed_at=int(time.time()) - 5)
        client = _client()
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        assert result["deferred"] == 1
        client.post.assert_not_awaited()
        client.get.assert_not_awaited()
        assert db.get_account(acct["id"])["refresh_failure_type"] == "reply_lost"
        assert not _skip_lines(caplog)

    def test_unreachable_network_defers_a_revoked_account_too(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        before = _verdict(db, account_id)
        client = _client()
        with patch("jacked.web.auth._network_reachable", AsyncMock(return_value=False)):
            result = _heal(db, client)
        assert result["deferred"] == 1
        client.post.assert_not_awaited()
        client.get.assert_not_awaited()
        assert _verdict(db, account_id) == before


# ---------------------------------------------------------------------------
# Brief test (4): a working access token still heals
# ---------------------------------------------------------------------------


class TestValidateFallbackStillHeals:
    def test_access_token_that_works_heals_without_a_token_call(self, tmp_path, caplog):
        db, account_id = _dead_account(tmp_path, access_expired=False)
        before = _verdict(db, account_id)
        client = _client(profile=(200, _owned_profile()))
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = _heal(db, client)

        assert result["healed"] == 1 and result["confirmed_invalid"] == 0
        client.post.assert_not_awaited()
        assert client.get.await_count == 1
        assert db.get_account(account_id)["validation_status"] == "valid"
        # The refresh token is still revoked: its verdict stays.
        assert _verdict(db, account_id) == before
        assert len(_skip_lines(caplog)) == 1


# ---------------------------------------------------------------------------
# The class: no refresh path sends a revoked token
# ---------------------------------------------------------------------------


class TestNoPathSendsARevokedToken:
    @pytest.mark.parametrize("mode", [RefreshMode.PRIMARY,
                                      RefreshMode.PRIMARY_CIRCUIT_BREAKER])
    def test_the_flow_refuses_without_a_token_call(self, tmp_path, caplog, mode):
        db, account_id = _dead_account(tmp_path)
        before = _verdict(db, account_id)
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            result = asyncio.run(auth._refresh_token_flow(account_id, db, mode))

        assert result.success is False
        assert result.error == "invalid_grant"
        assert result.chain == "primary"
        client.post.assert_not_awaited()
        assert _verdict(db, account_id) == before
        refusals = [r.getMessage() for r in caplog.records
                    if "not sending the primary refresh token" in r.getMessage()]
        assert refusals == [
            f"Account {account_id}: not sending the primary refresh token; "
            f"it was revoked (sign-in required, caller {mode.value})"
        ]

    def test_background_refresh_loop_does_not_send_it(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        db.update_account(account_id, validation_status="valid")
        client = _client()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.read_active_account_id",
                   return_value=None), \
             patch("jacked.api.credential_helpers.live_holder_account_ids",
                   return_value=set()):
            counts = asyncio.run(auth.refresh_all_expiring_tokens(db=db))

        client.post.assert_not_awaited()
        assert counts["failed"] == 1 and counts["refreshed"] == 0

    def test_validate_does_not_send_it_and_says_sign_in(self, tmp_path):
        db, account_id = _dead_account(tmp_path)
        client = _client(profile=(401, {}))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            verdict = asyncio.run(auth.validate_account(account_id, db))

        client.post.assert_not_awaited()
        assert verdict == {"valid": False, "error": SIGN_IN}
        row = db.get_account(account_id)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == SIGN_IN

    def test_usage_401_does_not_send_it_and_says_sign_in(self, tmp_path):
        """The active account's usage poll keeps the terminal text too."""
        db, account_id = _dead_account(tmp_path)
        db.update_account(account_id, validation_status="valid")
        client = _client(profile=(401, {}))  # every GET (the usage call) is a 401
        auth._account_usage_state.clear()
        try:
            with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
                assert asyncio.run(auth.fetch_usage(account_id, db)) is None
        finally:
            auth._account_usage_state.clear()

        client.post.assert_not_awaited()
        row = db.get_account(account_id)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == SIGN_IN

    def test_validate_401_without_a_verdict_keeps_todays_text(self, tmp_path):
        db = Database(str(tmp_path / "plain.db"))
        acct = db.create_account(EMAIL, "at", int(time.time()) + 3600, refresh_token="rt")
        client = _client(token=(500, {}), profile=(401, {}))
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            verdict = asyncio.run(auth.validate_account(acct["id"], db))

        assert client.post.await_count == 1
        assert verdict == {"valid": False, "error": "Token invalid (HTTP 401)"}
        assert db.get_account(acct["id"])["last_error"] == (
            "Token invalid (HTTP 401), refresh failed")


# ---------------------------------------------------------------------------
# Brief test (5): the migration on an old schema
# ---------------------------------------------------------------------------


class TestFingerprintMigration:
    def _old_schema(self, tmp_path):
        """A database from before the fingerprint column and its trigger."""
        path = tmp_path / "old.db"
        db = Database(str(path))
        acct = db.create_account(EMAIL, "at", int(time.time()) + 3600, refresh_token="rt")
        db.update_account(acct["id"], refresh_failure_type="invalid_grant",
                          refresh_last_failed_at=123)
        db.close()
        conn = sqlite3.connect(str(path))
        conn.execute(f"DROP TRIGGER IF EXISTS {Database.REFRESH_FP_TRIGGER}")
        conn.execute("ALTER TABLE accounts DROP COLUMN refresh_failed_token_fp")
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        conn.close()
        assert "refresh_failed_token_fp" not in cols
        return path, acct["id"]

    def test_column_and_trigger_exist_after_database_init(self, tmp_path):
        path, account_id = self._old_schema(tmp_path)
        db = Database(str(path))

        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        conn.close()
        assert "refresh_failed_token_fp" in cols
        assert Database.REFRESH_FP_TRIGGER in triggers
        row = db.get_account(account_id)
        assert row["refresh_failed_token_fp"] is None  # no backfill
        assert row["refresh_failure_type"] == "invalid_grant"
        assert row["refresh_last_failed_at"] == 123

    def test_migration_is_idempotent_and_the_trigger_works(self, tmp_path):
        path, account_id = self._old_schema(tmp_path)
        Database(str(path)).close()
        db = Database(str(path))  # second init: no error, no duplicate
        db.update_account(account_id, refresh_failed_token_fp="abc")
        db.update_account(account_id, refresh_token="rt-other")
        assert db.get_account(account_id)["refresh_failed_token_fp"] is None

    def test_column_is_writable_and_in_the_row_model(self, tmp_path):
        from jacked.web.database import Account

        db = Database(str(tmp_path / "model.db"))
        acct = db.create_account(EMAIL, "at", int(time.time()) + 3600, refresh_token="rt")
        assert db.update_account(acct["id"], refresh_failed_token_fp="f00d") is True
        row = db.get_account(acct["id"])
        assert Account(**row).refresh_failed_token_fp == "f00d"


# ---------------------------------------------------------------------------
# The cc chain: its invalid_grant verdict is bound to the failed token too
# ---------------------------------------------------------------------------


class TestCcVerdictBinding:
    """The cc clean-up removes the failed refresh token from the row, so a
    later importer (reconcile, the launch sync) cannot tell that token from
    a new lineage by comparing ``cc_refresh_token``. The fingerprint keeps
    the verdict tied to the token that failed."""

    def _cc_row(self, tmp_path):
        db = Database(str(tmp_path / "cc.db"))
        acct = db.create_account(EMAIL, "pat", int(time.time()) + 3600, refresh_token="prt")
        db.update_account(acct["id"], cc_access_token="cc_at", cc_refresh_token="cc_dead",
                          cc_expires_at=int(time.time()) + 600)
        return db, acct["id"]

    def _died(self, db, account_id, live):
        with patch("jacked.web.auth._live_recovery_payload", return_value=live):
            return asyncio.run(auth._cc_chain_died(
                account_id, db, db.get_account(account_id), "cc_dead"))

    def test_clear_path_binds_the_failed_token(self, tmp_path):
        db, account_id = self._cc_row(tmp_path)
        result = self._died(db, account_id, None)
        assert result.error == "invalid_grant"
        row = db.get_account(account_id)
        assert row["cc_refresh_token"] is None
        assert row["cc_refresh_failure_type"] == "invalid_grant"
        assert row["cc_refresh_failed_token_fp"] == _token_fingerprint("cc_dead")

    def test_access_only_recovery_binds_the_failed_token(self, tmp_path):
        db, account_id = self._cc_row(tmp_path)
        live = {"accessToken": "live_at", "refreshToken": "cc_dead",
                "expiresAt": (int(time.time()) + 3600) * 1000}
        assert self._died(db, account_id, live).success
        row = db.get_account(account_id)
        assert row["cc_access_token"] == "live_at" and row["cc_refresh_token"] is None
        assert row["cc_refresh_failed_token_fp"] == _token_fingerprint("cc_dead")

    def test_adoption_clears_the_breaker(self, tmp_path):
        """The shared dead-chain rule decides the adoption columns."""
        db, account_id = self._cc_row(tmp_path)
        live = {"accessToken": "live_at", "refreshToken": "cc_new",
                "expiresAt": (int(time.time()) + 3600) * 1000}
        assert self._died(db, account_id, live).success
        row = db.get_account(account_id)
        assert row["cc_refresh_token"] == "cc_new"
        assert row["cc_refresh_failure_type"] is None
        assert row["cc_refresh_last_failed_at"] is None

    def test_column_exists_after_init_on_an_old_schema(self, tmp_path):
        path = tmp_path / "old_cc.db"
        Database(str(path)).close()
        conn = sqlite3.connect(str(path))
        conn.execute("ALTER TABLE accounts DROP COLUMN cc_refresh_failed_token_fp")
        conn.commit()
        conn.close()
        Database(str(path)).close()
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        conn.close()
        assert "cc_refresh_failed_token_fp" in cols

    def test_the_verdict_module_imports_nothing_from_jacked(self):
        """Importers of live tokens (reconcile, the launch sync) can use the
        predicate without an import cycle through jacked.web.auth."""
        import ast
        import inspect

        from jacked.web import refresh_verdict

        tree = ast.parse(inspect.getsource(refresh_verdict))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        assert not any(m and m.startswith("jacked") for m in modules)
