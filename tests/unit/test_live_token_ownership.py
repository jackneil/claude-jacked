"""The shared ownership rule's third answer: proven, refused, or unknown.

A profile request that fails for the REQUEST (network, 5xx) is unknown and a
caller may defer. A request that fails for the TOKEN (401, 403) is terminal:
the verdict settles as refused, so the launch sync does not defer such a
file forever, one request every five minutes.
"""

import logging
from unittest import mock

import pytest

from jacked.api import credential_helpers
from jacked.api.authority_guard import ProfileLookup

ROW = {"id": 4, "email": "owner@example.com", "cc_access_token": "held", "cc_refresh_token": "rt"}
PAYLOAD = {"claudeAiOauth": {"accessToken": "fresh", "refreshToken": "other-rt",
                             "expiresAt": 9_999_999_999_000}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    credential_helpers.clear_ownership_cache()
    yield
    credential_helpers.clear_ownership_cache()


def _lookup(result):
    calls = []

    def fake(token):
        calls.append(token)
        return result

    return fake, calls


@pytest.mark.parametrize("status", [401, 403])
def test_a_token_the_endpoint_refuses_settles_as_not_proven(status, caplog):
    fake, calls = _lookup(ProfileLookup(status))
    with mock.patch.object(credential_helpers, "_ownership_lookup", fake):
        with caplog.at_level(logging.WARNING, logger="jacked.api.credential_helpers"):
            first = credential_helpers.live_payload_ownership(ROW, PAYLOAD, require_stamp=False)
        second = credential_helpers.live_payload_ownership(ROW, PAYLOAD, require_stamp=False)
    assert first is False and second is False
    assert len(calls) == 1  # the refusal is cached for the token's life
    assert f"live token refused by the profile endpoint (HTTP {status})" in caplog.text
    assert "fresh" not in caplog.text and "owner@example.com" not in caplog.text


@pytest.mark.parametrize("status", [None, 500, 503, 429])
def test_a_failed_request_is_unknown_and_retried(status, caplog):
    fake, calls = _lookup(ProfileLookup(status))
    with mock.patch.object(credential_helpers, "_ownership_lookup", fake):
        with caplog.at_level(logging.INFO, logger="jacked.api.credential_helpers"):
            verdict = credential_helpers.live_payload_ownership(ROW, PAYLOAD, require_stamp=False)
    assert verdict is None
    assert len(calls) == 1
    assert credential_helpers.live_payload_belongs_to(ROW, PAYLOAD, require_stamp=False) is False
    assert "credential ownership lookup returned" in caplog.text


def test_the_boolean_half_treats_unknown_as_not_proven():
    fake, _ = _lookup(ProfileLookup(503))
    with mock.patch.object(credential_helpers, "_ownership_lookup", fake):
        assert credential_helpers.live_payload_belongs_to(ROW, PAYLOAD, require_stamp=False) is False


def test_a_token_the_row_holds_needs_no_request():
    fake, calls = _lookup(ProfileLookup(500))
    held = {"claudeAiOauth": {"accessToken": "held"}}
    with mock.patch.object(credential_helpers, "_ownership_lookup", fake):
        assert credential_helpers.live_payload_ownership(ROW, held, require_stamp=False) is True
    assert calls == []


def test_forgetting_the_verdicts_of_one_row_keeps_the_other_rows():
    """A re-auth makes the verdicts of its own row stale, and only those."""
    fake, calls = _lookup(ProfileLookup(200, {"account": {"email": "owner@example.com"}}))
    other = {**ROW, "id": 5}
    with mock.patch.object(credential_helpers, "_ownership_lookup", fake):
        for row in (ROW, other):
            assert credential_helpers.live_payload_ownership(row, PAYLOAD, require_stamp=False)
        assert sorted(key[0] for key in credential_helpers._ownership_cache) == [4, 5]

        credential_helpers.forget_ownership_verdicts(4)

        assert [key[0] for key in credential_helpers._ownership_cache] == [5]
        for row in (ROW, other):
            assert credential_helpers.live_payload_ownership(row, PAYLOAD, require_stamp=False)
    assert len(calls) == 3  # row 4 asked again; row 5 used its cached verdict
