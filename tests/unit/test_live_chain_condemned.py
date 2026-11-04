"""A condemned Claude Code refresh token is never imported, dead chain or live.

After a new authorization revives a cc chain, a stale payload of the same
account can still carry the token the endpoint condemned. The dead-chain
rule already refuses it; the live-chain importers (reconcile and the
authority guard) ask the same fingerprint, so the condemned token never
re-enters a row.
"""

from __future__ import annotations

import time

from jacked.api import authority_guard, credential_helpers
from jacked.web.refresh_verdict import token_fingerprint

FUTURE_MS = 9_999_999_999_000


def _live_row(**overrides):
    row = {
        "id": 3,
        "email": "carol@test.com",
        "organization_uuid": "org-3",
        "access_token": "primary-at",
        "refresh_token": "primary-rt",
        "cc_access_token": "cc-at",
        "cc_refresh_token": "cc-rt",
        "cc_expires_at": int(time.time()) + 3600,
        "cc_refresh_failure_type": None,
        "cc_refresh_last_failed_at": None,
        "cc_refresh_failed_token_fp": token_fingerprint("cc-rt-dead"),
    }
    row.update(overrides)
    return row


def test_the_helper_names_only_the_condemned_token():
    row = _live_row()
    assert credential_helpers.refresh_token_importable(row, "cc-rt-dead") is False
    assert credential_helpers.refresh_token_importable(row, "cc-rt-new") is True
    assert credential_helpers.refresh_token_importable({}, "cc-rt-dead") is True


def test_reconcile_refuses_a_condemned_token_on_a_live_chain(caplog):
    row = _live_row()
    live = {"accessToken": "cc-at-2", "refreshToken": "cc-rt-dead", "expiresAt": FUTURE_MS}

    with caplog.at_level("INFO", logger="jacked.api.credential_helpers"):
        updates = credential_helpers._refresh_import(3, row, live)

    assert "cc_refresh_token" not in updates
    assert "not importing a condemned Claude Code refresh token" in caplog.text
    assert "cc-rt-dead" not in caplog.text


def test_reconcile_still_imports_a_new_token_on_a_live_chain():
    row = _live_row()
    live = {"accessToken": "cc-at-2", "refreshToken": "cc-rt-new", "expiresAt": FUTURE_MS}

    updates = credential_helpers._refresh_import(3, row, live)

    assert updates.get("cc_refresh_token") == "cc-rt-new"


def test_the_guard_refuses_a_condemned_token_on_a_live_chain():
    row = _live_row()
    payload = {"claudeAiOauth": {"accessToken": "cc-at-2", "refreshToken": "cc-rt-dead", "expiresAt": FUTURE_MS}}
    profile = {"account": {"email": "carol@test.com"}, "organization": {"uuid": "org-3"}}

    updates = authority_guard._adoption_updates(row, payload, profile)

    assert updates.get("cc_access_token") == "cc-at-2"
    assert "cc_refresh_token" not in updates


def test_the_guard_adopts_a_new_token_on_a_live_chain():
    row = _live_row()
    payload = {"claudeAiOauth": {"accessToken": "cc-at-2", "refreshToken": "cc-rt-new", "expiresAt": FUTURE_MS}}
    profile = {"account": {"email": "carol@test.com"}, "organization": {"uuid": "org-3"}}

    updates = authority_guard._adoption_updates(row, payload, profile)

    assert updates.get("cc_refresh_token") == "cc-rt-new"


# --- a refused condemned token leaves the pass-through fields alone ----------


def test_reconcile_keeps_the_rows_pass_through_fields_when_it_refuses_a_condemned_token():
    row = _live_row(cc_oauth_extra="kept-extra")
    live = {"accessToken": "cc-at-2", "refreshToken": "cc-rt-dead", "expiresAt": FUTURE_MS,
            "refreshTokenExpiresAt": 123456789000}

    updates = credential_helpers._refresh_import(3, row, live)

    assert "cc_refresh_token" not in updates
    assert "cc_oauth_extra" not in updates  # another lineage's expiry is not bound to cc-rt


def test_the_guard_keeps_the_rows_pass_through_fields_when_it_refuses_a_condemned_token():
    row = _live_row(cc_oauth_extra="kept-extra")
    payload = {"claudeAiOauth": {"accessToken": "cc-at-2", "refreshToken": "cc-rt-dead",
                                 "expiresAt": FUTURE_MS, "refreshTokenExpiresAt": 123456789000}}
    profile = {"account": {"email": "carol@test.com"}, "organization": {"uuid": "org-3"}}

    updates = authority_guard._adoption_updates(row, payload, profile)

    assert updates.get("cc_access_token") == "cc-at-2"
    assert "cc_refresh_token" not in updates
    assert "cc_oauth_extra" not in updates
