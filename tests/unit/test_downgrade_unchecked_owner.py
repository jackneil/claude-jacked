"""The refresh-downgrade guard fails closed on an owner it cannot check (#149i).

The guard protects a live refresh token from a null one of the same account.
When the live tokens are not the row's and the profile request fails, the
owner is unknown: the guard refuses (a transient refusal, outcome BUSY, code
OWNERSHIP_UNCHECKED) instead of letting the write go ahead. A refusal is
retryable; a lost refresh lineage is not.
"""

from __future__ import annotations

from dataclasses import replace
from unittest import mock

import pytest

from jacked.api import credential_helpers as ch
from jacked.api.authority_guard import ProfileLookup
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import CapabilityMode, SwitchOutcome
from jacked.credentials.refresh_guard import (
    OWNERSHIP_UNCHECKED_CODE,
    REFRESH_DOWNGRADE_REASON,
    REFRESH_DOWNGRADE_UNCHECKED_REASON,
    refuses_refresh_downgrade,
)
from tests.unit.test_credential_transactions import _engine, _payload, _request

ROW = {"id": 7, "email": "owner@example.com", "organization_uuid": "org-a",
       "cc_access_token": None, "cc_refresh_token": None, "cc_refresh_failure_type": "invalid_grant"}
TARGET = {"accessToken": "primary-at", "refreshToken": None}
LIVE = {"claudeAiOauth": {"accessToken": "live-at", "refreshToken": "live-rt",
                          "expiresAt": 9_999_999_999_000}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    ch.clear_ownership_cache()
    yield
    ch.clear_ownership_cache()


def test_an_unchecked_owner_is_a_transient_refusal():
    with mock.patch.object(ch, "_ownership_lookup", lambda token: ProfileLookup(503)):
        assert refuses_refresh_downgrade(TARGET, LIVE, ROW) == REFRESH_DOWNGRADE_UNCHECKED_REASON


def test_a_proven_owner_is_the_final_refusal():
    profile = ProfileLookup(200, {"account": {"email": "owner@example.com"}, "organization": {"uuid": "org-a"}})
    with mock.patch.object(ch, "_ownership_lookup", lambda token: profile):
        assert refuses_refresh_downgrade(TARGET, LIVE, ROW) == REFRESH_DOWNGRADE_REASON


def test_another_owner_lets_the_write_through():
    profile = ProfileLookup(200, {"account": {"email": "other@example.com"}, "organization": {"uuid": "org-a"}})
    with mock.patch.object(ch, "_ownership_lookup", lambda token: profile):
        assert refuses_refresh_downgrade(TARGET, LIVE, ROW) is None


def test_the_engine_maps_the_unchecked_refusal_to_busy_and_writes_nothing():
    before = _payload(1, "before")
    engine, _repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, before)
    request = replace(
        _request(_payload(2, "target")),
        refresh_downgrade_check=lambda held: REFRESH_DOWNGRADE_UNCHECKED_REASON,
    )

    result = engine.activate(request)

    assert result.outcome is SwitchOutcome.BUSY
    assert result.code == OWNERSHIP_UNCHECKED_CODE
    assert result.message == REFRESH_DOWNGRADE_UNCHECKED_REASON
    assert store.read().payload.digest == before.digest


def test_the_engine_maps_the_final_refusal_to_unusable():
    before = _payload(1, "before")
    engine, _repo, store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, before)
    request = replace(
        _request(_payload(2, "target")),
        refresh_downgrade_check=lambda held: REFRESH_DOWNGRADE_REASON,
    )

    result = engine.activate(request)

    assert result.outcome is SwitchOutcome.UNUSABLE
    assert result.code == "CC_REAUTH_REQUIRED"
    assert store.read().payload.digest == before.digest
    assert isinstance(CredentialPayload.from_mapping(store.read().payload.to_mapping()), CredentialPayload)
