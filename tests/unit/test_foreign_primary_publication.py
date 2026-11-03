"""No publisher hands another account's primary token to Claude Code (#152i).

``validate_account`` binds a cross-account verdict to the row's primary
refresh token (``foreign_token_fp``). While the verdict stands, the row's
primary tokens belong to a different account. ``build_oauth_data`` is the one
payload builder every publisher uses (the switch engine, the auto-swap, the
launch, the authority guard's re-stamp), so it refuses to fall back to those
tokens: it raises ``ForeignPrimaryTokens`` when the row has no usable Claude
Code chain of its own. Every publisher then fails closed with its own
refusal, and the window keeper sends no bound primary token either.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from unittest import mock

import click
import pytest

from jacked.api import authority_guard, usage_monitor
from jacked.api.credential_helpers import (
    FOREIGN_PRIMARY_CODE,
    FOREIGN_PRIMARY_MESSAGE,
    ForeignPrimaryTokens,
    build_oauth_data,
    publishes_foreign_primary,
)
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import IdentityAxis, SwitchContext, SwitchOutcome
from jacked.credentials.runtime import _activation_preflight, _row_payload, activate_account
from jacked.web.database import Database
from jacked.web.refresh_verdict import token_fingerprint
from tests.unit.test_credential_transactions import _request
from tests.unit.test_usage_monitor import (
    _patch_swap_credential_helpers,
    _SwapFakeDB,
    _TestCertifiedEngine,
)

FUTURE_S = int(time.time()) + 6 * 3600
BOUND = token_fingerprint("rt-foreign")


def _bound_row(**overrides) -> dict:
    row = {
        "id": 7,
        "email": "owner@test.com",
        "organization_uuid": "org-a",
        "access_token": "at-foreign",
        "refresh_token": "rt-foreign",
        "expires_at": FUTURE_S,
        "foreign_token_fp": BOUND,
        "cc_access_token": None,
        "cc_refresh_token": None,
        "cc_expires_at": None,
        "cc_refresh_failure_type": None,
        "validation_status": "invalid",
    }
    row.update(overrides)
    return row


def _own_cc(**overrides) -> dict:
    return _bound_row(
        cc_access_token="cc-own", cc_refresh_token="cc-rt-own", cc_expires_at=FUTURE_S, **overrides
    )


# --- the chokepoint ------------------------------------------------------------


class TestThePayloadBuilderRefuses:
    def test_a_bound_row_without_a_claude_code_chain_is_refused(self):
        with pytest.raises(ForeignPrimaryTokens) as excinfo:
            build_oauth_data(_bound_row())
        assert str(excinfo.value) == FOREIGN_PRIMARY_MESSAGE
        assert "at-foreign" not in str(excinfo.value) and "rt-foreign" not in str(excinfo.value)

    def test_a_bound_row_with_its_own_claude_code_chain_publishes_that_chain(self):
        oauth = build_oauth_data(_own_cc())
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("cc-own", "cc-rt-own")

    def test_a_bound_row_whose_chain_cannot_be_published_is_refused(self):
        # An expired cc access token with no cc refresh token falls back to
        # the primary pair in an unbound row; a bound row is refused instead.
        row = _bound_row(cc_access_token="cc-old", cc_refresh_token=None, cc_expires_at=1)
        assert publishes_foreign_primary(row) is True
        with pytest.raises(ForeignPrimaryTokens):
            build_oauth_data(row)

    def test_an_unbound_row_publishes_its_primary_access_token_as_before(self):
        # The primary refresh token is jacked's own and is never published.
        row = _bound_row(foreign_token_fp=None)
        assert publishes_foreign_primary(row) is False
        oauth = build_oauth_data(row)
        assert (oauth["accessToken"], oauth["refreshToken"]) == ("at-foreign", None)

    def test_a_verdict_about_another_token_does_not_bind_the_row(self):
        row = _bound_row(foreign_token_fp=token_fingerprint("rt-older"))
        assert publishes_foreign_primary(row) is False


# --- the switch engine ---------------------------------------------------------


class TestTheEngineRefusesBeforeAnyWrite:
    def test_the_activation_is_refused_before_the_platform_is_resolved(self, caplog):
        db = mock.MagicMock()
        with mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            side_effect=AssertionError("the platform must not be resolved"),
        ), caplog.at_level(logging.WARNING, logger="jacked.credentials.runtime"):
            result = activate_account(db, _bound_row(), SwitchContext.MANUAL, "op-foreign")

        assert result.outcome is SwitchOutcome.UNUSABLE
        assert result.code == FOREIGN_PRIMARY_CODE
        assert result.message == FOREIGN_PRIMARY_MESSAGE
        assert result.committed_authority == IdentityAxis(None, "unchanged")
        assert result.published_fingerprint == ""
        db.get_account.assert_not_called()
        lines = [r.getMessage() for r in caplog.records if "activation refused" in r.getMessage()]
        assert lines == [f"Account 7: activation refused; {FOREIGN_PRIMARY_MESSAGE}"]
        assert "at-foreign" not in caplog.text and "rt-foreign" not in caplog.text

    def test_a_bound_row_with_its_own_chain_reaches_the_engine(self):
        seen = []

        def resolve(home):
            seen.append(home)
            raise OSError("stop here: the platform was resolved")

        with mock.patch("jacked.credentials.runtime.detect_claude_identity", side_effect=resolve):
            result = activate_account(mock.MagicMock(), _own_cc(), SwitchContext.MANUAL, "op-cc")

        assert seen and result.outcome is SwitchOutcome.UNSUPPORTED  # the OSError, not the refusal
        assert result.code != FOREIGN_PRIMARY_CODE

    def test_a_republish_is_never_refused_by_the_rows_verdict(self):
        """A re-stamp publishes the authority's own payload, never the row's."""
        with mock.patch(
            "jacked.credentials.runtime.detect_claude_identity", side_effect=OSError("resolved")
        ):
            result = activate_account(
                mock.MagicMock(), _bound_row(), SwitchContext.REASSERT, "op-restamp",
                republish={"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}},
                expected_digest="0" * 64,
            )
        assert result.code != FOREIGN_PRIMARY_CODE

    def test_the_preflight_refuses_a_row_bound_inside_the_lease(self, caplog):
        """The early check read an unbound row; a validation bound it before
        the lease: the preflight refuses (the engine answers busy) and the
        request publishes nothing."""
        unbound = _bound_row(foreign_token_fp=None)
        db = mock.MagicMock()
        db.get_account.return_value = _bound_row()
        request = replace(_request(_row_payload(7, unbound)), account_id=7)

        with mock.patch(
            "jacked.credentials.runtime.reconcile_outgoing_before_activation",
            return_value=True,
        ), caplog.at_level(logging.WARNING, logger="jacked.credentials.runtime"):
            prepared = _activation_preflight(db, request)()

        assert prepared is None
        assert f"Account 7: activation refused; {FOREIGN_PRIMARY_MESSAGE}" in caplog.text
        assert "at-foreign" not in caplog.text


# --- the auto-swap -------------------------------------------------------------


class TestTheAutoSwapSkipsABoundTarget:
    def test_the_target_is_skipped_with_one_line_and_no_engine_call(self, monkeypatch, caplog):
        _patch_swap_credential_helpers(monkeypatch)
        monkeypatch.setattr(usage_monitor, "_read_active_account_id", lambda: 99)
        captured: dict = {}
        db = _SwapFakeDB(captured)
        engine = _TestCertifiedEngine(db, active_id=99, target=_bound_row())

        with caplog.at_level(logging.WARNING, logger="jacked.api.usage_monitor"):
            ok = asyncio.run(
                usage_monitor._execute_swap(
                    db,
                    active_acct_id=99,
                    active_acct={"id": 99, "email": "old@test"},
                    target=_bound_row(),
                    reason="higher tier emerged: T0 vs T2",
                    trigger="higher_tier_emerged",
                    usage_5h=20,
                    usage_7d=50,
                    ws_registry=None,
                    transaction_engine=engine,
                )
            )

        assert ok is False
        assert engine.activations == []
        assert "record_swap" not in captured  # no journal row for a write that never ran
        lines = [r.getMessage() for r in caplog.records if "Auto-swap to account 7 refused" in r.getMessage()]
        assert lines == [f"Auto-swap to account 7 refused; {FOREIGN_PRIMARY_MESSAGE}"]
        assert "at-foreign" not in caplog.text

    def test_the_request_builder_raises_for_a_bound_target(self):
        with pytest.raises(ForeignPrimaryTokens):
            usage_monitor._auto_swap_request(_bound_row())

    def test_a_bound_target_with_its_own_chain_builds_a_request(self):
        request = usage_monitor._auto_swap_request(_own_cc())
        assert request.payload.to_mapping()["claudeAiOauth"]["accessToken"] == "cc-own"


# --- the launch ----------------------------------------------------------------


def _launch_db(tmp_path, *, bound=True, own_cc=False) -> tuple[Database, int]:
    db = Database(str(tmp_path / "launch.db"))
    acct = db.create_account(
        "owner@test.com", "at-foreign", FUTURE_S, refresh_token="rt-foreign",
        organization_uuid="org-a",
    )
    updates: dict = {}
    if bound:
        updates.update(foreign_token_fp=BOUND, validation_status="invalid")
    if own_cc:
        updates.update(cc_access_token="cc-own", cc_refresh_token="cc-rt-own", cc_expires_at=FUTURE_S)
    if updates:
        db.update_account(acct["id"], **updates)
    return db, acct["id"]


class TestTheLaunchRefusesABoundRow:
    def test_the_resolver_refuses_with_the_use_routes_copy(self, tmp_path):
        from jacked.launch import resolve_account

        db, account_id = _launch_db(tmp_path)
        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException) as excinfo:
                resolve_account(account_id, db)

        message = excinfo.value.message
        assert message.startswith(f"Account {account_id} has invalid credentials. Re-auth first.")
        assert FOREIGN_PRIMARY_MESSAGE in message
        assert "at-foreign" not in message and "rt-foreign" not in message

    def test_a_bound_row_with_its_own_chain_resolves(self, tmp_path):
        from jacked.launch import resolve_account

        db, account_id = _launch_db(tmp_path, own_cc=True)
        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            assert resolve_account(account_id, db)["id"] == account_id

    def test_the_publish_refuses_a_row_bound_after_the_resolver(self, tmp_path):
        """The resolver saw an unbound row; a validation bound it before the
        per-account file was written: the launch stops, and no credential
        file for the account exists."""
        from jacked.launch import prepare_account_dir

        db, account_id = _launch_db(tmp_path, bound=False)
        account = db.get_account(account_id)
        db.update_account(account_id, foreign_token_fp=BOUND, validation_status="invalid")

        # The file-authority topology: no global activation runs, so the
        # scoped file write is the only publisher (the engine's own refusal
        # covers the global-authority topology).
        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"), \
             mock.patch("jacked.launch.should_refresh", return_value=False), \
             mock.patch("jacked.launch.scoped_launch_needs_global_activation", return_value=False), \
             mock.patch("jacked.launch._activate_launch_credentials") as activate:
            with pytest.raises(click.ClickException) as excinfo:
                prepare_account_dir(account, db)

        activate.assert_not_called()
        assert FOREIGN_PRIMARY_MESSAGE in excinfo.value.message
        assert not (tmp_path / "accounts" / str(account_id) / ".credentials.json").exists()


# --- the authority guard's publication record ---------------------------------


def test_a_bound_row_requested_nothing_publishable():
    assert authority_guard._requested_fingerprint(_bound_row(), None) == ""


def test_a_republish_fingerprint_is_the_authoritys_pair():
    republish = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}
    assert len(authority_guard._requested_fingerprint(_bound_row(), republish)) == 16


# --- the window keeper ---------------------------------------------------------


class TestTheKeeperSendsNoBoundPrimaryToken:
    def test_the_candidates_carry_no_bound_primary_token(self):
        assert usage_monitor._keeper_candidates(_bound_row()) == (None, None)
        assert usage_monitor._keeper_candidates(_own_cc()) == ("cc-own", None)
        assert usage_monitor._keeper_candidates(_bound_row(foreign_token_fp=None)) == (
            None, "at-foreign")

    @pytest.mark.parametrize("bound", [True, False], ids=["bound", "unbound"])
    def test_the_ladders_primary_fallback_re_reads_the_verdict(self, bound):
        """A failed cc ping falls back to the primary token of the re-read
        row; a row bound meanwhile gets no fallback ping."""
        from jacked.web.window_keeper import PingResult

        acct = _own_cc(access_token="pri-new")
        fresh = _own_cc(access_token="pri-new", foreign_token_fp=BOUND if bound else None)
        db = mock.MagicMock()
        db.get_account.return_value = fresh
        pings = []

        async def ping(token, *, account_id=None):
            pings.append(token)
            return PingResult(ok=True, status=200)

        with mock.patch("jacked.api.credential_helpers.cc_rotation_block", return_value="holders_unknown"), \
             mock.patch("jacked.web.window_keeper.ping_account", side_effect=ping):
            outcome = asyncio.run(
                usage_monitor._keeper_auth_ladder(db, acct, "cc-own", True, {"cc-own"})
            )

        if bound:
            assert pings == [] and outcome.good_token is None
        else:
            assert pings == ["pri-new"] and outcome.good_token == "pri-new"


def test_the_live_store_ownership_shortcut_still_holds():
    """A sanity check on the fixture: the bound row's own cc chain is the
    row's lineage, so the downgrade guard still protects it."""
    from jacked.credentials.refresh_guard import refuses_refresh_downgrade

    live = {"claudeAiOauth": {"accessToken": "cc-own", "refreshToken": "cc-rt-own"}}
    target = {"accessToken": "cc-own", "refreshToken": None}
    assert refuses_refresh_downgrade(target, live, _own_cc()) is not None
    assert CredentialPayload.from_mapping(live).identity.account_id is None
