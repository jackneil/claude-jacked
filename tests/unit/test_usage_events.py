"""Tests for the in-process usage-change signal (jacked.usage_events).

The menu-bar pill watches usage_events.version() on a 1s timer; the write
paths must bump it so a dashboard-triggered refresh or an account switch
shows up in the tray within a second instead of the 30s poll heartbeat.
"""

import threading

import pytest

from jacked import usage_events
from jacked.credentials.models import (
    CapabilityMode,
    CredentialIdentity,
    FinalizeSwitchRecord,
    OutcomeSwitchRecord,
    PendingSwitchRecord,
    SwitchContext,
    SwitchOutcome,
)
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository
from jacked.web.database import Database


class TestUsageEventsPrimitive:
    def test_bump_is_monotonic(self):
        before = usage_events.version()
        assert usage_events.bump() == before + 1
        assert usage_events.version() == before + 1

    def test_bump_returns_new_version(self):
        v1 = usage_events.bump()
        v2 = usage_events.bump()
        assert v2 == v1 + 1

    def test_concurrent_bumps_never_lose_a_count(self):
        before = usage_events.version()
        n_threads, bumps_each = 8, 250

        def hammer():
            for _ in range(bumps_each):
                usage_events.bump()

        threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert usage_events.version() == before + n_threads * bumps_each


class TestWritePathsBump:
    def test_usage_cache_write_bumps(self):
        db = Database(":memory:")
        acct = db.create_account("u@t.com", "tok", 9999999999)
        before = usage_events.version()
        assert db.update_account_usage_cache(acct["id"], five_hour=42.0)
        assert usage_events.version() == before + 1

    def test_failed_usage_cache_write_does_not_bump(self):
        db = Database(":memory:")
        before = usage_events.version()
        assert not db.update_account_usage_cache(999999, five_hour=42.0)
        assert usage_events.version() == before


def _repository(tmp_path) -> DatabaseCredentialSwitchRepository:
    return DatabaseCredentialSwitchRepository(Database(str(tmp_path / "jacked.db")))


def _pending(operation_id: str) -> PendingSwitchRecord:
    return PendingSwitchRecord(
        operation_id=operation_id,
        account_id=7,
        organization_id="org-1",
        context=SwitchContext.MANUAL,
        capability_mode=CapabilityMode.GLOBAL_COOPERATIVE,
        machine_install_id="machine-1",
        backend_locator="file:credentials",
        capability_epoch=2,
        canonicalizer_version=1,
        before_hmac="before",
        target_hmac="target",
    )


def _finalize(operation_id: str) -> FinalizeSwitchRecord:
    return FinalizeSwitchRecord(
        operation_id=operation_id,
        account_id=7,
        outcome=SwitchOutcome.COMMITTED,
        observed_identity=CredentialIdentity(7, "person@example.com", "org-1"),
        credential_revision="revision-1",
    )


def _outcome(operation_id: str, outcome: SwitchOutcome) -> OutcomeSwitchRecord:
    return OutcomeSwitchRecord(
        operation_id=operation_id,
        account_id=5,
        outcome=outcome,
        observed_identity=CredentialIdentity(5, "target@example.com", None),
        message="test outcome",
    )


class TestSwitchRepositoryBumps:
    """A switch that moves an account pointer wakes the pill exactly once."""

    def test_finalize_bumps_once(self, tmp_path):
        repository = _repository(tmp_path)
        repository.create_pending(_pending("op-final"))
        before = usage_events.version()

        repository.finalize(_finalize("op-final"))

        assert usage_events.version() == before + 1

    def test_refused_finalize_does_not_bump(self, tmp_path):
        # The bump comes after the transaction commits. A finalize that
        # raises (no pending operation) changed nothing, so it wakes nobody.
        repository = _repository(tmp_path)
        before = usage_events.version()

        with pytest.raises(ValueError, match="not pending"):
            repository.finalize(_finalize("op-unknown"))

        assert usage_events.version() == before

    def test_unfenced_outcome_bumps_once(self, tmp_path):
        repository = _repository(tmp_path)
        before = usage_events.version()

        repository.record_outcome(
            _outcome("op-observed", SwitchOutcome.OBSERVED_TARGET_UNFENCED)
        )

        assert usage_events.version() == before + 1

    @pytest.mark.parametrize(
        "outcome",
        [o for o in SwitchOutcome if o is not SwitchOutcome.OBSERVED_TARGET_UNFENCED],
        ids=lambda o: o.value,
    )
    def test_other_outcomes_do_not_bump(self, tmp_path, outcome):
        # Only the unfenced observation moves the desired pointer, so every
        # other recorded outcome (a failed or refused switch) wakes nobody.
        repository = _repository(tmp_path)
        before = usage_events.version()

        repository.record_outcome(_outcome("op-other", outcome))

        assert usage_events.version() == before
