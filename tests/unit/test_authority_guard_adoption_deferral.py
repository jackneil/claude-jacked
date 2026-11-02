"""A failed ownership lookup defers an adoption; it never settles the payload.

The authority guard adopts the rotated tokens of a foreign payload into the
row that owns them. When the guard has no profile in hand, the shared
ownership rule can answer "unknown": the profile request did not complete.
Then nothing is proven either way. The guard writes nothing, it does not
overwrite the authority, and the payload stays owed. A later pass proves the
tokens and adopts them (``_adoption_updates`` returns ``ADOPTION_DEFERRED``).
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from jacked.api import authority_guard, credential_helpers
from jacked.api.authority_guard import (
    ADOPTION_DEFERRED,
    HealResult,
    ProfileLookup,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from tests.unit.test_authority_guard import _Activator, _db, _foreign_payload, _profile

RETRY_REASON = "ownership lookup failed; adoption retried next pass"
# The ownership rule refuses an expired token, so these payloads never expire.
FAR_FUTURE_MS = 9_999_999_999_000
OWNERSHIP_LOOKUP = "jacked.api.credential_helpers._ownership_lookup"
CAROL = ProfileLookup(
    200, {"account": {"email": "carol@test.com"}, "organization": {"uuid": "org-3"}}
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_authority_guard_state()
    credential_helpers.clear_ownership_cache()
    yield
    reset_authority_guard_state()
    credential_helpers.clear_ownership_cache()


def _the_lookup_retry_window_passes() -> None:
    """Age the cached ownership verdicts past the retry window of a failed
    request (``_OWNERSHIP_RETRY_SECONDS``)."""
    cache = credential_helpers._ownership_cache
    window = credential_helpers._OWNERSHIP_RETRY_SECONDS
    for key, (verdict, decided_at, *rest) in list(cache.items()):
        cache[key] = (verdict, decided_at - window, *rest)


def test_an_unstamped_adoption_whose_lookup_failed_stays_owed():
    """Pass 1 identifies the payload, and the adoption CAS loses three times.
    Pass 2 remembers the account, so it sends no identification request, and
    the ownership request fails. Before the fix, that failure counted as a
    refusal: the payload settled, and pass 3 answered "foreign payload
    already handled" while the only copy of the rotation was in the
    authority."""
    db = _db()
    payload = _foreign_payload(expires=FAR_FUTURE_MS)
    identify = _profile("carol@test.com", "org-3")
    activate = _Activator()

    def heal(now):
        return heal_foreign_authority(
            db, now=now, profile_lookup=identify, activate=activate,
            read_authority=lambda: payload,
        )

    with mock.patch.object(db, "update_account_if_unchanged", return_value=False) as cas:
        first = heal(100.0)
    assert cas.call_count == 3
    assert first == HealResult(
        "skipped", foreign_account_id=3,
        reason="the row kept changing; adoption retried next pass",
    )

    failed = mock.Mock(return_value=ProfileLookup(503))
    with mock.patch(OWNERSHIP_LOOKUP, failed):
        second = heal(110.0)
        # Inside the retry window, the cached failure answers: no new request.
        assert heal(120.0) == second
    assert second == HealResult("skipped", foreign_account_id=3, reason=RETRY_REASON)
    assert failed.call_count == 1
    assert identify.calls == ["live_at_3"]  # identified once, on pass 1
    assert authority_guard._handled["settled"] is False
    assert authority_guard._handled["account_id"] == 3
    assert db.updates == []
    assert activate.calls == []  # the authority was not overwritten

    _the_lookup_retry_window_passes()
    with mock.patch(OWNERSHIP_LOOKUP, return_value=CAROL):
        third = heal(130.0)
    assert third == HealResult("reasserted", foreign_account_id=3, desired_account_id=11)
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert db.accounts[3]["cc_refresh_token"] == "live_rt_3"
    assert len(activate.calls) == 1  # the reassert ran after the adoption


def test_a_stamped_adoption_whose_lookup_failed_waits_out_the_retry_delay():
    """The stamped path has the identifying profile in hand, so the ownership
    rule sends no request there and cannot answer "unknown" today. The stub
    makes the first adoption answer ADOPTION_DEFERRED, to pin what the
    caller does with it: skip, keep the payload owed for the retry delay,
    then adopt."""
    db = _db()
    payload = {"_jackedAccountId": 3, **_foreign_payload(expires=FAR_FUTURE_MS)}
    identify = _profile("carol@test.com", "org-3")
    real_import = authority_guard._import_rotated_tokens
    deferred_once = [ADOPTION_DEFERRED]

    def import_rotated_tokens(*args, **kwargs):
        if deferred_once:
            return deferred_once.pop()
        return real_import(*args, **kwargs)

    def heal():
        return heal_foreign_authority(
            db, now=100.0, profile_lookup=identify, activate=_Activator(),
            read_authority=lambda: payload,
        )

    with mock.patch.object(authority_guard, "_import_rotated_tokens", import_rotated_tokens):
        before = time.time()
        first = heal()
        after = time.time()
        retry_at = authority_guard._stamped_handled["retry_at"]
        within_the_delay = heal()
        with authority_guard._state_lock:
            authority_guard._stamped_handled["retry_at"] = 0.0  # the delay elapsed
        later = heal()

    delay = authority_guard._STAMPED_LOOKUP_RETRY_SECONDS
    assert first == HealResult("skipped", foreign_account_id=3, reason=RETRY_REASON)
    assert retry_at is not None and before + delay <= retry_at <= after + delay
    assert within_the_delay == HealResult("none", foreign_account_id=3)
    assert later == HealResult("rotation_adopted", foreign_account_id=3)
    assert identify.calls == ["live_at_3", "live_at_3"]
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert db.accounts[3]["cc_refresh_token"] == "live_rt_3"
