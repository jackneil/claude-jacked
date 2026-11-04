"""A non-Claude account never reaches an Anthropic token or usage path.

Codex and Grok rows store a marker in ``access_token`` and no Anthropic
refresh token. The guards used to test ``provider == "codex"``, so a Grok row
went down every Claude path: the token chooser handed its marker to the
usage API, validation sent it to the profile API and marked the row invalid,
and the launcher would write it into the Claude credential store. These tests
pin the guard to ``provider != "claude"`` for every provider jacked knows.
"""

import asyncio
from unittest.mock import patch

import click
import pytest

from jacked.providers import is_claude, provider_of
from jacked.web import auth
from jacked.web.database import Database

NON_CLAUDE = ("codex", "grok")


def test_provider_of_defaults_to_claude():
    assert provider_of({}) == "claude"
    assert provider_of({"provider": None}) == "claude"
    assert provider_of({"provider": " Grok "}) == "grok"
    assert is_claude({"provider": "claude"}) and is_claude({})
    assert not is_claude({"provider": "grok"})


@pytest.mark.parametrize("provider", NON_CLAUDE)
def test_refresh_gates_refuse_every_non_claude_provider(provider):
    row = {"provider": provider, "refresh_token": "x", "expires_at": 0,
           "cc_refresh_token": "rt", "cc_expires_at": 0, "access_token": "m"}
    assert auth.should_refresh(row) is False
    assert auth.should_refresh_cc(row) is False
    assert auth.choose_usage_token(row) is None


@pytest.mark.parametrize("provider", NON_CLAUDE)
def test_refresh_cc_token_never_exchanges_a_non_claude_row(tmp_path, provider):
    db = Database(str(tmp_path / "j.db"))
    acct = db.create_account("a@x.com", f"{provider}-managed", 4102444800,
                             provider=provider, organization_uuid="u-1")
    db.update_account(acct["id"], cc_refresh_token="rt", cc_expires_at=0)
    with patch.object(auth, "_refresh_token_flow") as flow:
        result = asyncio.run(auth.refresh_cc_token_result(acct["id"], db, force=True))
    flow.assert_not_called()
    assert result.outcome is auth.CCRefreshOutcome.NO_REFRESH_TOKEN


def test_validate_a_grok_row_does_not_call_the_profile_api(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    acct = db.create_account("g@x.com", "grok-managed", 4102444800,
                             provider="grok", organization_uuid="user-1")
    db.update_account(acct["id"], validation_status="valid")
    with patch.object(auth, "_get_profile") as profile:
        result = asyncio.run(auth.validate_account(acct["id"], db))
    profile.assert_not_called()
    assert result["valid"] is True
    assert db.get_account(acct["id"])["validation_status"] == "valid"


def test_fetch_usage_for_a_grok_row_sends_nothing_to_anthropic(tmp_path):
    db = Database(str(tmp_path / "j.db"))
    acct = db.create_account("g@x.com", "grok-managed", 4102444800,
                             provider="grok", organization_uuid="user-1")
    with patch.object(auth.httpx, "AsyncClient") as client:
        result = asyncio.run(auth.fetch_usage(acct["id"], db))
    client.assert_not_called()
    assert result == {"_cached": True}


@pytest.mark.parametrize("provider", NON_CLAUDE)
def test_launch_refuses_a_non_claude_account(provider):
    from jacked import launch

    row = {"id": 3, "email": "a@x.com", "provider": provider,
           "access_token": f"{provider}-managed", "is_active": 1}
    with pytest.raises(click.ClickException, match=f"(?i)is a {provider} account"):
        launch.prepare_account_dir(row, db=None)


@pytest.mark.parametrize("provider", NON_CLAUDE)
def test_keeper_skips_a_non_claude_account(provider):
    from jacked.api.keeper import process

    row = {"id": 3, "email": "a@x.com", "provider": provider}
    with patch.object(process, "_note_keeper_skip") as skip, \
            patch.object(process, "_confirm_pending") as pending:
        outcome = asyncio.run(process._keeper_process_account(None, row, 60))
    pending.assert_not_called()
    assert outcome == "skipped"
    assert skip.call_args.args[-1] == f"{provider}_no_keeper"
