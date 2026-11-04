"""The refresh-downgrade guard fails closed on an owner it cannot check (#149i).

The guard protects a live refresh token from a null one. The write goes ahead
only on positive evidence that the live tokens belong to another account: a
row that stores the refresh token, or a 200 that names another owner. When the
profile request fails, or nothing can prove the owner (an expired access token,
a 401, a 403), the guard refuses. A stamp or a per-account directory that names
the target keeps the pair for good; any other payload gets the transient
refusal (outcome BUSY, code OWNERSHIP_UNCHECKED). A refusal is retryable; a lost
refresh lineage is not.
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
    downgrade_check_for,
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


EXPIRED = {"claudeAiOauth": {"accessToken": "live-at", "refreshToken": "live-rt", "expiresAt": 1000}}


def _no_request(token):
    raise AssertionError("an expired access token costs no profile request")


@pytest.mark.parametrize(
    ("stamp", "directory", "reason"),
    [
        (7, False, REFRESH_DOWNGRADE_REASON),
        (None, True, REFRESH_DOWNGRADE_REASON),
        (None, False, REFRESH_DOWNGRADE_UNCHECKED_REASON),
        (9, False, REFRESH_DOWNGRADE_UNCHECKED_REASON),
    ],
    ids=["stamp-names-target", "directory-names-target", "unstamped", "stamp-names-another"],
)
def test_an_expired_pair_never_gets_a_null_refresh_token(stamp, directory, reason):
    live = EXPIRED if stamp is None else {"_jackedAccountId": stamp, **EXPIRED}
    with mock.patch.object(ch, "_ownership_lookup", _no_request):
        assert refuses_refresh_downgrade(TARGET, live, ROW, directory_names_target=directory) == reason


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    ("stamp", "directory", "reason"),
    [
        (7, False, REFRESH_DOWNGRADE_REASON),
        (None, True, REFRESH_DOWNGRADE_REASON),
        (None, False, REFRESH_DOWNGRADE_UNCHECKED_REASON),
        (9, False, REFRESH_DOWNGRADE_UNCHECKED_REASON),
    ],
    ids=["stamp-names-target", "directory-names-target", "unstamped", "stamp-names-another"],
)
def test_a_status_refusal_never_gets_a_null_refresh_token(status, stamp, directory, reason):
    live = LIVE if stamp is None else {"_jackedAccountId": stamp, **LIVE}
    with mock.patch.object(ch, "_ownership_lookup", lambda token: ProfileLookup(status)):
        assert refuses_refresh_downgrade(TARGET, live, ROW, directory_names_target=directory) == reason


def test_a_final_verdict_lets_the_write_through_whatever_the_stamp_says():
    """The stamp survives Claude Code's own writes: a 200 outranks it, and the
    verdict stands after the access token expired."""
    profile = ProfileLookup(200, {"account": {"email": "other@example.com"}, "organization": {"uuid": "org-a"}})
    with mock.patch.object(ch, "_ownership_lookup", lambda token: profile):
        assert refuses_refresh_downgrade(TARGET, {"_jackedAccountId": 7, **LIVE}, ROW) is None
    with mock.patch.object(ch, "_ownership_lookup", _no_request):
        assert refuses_refresh_downgrade(TARGET, {"_jackedAccountId": 7, **EXPIRED}, ROW) is None


def test_a_row_that_stores_the_refresh_token_is_positive_evidence():
    with mock.patch.object(ch, "_ownership_lookup", _no_request):
        assert refuses_refresh_downgrade(TARGET, EXPIRED, ROW, lineage_known=lambda rt: 9) is None


def test_an_expiry_that_is_not_a_real_time_is_not_a_rotation():
    live = {"claudeAiOauth": {"accessToken": "live-at", "refreshToken": "live-rt", "expiresAt": "soon"}}
    with mock.patch.object(ch, "_ownership_lookup", _no_request):
        assert refuses_refresh_downgrade(TARGET, live, ROW, directory_names_target=True) is None


def test_the_engine_check_reads_the_stamp_of_the_authority_object():
    row = {**ROW, "access_token": "primary-at", "expires_at": 9_999_999_999}
    held = CredentialPayload.from_mapping({"_jackedAccountId": 7, **EXPIRED})
    other = CredentialPayload.from_mapping({"_jackedAccountId": 9, **EXPIRED})
    with mock.patch.object(ch, "_ownership_lookup", _no_request):
        assert downgrade_check_for(row, TARGET)(held) == REFRESH_DOWNGRADE_REASON
        assert downgrade_check_for(row, TARGET)(other) == REFRESH_DOWNGRADE_UNCHECKED_REASON


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
