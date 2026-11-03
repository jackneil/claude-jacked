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
