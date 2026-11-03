"""A re-stamp republishes exactly the authority's pair, under a digest guard (#154i).

The engine reports the fingerprint of the pair a committed activation
published, so the guard's own-write evidence never comes from a row that a
concurrent writer could have changed. A request that republishes the payload
it read names that payload's digest; when the authority holds something else
by the time the engine reads it inside the lease (Claude Code rotated
meanwhile), the engine refuses with CONCURRENT_WRITE and writes nothing, and
the guard adopts the new payload on its next pass.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from jacked.api import stamped_rotation
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.fingerprint import payload_fingerprint
from jacked.credentials.models import CapabilityMode, SwitchOutcome
from jacked.credentials.runtime import _republished_payload
from tests.unit.test_credential_transactions import _engine, _payload, _request


def _oauth(payload: CredentialPayload) -> dict:
    return payload.to_mapping()["claudeAiOauth"]


# --- the engine ---------------------------------------------------------------


def test_a_committed_activation_reports_the_fingerprint_of_what_it_published():
    engine, _repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, _payload(1, "before"))
    target = _payload(2, "target")

    result = engine.activate(_request(target))

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert result.published_fingerprint == payload_fingerprint(_oauth(target))
    assert len(result.published_fingerprint) == 16
    assert store.read().payload.digest == target.digest


def test_a_refused_activation_reports_no_fingerprint():
    engine, _repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, _payload(1, "before"))
    request = replace(_request(_payload(2, "target")), expected_authority_digest="0" * 64)

    result = engine.activate(request)

    assert result.outcome is SwitchOutcome.CONCURRENT_WRITE
    assert result.published_fingerprint == ""


def test_a_changed_authority_refuses_the_republish_and_writes_nothing(caplog):
    before = _payload(1, "before")
    engine, repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, before)
    # The guard read and adopted one payload; Claude Code rotated since.
    request = replace(_request(_payload(2, "target")), expected_authority_digest=_payload(1, "older").digest)

    with caplog.at_level(logging.WARNING, logger="jacked.credentials.transaction"):
        result = engine.activate(request)

    assert result.outcome is SwitchOutcome.CONCURRENT_WRITE
    assert result.message == "the authority changed before the write"
    assert store.read().payload.digest == before.digest  # byte-identical authority
    assert "the authority changed before the write" in caplog.text
    assert "before" not in caplog.text.replace("before the write", "")  # no token value


def test_an_unchanged_authority_lets_the_republish_through():
    before = _payload(1, "before")
    engine, _repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, before)
    request = replace(_request(_payload(2, "target")), expected_authority_digest=before.digest)

    result = engine.activate(request)

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert store.read().payload.digest == _payload(2, "target").digest


# --- the runtime builds the republished payload -------------------------------


def test_the_republished_payload_keeps_the_pair_and_takes_the_stamp():
    authority = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 5}, "_jackedAccountId": 1}

    republished = _republished_payload(7, authority)

    assert republished.identity.account_id == 7
    assert republished.to_mapping()["claudeAiOauth"] == authority["claudeAiOauth"]
    assert authority["_jackedAccountId"] == 1  # the input is not mutated


# --- the pointer write failure is loud ---------------------------------------


class _FailingSettings:
    def get_setting(self, key):
        return None

    def set_settings(self, values):
        raise RuntimeError("database is locked")

    def mark_global_sessions_pending(self):  # never reached
        raise AssertionError("not reached")


def test_a_failed_pointer_write_warns_with_the_exception_type(caplog):
    with caplog.at_level(logging.WARNING, logger="jacked.api.stamped_rotation"):
        with pytest.raises(RuntimeError):
            stamped_rotation._point_at(_FailingSettings(), 5)

    lines = [r.getMessage() for r in caplog.records if "could not record it as the active account" in r.getMessage()]
    assert len(lines) == 1
    assert "Account 5" in lines[0] and "RuntimeError" in lines[0]


# --- the downgrade check sees the object the request publishes ---------------


class _DeadRowDb:
    """A row whose cc chain is dead: build_oauth_data publishes no refresh token."""

    def __init__(self):
        self.row = {"id": 7, "email": "a@b.com", "cc_access_token": "at", "cc_refresh_token": None,
                    "cc_expires_at": 9_999_999_999, "cc_refresh_failure_type": "invalid_grant"}

    def get_account(self, account_id):
        return dict(self.row)


def _preflight(db, request, *, rebuild_payload):
    from unittest import mock

    from jacked.credentials import runtime

    with mock.patch.object(runtime, "reconcile_outgoing_before_activation", lambda _db, _tid: True):
        return runtime._activation_preflight(db, request, rebuild_payload=rebuild_payload)()


def test_a_republish_is_checked_against_the_object_it_publishes():
    from jacked.credentials.runtime import _republished_payload

    db = _DeadRowDb()
    authority = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt-live", "expiresAt": 9_999_999_999_000},
                 "_jackedAccountId": 1}
    held = CredentialPayload.from_mapping(authority)
    request = replace(_request(_republished_payload(7, authority)), account_id=7)

    prepared = _preflight(db, request, rebuild_payload=False)

    assert prepared.payload.to_mapping()["claudeAiOauth"]["refreshToken"] == "rt-live"
    assert prepared.refresh_downgrade_check(held) is None  # the same pair goes back, with a stamp


def test_a_row_publication_over_a_live_pair_is_still_refused():
    from jacked.credentials.refresh_guard import REFRESH_DOWNGRADE_REASON
    from jacked.credentials.runtime import _row_payload

    db = _DeadRowDb()
    held = CredentialPayload.from_mapping(
        {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt-live", "expiresAt": 9_999_999_999_000}}
    )
    request = replace(_request(_row_payload(7, db.row)), account_id=7)

    prepared = _preflight(db, request, rebuild_payload=True)

    assert prepared.payload.to_mapping()["claudeAiOauth"].get("refreshToken") is None
    assert prepared.refresh_downgrade_check(held) == REFRESH_DOWNGRADE_REASON
