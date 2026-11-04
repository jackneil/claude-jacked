"""The authority guard settles a repair after its attempt, never before (#157i).

Both repairs of a foreign authority write (re-stamping the account that
holds the authority, or reasserting the desired account) share one retry
rule: the one-per-minute floor spaces the attempts, a committed outcome
settles the payload, a held switch lease is retried, and any other outcome
counts toward ``RESTAMP_MAX_ATTEMPTS`` before the payload is settled with
one WARNING.
"""

from __future__ import annotations

import logging

import pytest

from jacked.api import authority_guard
from jacked.api.authority_guard import (
    RESTAMP_MAX_ATTEMPTS,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from jacked.credentials.models import SwitchContext, SwitchOutcome
from tests.unit.test_authority_guard import (
    _db,
    _foreign_payload,
    _profile,
    _switch_result,
)

LOGGER = "jacked.api.authority_guard"
COMMITTED = {
    SwitchOutcome.COMMITTED,
    SwitchOutcome.COMMITTED_DEGRADED,
    SwitchOutcome.OBSERVED_TARGET_UNFENCED,
}


@pytest.fixture(autouse=True)
def _clean_guard_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


class _SequenceActivator:
    """Return the given outcomes in order; the last outcome repeats."""

    def __init__(self, *outcomes: SwitchOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple] = []

    def __call__(self, db, account, context, operation_id):
        self.calls.append((account, context, operation_id))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        return _switch_result(outcome, int(account["id"]))


class _RaisingActivator:
    """An engine that raises on every attempt. The message carries a token."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, db, account, context, operation_id):
        self.calls.append((account, context, operation_id))
        raise RuntimeError("engine failed while it held live_at_3")


def _unusable_desired_db():
    """Desired row 11 cannot be reasserted: it has no Claude Code refresh token."""
    db = _db()
    db.accounts[11].update({"cc_refresh_token": None})
    return db


def _heal(db, activate, *, now, payload=None, lookup=None):
    payload = payload or _foreign_payload()
    return heal_foreign_authority(
        db,
        now=now,
        profile_lookup=lookup or _profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: payload,
    )


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _assert_values_free(caplog) -> None:
    text = caplog.text
    assert "@" not in text, "a log line names an email"
    for secret in ("live_at_3", "live_rt_3", "db_at_", "db_rt_"):
        assert secret not in text, f"a log line carries a token ({secret})"


_DURABLE_FAILURES = pytest.mark.parametrize(
    ("make_activator", "outcome_label"),
    [
        (lambda: _SequenceActivator(SwitchOutcome.FAILED_PRESERVED), "failed_preserved"),
        (_RaisingActivator, "none"),
    ],
    ids=["failed_preserved", "raises"],
)


# --- (a) the appendix acceptance scenario -----------------------------------


def test_a_restamp_that_lost_the_switch_lease_is_retried_after_the_floor():
    """The first re-stamp lost the switch lease. Before #157i the payload was
    recorded as handled BEFORE the attempt, so the retry never ran and the
    authority stayed unstamped until Claude Code rotated the token."""
    db = _unusable_desired_db()
    lookup = _profile("carol@test.com", "org-3")
    activate = _SequenceActivator(
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS, SwitchOutcome.COMMITTED
    )

    first = _heal(db, activate, now=100.0, lookup=lookup)
    assert first.action == "skipped"
    assert "no Claude Code tokens" in first.reason
    assert len(activate.calls) == 1

    floored = _heal(db, activate, now=130.0, lookup=lookup)
    assert floored.action == "skipped"
    assert floored.reason == "reassert rate limit is active"
    assert (floored.foreign_account_id, floored.desired_account_id) == (3, 11)
    assert len(activate.calls) == 1

    retried = _heal(db, activate, now=161.0, lookup=lookup)
    assert retried.action == "adopted"
    assert retried.foreign_account_id == 3
    assert retried.desired_account_id == 11
    assert len(activate.calls) == 2

    settled = _heal(db, activate, now=300.0, lookup=lookup)
    assert settled.reason == "foreign payload already handled"
    assert len(activate.calls) == 2

    # Every attempt re-stamps the holder, never the unusable desired row.
    assert [call[0]["id"] for call in activate.calls] == [3, 3]
    assert all(call[1] is SwitchContext.REASSERT for call in activate.calls)
    assert all(call[2].startswith("restamp-") for call in activate.calls)
    # The retries reuse the cached identification: one profile call in all.
    assert lookup.calls == ["live_at_3"]


def test_the_refusal_warning_is_logged_once_per_payload(caplog):
    db = _unusable_desired_db()
    activate = _SequenceActivator(
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.COMMITTED,
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for now in (100.0, 110.0, 120.0, 161.0, 190.0, 222.0, 300.0):
            _heal(db, activate, now=now)

    assert len(activate.calls) == 3
    refusals = [m for m in _warnings(caplog) if "cannot be reasserted" in m]
    assert refusals == [
        "Desired account 11 cannot be reasserted (desired account has no Claude "
        "Code tokens); keeping account 3, which now holds the authority"
    ]
    _assert_values_free(caplog)


def test_a_floored_pass_logs_no_refusal_until_the_attempt_runs(caplog):
    """The floor is shared: a reassert of an earlier payload took the slot,
    so the re-stamp of the next payload waits, and its refusal WARNING is
    logged by the pass whose attempt runs."""
    db = _db()
    activate = _SequenceActivator(SwitchOutcome.COMMITTED)
    earlier = _heal(db, activate, now=100.0, payload=_foreign_payload(access="earlier"))
    assert earlier.action == "reasserted"
    db.accounts[11].update({"cc_refresh_token": None})

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        floored = _heal(db, activate, now=130.0)
        assert floored.action == "skipped"
        assert floored.reason == "reassert rate limit is active"
        assert not [m for m in _warnings(caplog) if "cannot be reasserted" in m]
        retried = _heal(db, activate, now=161.0)

    assert retried.action == "adopted"
    assert [call[0]["id"] for call in activate.calls] == [11, 3]
    assert len([m for m in _warnings(caplog) if "cannot be reasserted" in m]) == 1


# --- (b) a durable re-stamp failure is capped -------------------------------


@_DURABLE_FAILURES
def test_a_durable_restamp_failure_settles_after_the_attempt_cap(
    make_activator, outcome_label, caplog
):
    db = _unusable_desired_db()
    activate = make_activator()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        results = [_heal(db, activate, now=now) for now in (100.0, 130.0, 161.0, 222.0)]
        assert len(activate.calls) == RESTAMP_MAX_ATTEMPTS == 3
        last = _heal(db, activate, now=283.0)

    assert len(activate.calls) == 3, "a settled payload is not attempted again"
    assert last.reason == "foreign payload already handled"
    assert [r.action for r in results] == ["skipped"] * 4
    # A failed attempt never shortens the floor.
    assert results[1].reason == "reassert rate limit is active"
    assert all("no Claude Code tokens" in results[i].reason for i in (0, 2, 3))

    warnings = _warnings(caplog)
    capped = [m for m in warnings if "did not commit" in m]
    assert capped == [
        f"Account 3: the authority re-stamp did not commit after 3 attempts "
        f"(last outcome: {outcome_label}); jacked stops retrying it until the "
        f"authority payload changes"
    ]
    assert sum("cannot be reasserted" in m for m in warnings) == 1
    assert len(warnings) == 2
    _assert_values_free(caplog)


# --- (c) the same cap applies to the reassert branch ------------------------


@_DURABLE_FAILURES
def test_a_durable_reassert_failure_settles_after_the_attempt_cap(
    make_activator, outcome_label, caplog
):
    db = _db()
    activate = make_activator()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        results = [_heal(db, activate, now=now) for now in (100.0, 130.0, 161.0, 222.0)]
        assert len(activate.calls) == 3
        last = _heal(db, activate, now=283.0)

    assert len(activate.calls) == 3, "a settled payload is not attempted again"
    assert [call[0]["id"] for call in activate.calls] == [11, 11, 11]
    assert all(call[2].startswith("reassert-") for call in activate.calls)
    assert last.reason == "foreign payload already handled"
    assert [r.action for r in results] == ["skipped"] * 4
    assert results[0].reason == f"reassert outcome {outcome_label}"
    assert results[1].reason == "reassert rate limit is active"

    assert _warnings(caplog) == [
        f"Account 11: the authority reassert did not commit after 3 attempts "
        f"(last outcome: {outcome_label}); jacked stops retrying it until the "
        f"authority payload changes"
    ]
    _assert_values_free(caplog)


# --- (d) the reset clears the attempt counter -------------------------------


def test_reset_clears_the_attempt_counter():
    db = _db()
    activate = _SequenceActivator(SwitchOutcome.FAILED_PRESERVED)
    _heal(db, activate, now=100.0)
    _heal(db, activate, now=161.0)
    assert authority_guard._handled["attempts"] == 2

    reset_authority_guard_state()

    assert authority_guard._handled == {
        "digest": None,
        "account_id": None,
        "settled": True,
        "attempts": 0,
        "transient": 0,
        "refusal_logged": False,
    }
    # The same payload has the whole budget again.
    for now in (1000.0, 1061.0, 1122.0):
        assert _heal(db, activate, now=now).action == "skipped"
    assert _heal(db, activate, now=1183.0).reason == "foreign payload already handled"
    assert len(activate.calls) == 5


# --- the retry budget belongs to one payload --------------------------------


def test_a_new_payload_starts_its_own_attempt_budget():
    """The second payload is first recorded by a floored pass. Its budget
    must start at zero there, or it would settle after one attempt."""
    db = _db()
    activate = _SequenceActivator(SwitchOutcome.FAILED_PRESERVED)
    first = _foreign_payload(access="live_at_3a")
    second = _foreign_payload(access="live_at_3b")

    _heal(db, activate, now=100.0, payload=first)
    _heal(db, activate, now=161.0, payload=first)
    floored = _heal(db, activate, now=190.0, payload=second)
    assert floored.reason == "reassert rate limit is active"
    assert len(activate.calls) == 2

    for now in (222.0, 283.0, 344.0):
        assert _heal(db, activate, now=now, payload=second).action == "skipped"
    assert len(activate.calls) == 5
    after = _heal(db, activate, now=405.0, payload=second)
    assert after.reason == "foreign payload already handled"
    assert len(activate.calls) == 5


@pytest.mark.parametrize(
    ("make_db", "target"),
    [(_unusable_desired_db, 3), (_db, 11)],
    ids=["restamp", "reassert"],
)
def test_a_held_switch_lease_never_consumes_the_attempt_budget(make_db, target):
    db = make_db()
    activate = _SequenceActivator(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)
    passes = [100.0 + 61.0 * index for index in range(RESTAMP_MAX_ATTEMPTS + 3)]

    for now in passes:
        assert _heal(db, activate, now=now).action == "skipped"

    assert len(activate.calls) == len(passes)
    assert {call[0]["id"] for call in activate.calls} == {target}
    assert authority_guard._handled["settled"] is False


def test_a_lease_retry_after_failures_still_settles_on_commit():
    db = _unusable_desired_db()
    activate = _SequenceActivator(
        SwitchOutcome.FAILED_PRESERVED,
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.FAILED_PRESERVED,
        SwitchOutcome.COMMITTED_DEGRADED,
    )

    actions = [_heal(db, activate, now=now).action for now in (100.0, 161.0, 222.0, 283.0)]

    assert actions == ["skipped", "skipped", "skipped", "adopted"]
    assert _heal(db, activate, now=344.0).reason == "foreign payload already handled"
    assert len(activate.calls) == 4


# --- the shared settle rule, walked over every outcome the engine has --------


@pytest.mark.parametrize("outcome", [*SwitchOutcome, None], ids=lambda o: str(o))
def test_every_switch_outcome_has_a_settle_rule(outcome, caplog):
    digest = "d" * 64
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        settled = [
            authority_guard._settle_after(digest, 3, outcome, row_id=3, repair="re-stamp")
            for _ in range(RESTAMP_MAX_ATTEMPTS)
        ]

    if outcome in COMMITTED:
        assert settled[0] is True
    elif outcome in authority_guard._TRANSIENT_OUTCOMES:
        # "Not now": the payload stays owed, no attempt counts, nothing is logged.
        assert settled == [False] * RESTAMP_MAX_ATTEMPTS
        assert authority_guard._handled["attempts"] == 0
        assert _warnings(caplog) == []
    else:
        assert settled == [False] * (RESTAMP_MAX_ATTEMPTS - 1) + [True]
        assert len(_warnings(caplog)) == 1  # the cap settles with one WARNING


# --- the re-stamp reports the engine outcome --------------------------------


def test_the_restamp_returns_the_engine_outcome():
    from jacked.api.authority_guard import _restamp_foreign_holder

    db = _db()
    row = db.get_account(3)

    degraded = _SequenceActivator(SwitchOutcome.COMMITTED_DEGRADED)
    assert _restamp_foreign_holder(db, row, degraded) is SwitchOutcome.COMMITTED_DEGRADED
    busy = _SequenceActivator(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)
    assert (
        _restamp_foreign_holder(db, row, busy)
        is SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS
    )
    assert _restamp_foreign_holder(db, row, _RaisingActivator()) is None

    db.accounts[3]["cc_access_token"] = None
    idle = _SequenceActivator(SwitchOutcome.COMMITTED)
    assert _restamp_foreign_holder(db, row, idle) is None
    assert idle.calls == []


# --- a repair never overwrites a payload jacked does not hold ---------------


def _uuid_mismatch_lookup():
    """The profile matches row 3's email and organization but names another
    account uuid, so the shared ownership rule refuses the payload."""

    def lookup(access_token: str) -> dict:
        lookup.calls.append(access_token)
        return {
            "account": {"email": "carol@test.com", "uuid": "uuid-someone-else"},
            "organization": {"uuid": "org-3"},
        }

    lookup.calls = []
    return lookup


@pytest.mark.parametrize(
    "make_db", [_db, _unusable_desired_db], ids=["reassert", "restamp"]
)
def test_a_payload_the_ownership_rule_refuses_is_never_overwritten(make_db, caplog):
    """The refusal used to read as "nothing new to adopt": the reassert
    branch reasserted over the payload, and the re-stamp branch published
    row 3's STALE tokens over it. Both destroy a refresh lineage that jacked
    does not hold."""
    db = make_db()
    db.accounts[3]["account_uuid"] = "uuid-carol"
    lookup = _uuid_mismatch_lookup()
    activate = _SequenceActivator(SwitchOutcome.COMMITTED)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        first = _heal(db, activate, now=100.0, lookup=lookup)
        second = _heal(db, activate, now=161.0, lookup=lookup)

    assert activate.calls == [], "the authority must not be overwritten"
    assert first.action == "unknown_account"
    assert first.foreign_account_id is None
    assert "not proven" in first.reason
    assert db.updates == []
    assert db.accounts[3]["cc_access_token"] == "db_at_3"
    assert db.accounts[3]["cc_refresh_token"] == "db_rt_3"
    # Settled: the next pass sends no second profile request.
    assert second.reason == "foreign payload already handled"
    assert lookup.calls == ["live_at_3"]
    assert any(
        "will not overwrite" in r.getMessage() and "account 3" in r.getMessage()
        for r in caplog.records
    )
    _assert_values_free(caplog)


def test_a_stamped_payload_the_ownership_rule_refuses_adopts_nothing():
    """The stamped path never writes the authority. The new refusal result
    must not read as an adoption (a negative sentinel is truthy)."""
    db = _db()
    db.accounts[3]["account_uuid"] = "uuid-carol"
    lookup = _uuid_mismatch_lookup()
    activate = _SequenceActivator(SwitchOutcome.COMMITTED)
    payload = {"_jackedAccountId": 3, **_foreign_payload()}

    first = _heal(db, activate, now=100.0, payload=payload, lookup=lookup)
    second = _heal(db, activate, now=161.0, payload=payload, lookup=lookup)

    assert first.action == "none"
    assert "not proven" in first.reason
    assert second.action == "none"
    assert db.updates == []
    assert activate.calls == []
    assert lookup.calls == ["live_at_3"], "the refused payload is settled"


def test_an_unstamped_primary_publication_is_still_repaired():
    """jacked holds the primary lineage of its own fallback publication, so
    that payload is not an ownership refusal: the reassert still runs."""
    db = _db()
    db.accounts[3].update(
        cc_access_token=None, cc_refresh_token=None, access_token="primary_at_3"
    )
    activate = _SequenceActivator(SwitchOutcome.COMMITTED)

    result = _heal(
        db,
        activate,
        now=100.0,
        payload={"claudeAiOauth": {"accessToken": "primary_at_3", "refreshToken": None}},
    )

    assert result.action == "reasserted"
    assert [call[0]["id"] for call in activate.calls] == [11]
    assert db.accounts[3]["cc_access_token"] is None


def test_the_adoption_reports_an_ownership_refusal():
    from jacked.api.authority_guard import ADOPTION_REFUSED, _import_rotated_tokens

    db = _db()
    db.accounts[3]["account_uuid"] = "uuid-carol"
    profile = _uuid_mismatch_lookup()("live_at_3")

    adopted = _import_rotated_tokens(db, db.get_account(3), _foreign_payload(), profile)

    assert adopted == ADOPTION_REFUSED
    assert db.updates == []
