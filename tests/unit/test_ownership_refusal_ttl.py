"""A refused ownership verdict has a lifetime; a verdict that names another
owner does not.

The profile endpoint answers about the TOKEN with 401 or 403. That is a
refusal that names no owner: the import stops, and the launch sync keeps the
file (``ownership_refusal_is_status``). An edge or policy 403 can be
transient, so the refusal is asked again after
``_OWNERSHIP_REFUSAL_RETRY_SECONDS`` instead of being kept for the life of
the service. A 200 whose profile names another account is final for the
token's life. A failed request (None, 5xx) is unknown and is asked again
after the shorter ``_OWNERSHIP_RETRY_SECONDS``.
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from jacked.api import credential_helpers as ch
from jacked.api.authority_guard import ProfileLookup

LOGGER = "jacked.api.credential_helpers"
ROW = {
    "id": 4,
    "email": "owner@example.com",
    "organization_uuid": "org-own",
    "cc_access_token": "held",
    "cc_refresh_token": "rt",
}
PAYLOAD = {
    "claudeAiOauth": {
        "accessToken": "fresh",
        "refreshToken": "other-rt",
        "expiresAt": 9_999_999_999_000,
    }
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    ch.clear_ownership_cache()
    yield
    ch.clear_ownership_cache()


def _lookup(*results):
    """A fake profile endpoint that answers ``results`` in order (the last
    answer repeats) and counts its requests."""
    answers = list(results)
    calls: list[str] = []

    def fake(token):
        calls.append(token)
        return answers[min(len(calls), len(answers)) - 1]

    return fake, calls


def _profile(email):
    return ProfileLookup(
        200, {"account": {"email": email}, "organization": {"uuid": "org-own"}}
    )


def _age_cache(seconds):
    """Make every cached verdict ``seconds`` older."""
    for key, (verdict, at, final) in list(ch._ownership_cache.items()):
        ch._ownership_cache[key] = (verdict, at - seconds, final)


def _ask():
    return ch.live_payload_ownership(ROW, PAYLOAD, require_stamp=False)


# --- the refusal window ------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_is_asked_again_after_its_window(status):
    fake, calls = _lookup(ProfileLookup(status))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
        _age_cache(ch._OWNERSHIP_REFUSAL_RETRY_SECONDS - 1)
        assert _ask() is False
        assert len(calls) == 1  # inside the window: the verdict is reused
        _age_cache(2)
        assert _ask() is False
        assert len(calls) == 2  # the window passed: one fresh request


def test_the_refusal_window_is_longer_than_the_unknown_window():
    assert ch._OWNERSHIP_REFUSAL_RETRY_SECONDS > ch._OWNERSHIP_RETRY_SECONDS


def test_a_refusal_that_turns_into_a_proof_is_adopted_after_the_window():
    """An edge 403 followed by a 200 that names the owner: the second
    request proves the token, so the launch sync imports on a later pass."""
    fake, calls = _lookup(ProfileLookup(403), _profile("owner@example.com"))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
        _age_cache(ch._OWNERSHIP_REFUSAL_RETRY_SECONDS + 1)
        assert _ask() is True
    assert len(calls) == 2


# --- final verdicts ----------------------------------------------------------


def test_a_profile_that_names_another_owner_is_final():
    fake, calls = _lookup(_profile("intruder@example.com"))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
        _age_cache(10 * ch._OWNERSHIP_REFUSAL_RETRY_SECONDS)
        assert _ask() is False
    assert len(calls) == 1
    ((_verdict, _at, final),) = ch._ownership_cache.values()
    assert final is True


def test_a_proven_owner_is_final():
    fake, calls = _lookup(_profile("owner@example.com"))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is True
        _age_cache(10 * ch._OWNERSHIP_REFUSAL_RETRY_SECONDS)
        assert _ask() is True
    assert len(calls) == 1


# --- the unknown window and the forget rule ----------------------------------


@pytest.mark.parametrize("status", [None, 500, 503])
def test_an_unknown_verdict_is_asked_again_after_the_short_window(status):
    fake, calls = _lookup(ProfileLookup(status))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is None
        _age_cache(ch._OWNERSHIP_RETRY_SECONDS - 1)
        assert _ask() is None
        assert len(calls) == 1
        _age_cache(2)
        assert _ask() is None
        assert len(calls) == 2


def test_a_verdict_is_bound_to_the_row_identity_it_was_made_against():
    """A re-auth that moves the row to another identity misses the cache,
    so the same token is proven again against the new row."""
    fake, calls = _lookup(_profile("owner@example.com"))
    moved = {**ROW, "organization_uuid": "org-moved"}
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is True
        assert ch.live_payload_ownership(moved, PAYLOAD, require_stamp=False) is False
        assert _ask() is True  # the first row's final verdict is still cached
    assert len(calls) == 2
    assert {key[0] for key in ch._ownership_cache} == {ROW["id"]}
    assert len(ch._ownership_cache) == 2


def test_the_final_refusal_accessor_names_only_an_owner_mismatch():
    fake, _ = _lookup(ProfileLookup(401))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
    assert ch.ownership_refusal_is_final(ROW, "fresh") is False  # asked again later

    ch.clear_ownership_cache()
    fake, _ = _lookup(_profile("intruder@example.com"))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
    assert ch.ownership_refusal_is_final(ROW, "fresh") is True
    assert ch.ownership_refusal_is_final({**ROW, "organization_uuid": "moved"}, "fresh") is False
    assert ch.ownership_refusal_is_final(ROW, "other-token") is False
    assert ch.ownership_refusal_is_final(ROW, None) is False

    ch.clear_ownership_cache()
    fake, _ = _lookup(_profile("owner@example.com"))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is True
    assert ch.ownership_refusal_is_final(ROW, "fresh") is False


def test_forgetting_a_row_drops_its_refusal_at_once():
    fake, calls = _lookup(ProfileLookup(401))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        assert _ask() is False
        ch.forget_ownership_verdicts(ROW["id"])
        assert _ask() is False
    assert len(calls) == 2


def test_the_refusal_line_names_the_status_and_no_token(caplog):
    fake, _calls = _lookup(ProfileLookup(403))
    with mock.patch.object(ch, "_ownership_lookup", fake):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _ask()
    assert "live token refused by the profile endpoint (HTTP 403)" in caplog.text
    assert "fresh" not in caplog.text
    assert "other-rt" not in caplog.text
    assert "owner@example.com" not in caplog.text
