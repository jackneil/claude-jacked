"""Transient engine outcomes do not spend the repair budget (#157i).

The engine answers "not now" with four outcomes: a held switch lease
(INTERACTIVE_OPERATION_IN_PROGRESS), a busy outgoing row (BUSY), a torn
authority (CONCURRENT_WRITE) and an unreadable one (INDETERMINATE). None of
them is a verdict on the repair, so none of them counts toward
``RESTAMP_MAX_ATTEMPTS``: the payload stays owed and the one-per-minute
floor spaces the retries. A re-stamp that cannot run at all, because the
holder has no Claude Code token, settles at once and spends neither the
floor nor an attempt.
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
from jacked.credentials.models import SwitchOutcome
from tests.unit.test_authority_guard import (
    _db,
    _foreign_payload,
    _profile,
    _switch_result,
)

LOGGER = "jacked.api.authority_guard"
TRANSIENT = sorted(
    {
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.BUSY,
        SwitchOutcome.CONCURRENT_WRITE,
        SwitchOutcome.INDETERMINATE,
    },
    key=lambda outcome: outcome.value,
)


@pytest.fixture(autouse=True)
def _clean_guard_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


class _Sequence:
    """Return the given outcomes in order; the last outcome repeats."""

    def __init__(self, *outcomes: SwitchOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple] = []
        self.republished: list[tuple] = []

    def __call__(self, db, account, context, operation_id, **repair):
        self.calls.append((account, context, operation_id))
        self.republished.append((repair.get("republish"), repair.get("expected_digest")))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        return _switch_result(outcome, int(account["id"]))


def _restamp_db():
    """Desired account 11 cannot be reasserted, so a foreign write by
    account 3 takes the re-stamp repair."""
    db = _db()
    db.accounts[11]["cc_refresh_token"] = None
    return db


def _heal(db, activate, now, payload=None):
    return heal_foreign_authority(
        db,
        now=now,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: payload or _foreign_payload(),
    )


# --- the settle rule ---------------------------------------------------------


def test_the_transient_class_is_exactly_the_engines_not_now_answers():
    assert authority_guard._TRANSIENT_OUTCOMES == frozenset(TRANSIENT)


@pytest.mark.parametrize("outcome", TRANSIENT, ids=lambda o: o.name)
def test_a_transient_outcome_never_settles_and_never_counts(outcome, caplog):
    digest = "t" * 64
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        settled = [
            authority_guard._settle_after(digest, 3, outcome, row_id=3, repair="re-stamp")
            for _ in range(RESTAMP_MAX_ATTEMPTS + 2)
        ]
    assert settled == [False] * (RESTAMP_MAX_ATTEMPTS + 2)
    assert authority_guard._handled["attempts"] == 0
    assert authority_guard._handled["settled"] is False
    assert "did not commit" not in caplog.text


def test_a_transient_run_does_not_shorten_the_budget_of_a_real_failure():
    digest = "u" * 64
    for _ in range(RESTAMP_MAX_ATTEMPTS):
        assert (
            authority_guard._settle_after(
                digest, 3, SwitchOutcome.BUSY, row_id=3, repair="re-stamp"
            )
            is False
        )
    results = [
        authority_guard._settle_after(
            digest, 3, SwitchOutcome.FAILED_PRESERVED, row_id=3, repair="re-stamp"
        )
        for _ in range(RESTAMP_MAX_ATTEMPTS)
    ]
    assert results == [False] * (RESTAMP_MAX_ATTEMPTS - 1) + [True]


def test_a_long_transient_run_is_reported_once(caplog):
    digest = "w" * 64
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(authority_guard.TRANSIENT_WARN_AFTER + 5):
            assert (
                authority_guard._settle_after(
                    digest, 3, SwitchOutcome.INDETERMINATE, row_id=3, repair="reassert"
                )
                is False
            )
    lines = [r.getMessage() for r in caplog.records if "not now" in r.getMessage()]
    assert len(lines) == 1
    assert f"{authority_guard.TRANSIENT_WARN_AFTER} times in a row" in lines[0]
    assert "reassert" in lines[0] and "Account 3" in lines[0]
    assert authority_guard._handled["attempts"] == 0
    assert authority_guard._handled["settled"] is False


def test_a_real_failure_resets_the_transient_run():
    digest = "x" * 64
    for _ in range(authority_guard.TRANSIENT_WARN_AFTER - 1):
        authority_guard._settle_after(digest, 3, SwitchOutcome.BUSY, row_id=3, repair="re-stamp")
    authority_guard._settle_after(
        digest, 3, SwitchOutcome.FAILED_PRESERVED, row_id=3, repair="re-stamp"
    )
    assert authority_guard._handled["transient"] == 0
    assert authority_guard._handled["attempts"] == 1


def test_a_new_payload_starts_its_own_transient_run(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(authority_guard.TRANSIENT_WARN_AFTER - 1):
            authority_guard._settle_after("y" * 64, 3, SwitchOutcome.BUSY, row_id=3, repair="re-stamp")
        authority_guard._settle_after("z" * 64, 3, SwitchOutcome.BUSY, row_id=3, repair="re-stamp")
    assert authority_guard._handled["transient"] == 1
    assert "not now" not in caplog.text


# --- through the guard: the re-stamp repair ----------------------------------


@pytest.mark.parametrize("outcome", TRANSIENT, ids=lambda o: o.name)
def test_a_busy_restamp_is_retried_past_the_attempt_cap(outcome):
    db = _restamp_db()
    activate = _Sequence(*[outcome] * (RESTAMP_MAX_ATTEMPTS + 1), SwitchOutcome.COMMITTED)
    times = [100.0 + 61.0 * i for i in range(RESTAMP_MAX_ATTEMPTS + 2)]

    actions = [_heal(db, activate, now).action for now in times]

    assert actions == ["skipped"] * (RESTAMP_MAX_ATTEMPTS + 1) + ["adopted"]
    assert len(activate.calls) == RESTAMP_MAX_ATTEMPTS + 2
    assert all(call[0]["id"] == 3 for call in activate.calls)


def test_a_busy_restamp_still_waits_out_the_floor():
    db = _restamp_db()
    activate = _Sequence(SwitchOutcome.BUSY)

    first = _heal(db, activate, now=100.0)
    second = _heal(db, activate, now=130.0)

    assert (first.action, second.action) == ("skipped", "skipped")
    assert second.reason == "reassert rate limit is active"
    assert len(activate.calls) == 1


def test_a_failed_restamp_still_settles_after_the_cap():
    db = _restamp_db()
    activate = _Sequence(SwitchOutcome.FAILED_PRESERVED)
    times = [100.0 + 61.0 * i for i in range(RESTAMP_MAX_ATTEMPTS + 1)]

    results = [_heal(db, activate, now) for now in times]

    assert [r.action for r in results] == ["skipped"] * (RESTAMP_MAX_ATTEMPTS + 1)
    assert results[-1].reason == "foreign payload already handled"
    assert len(activate.calls) == RESTAMP_MAX_ATTEMPTS


# --- a re-stamp with nothing to publish --------------------------------------


def _primary_only_holder_db():
    """Account 3 has only a primary lineage, and the authority holds jacked's
    primary-fallback publication of it: the holder has no Claude Code token
    to re-stamp the authority with."""
    db = _restamp_db()
    db.accounts[3].update(
        {"cc_access_token": None, "cc_refresh_token": None, "access_token": "primary_at_3"}
    )
    return db


def test_a_holder_without_a_claude_code_token_is_restamped_by_republishing(caplog):
    """The holder's row has no Claude Code token, and the authority holds
    jacked's own primary-fallback publication of it, unstamped. A re-stamp
    republishes exactly that payload with the holder's stamp (#154i rule):
    nothing to strip, nothing to lose, and the stamp becomes right."""
    db = _primary_only_holder_db()
    activate = _Sequence(SwitchOutcome.COMMITTED)
    payload = _foreign_payload(access="primary_at_3", refresh=None)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _heal(db, activate, now=100.0, payload=payload)

    assert result.action == "adopted"
    assert (result.foreign_account_id, result.desired_account_id) == (3, 11)
    assert [call[0]["id"] for call in activate.calls] == [3]
    republished, digest = activate.republished[0]
    assert republished["claudeAiOauth"] == payload["claudeAiOauth"]
    assert digest  # the engine must find this exact payload in the authority
    assert authority_guard._last_reassert_at == 100.0  # a real repair spends the floor
    assert "holds no Claude Code token" not in caplog.text
    assert "primary_at_3" not in caplog.text
    assert "carol@test.com" not in caplog.text


def test_a_holder_without_a_claude_code_token_is_handled_once():
    db = _primary_only_holder_db()
    activate = _Sequence(SwitchOutcome.COMMITTED)
    payload = _foreign_payload(access="primary_at_3", refresh=None)

    _heal(db, activate, now=100.0, payload=payload)
    again = _heal(db, activate, now=101.0, payload=payload)

    assert again.reason == "foreign payload already handled"
    assert len(activate.calls) == 1
    # Another payload is not held back by that settled one (after the floor).
    db.accounts[3].update({"cc_access_token": "live_at_3", "cc_refresh_token": "live_rt_3"})
    later = _heal(db, activate, now=161.0)
    assert later.action == "adopted"
    assert len(activate.calls) == 2
