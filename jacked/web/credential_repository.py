"""SQLite adapter for the crash-safe credential transaction engine."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from jacked.credentials.models import (
    CapabilityMode,
    FinalizeSwitchRecord,
    OutcomeSwitchRecord,
    PendingSwitchRecord,
    SwitchContext,
    SwitchOutcome,
)

logger = logging.getLogger(__name__)


def _wake_usage_watchers() -> None:
    """Bump ``jacked.usage_events`` so same-process pill watchers re-render.

    Every switch outcome that moves the active or desired account pointer
    calls this once, after its DB transaction committed. The counter is the
    only wake channel the menu-bar agent has for account switches (usage
    cache writes bump it from ``Database.update_account_usage_cache``).

    >>> from jacked import usage_events
    >>> before = usage_events.version()
    >>> _wake_usage_watchers()
    >>> usage_events.version() == before + 1
    True
    """
    from jacked import usage_events

    usage_events.bump()


class DatabaseCredentialSwitchRepository:
    """Persist switch state without ever serializing credential payloads."""

    def __init__(self, db) -> None:
        self.db = db

    def create_pending(self, record: PendingSwitchRecord) -> None:
        from jacked.web.database import selected_account_id

        self.db.create_credential_switch(
            {
                "operation_id": record.operation_id,
                "account_id": record.account_id,
                # The audit names the account this switch leaves: the one
                # jacked selected for new sessions, if its row still exists.
                "previous_account_id": selected_account_id(self.db, existing_only=True),
                "organization_id": record.organization_id,
                "machine_install_id": record.machine_install_id,
                "context": record.context.value,
                "capability_mode": record.capability_mode.value,
                "capability_epoch": str(record.capability_epoch),
                "backend_locator": record.backend_locator,
                "canonicalizer_version": record.canonicalizer_version,
                "before_hmac": record.before_hmac,
                "target_hmac": record.target_hmac,
                # Non-secret identity, kept so recovery can republish it to
                # Claude's config after a crash.
                "email": record.email,
                "display_name": record.display_name,
                "organization_name": record.organization_name,
                "phase": "pending",
            }
        )

    def finalize(self, record: FinalizeSwitchRecord) -> None:
        """Publish journal, committed pointer, and audits in one DB transaction."""
        from jacked.web.database import SELECTED_ACCOUNT_KEYS

        now = datetime.now(timezone.utc).isoformat()
        with self.db._writer() as conn:
            pending = conn.execute(
                "SELECT * FROM credential_switches WHERE operation_id = ?",
                (record.operation_id,),
            ).fetchone()
            if pending is None:
                raise ValueError("credential switch operation is not pending")
            cursor = conn.execute(
                """UPDATE credential_switches
                   SET phase = 'committed', outcome = ?, observed_account_id = ?,
                       observed_at = ?, detail_json = ?, updated_at = ?
                   WHERE operation_id = ?
                     AND phase IN ('pending', 'indeterminate')""",
                (
                    record.outcome.value,
                    record.observed_identity.account_id,
                    now,
                    json.dumps(
                        {"credential_revision": record.credential_revision},
                        sort_keys=True,
                    ),
                    now,
                    record.operation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("credential switch operation is already terminal")
            # A committed switch moves both pointers together.
            for key in SELECTED_ACCOUNT_KEYS:
                conn.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    (key, str(record.account_id), now),
                )
            conn.execute(
                """INSERT INTO swap_log
                   (from_account_id, to_account_id, reason, trigger, status)
                   VALUES (?, ?, ?, ?, 'committed')""",
                (
                    pending["previous_account_id"],
                    record.account_id,
                    f"credential transaction {record.operation_id}",
                    pending["context"],
                ),
            )
            conn.execute(
                """INSERT INTO decision_log
                   (account_id, action, trigger, target_id, reason, detail)
                   VALUES (?, 'credential_switch_committed', ?, ?, ?, ?)""",
                (
                    record.account_id,
                    pending["context"],
                    record.account_id,
                    record.outcome.value,
                    json.dumps(
                        {
                            "operation_id": record.operation_id,
                            "credential_revision": record.credential_revision,
                        },
                        sort_keys=True,
                    ),
                ),
            )
        # The active account changed: wake the in-process menu-bar watcher
        # (jacked.usage_events) after the transaction commits, so the pill
        # re-renders within a second instead of waiting out its poll.
        _wake_usage_watchers()

    def record_outcome(self, record: OutcomeSwitchRecord) -> None:
        now = datetime.now(timezone.utc).isoformat()
        terminal = record.outcome is not SwitchOutcome.INDETERMINATE
        phase = (
            "observed_only"
            if record.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
            else "failed" if terminal else "indeterminate"
        )
        with self.db._writer() as conn:
            existing = conn.execute(
                "SELECT operation_id FROM credential_switches WHERE operation_id = ?",
                (record.operation_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE credential_switches
                       SET phase = ?, outcome = ?, observed_account_id = ?,
                           observed_at = ?, detail_json = ?, updated_at = ?
                       WHERE operation_id = ?""",
                    (
                        phase,
                        record.outcome.value,
                        record.observed_identity.account_id,
                        now,
                        json.dumps({"message": record.message}, sort_keys=True),
                        now,
                        record.operation_id,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO credential_switches
                       (operation_id, account_id, context, capability_mode,
                        capability_epoch, backend_locator, canonicalizer_version,
                        target_hmac, phase, outcome, observed_account_id,
                        observed_at, detail_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'unknown',
                               'observed-authority', 1, '', ?, ?, ?,
                               ?, ?, ?, ?)""",
                    (
                        record.operation_id,
                        record.account_id,
                        record.context.value,
                        record.capability_mode.value,
                        phase,
                        record.outcome.value,
                        record.observed_identity.account_id,
                        now,
                        json.dumps({"message": record.message}, sort_keys=True),
                        now,
                        now,
                    ),
                )
            if record.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED:
                conn.execute(
                    """INSERT INTO settings (key, value, updated_at)
                       VALUES ('desired_account_id', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    (str(record.account_id), now),
                )
            conn.execute(
                """INSERT INTO decision_log
                   (account_id, action, trigger, target_id, reason, detail)
                   VALUES (?, 'credential_switch_outcome', 'transaction', ?, ?, ?)""",
                (
                    record.account_id,
                    record.account_id,
                    record.outcome.value,
                    json.dumps(
                        {"operation_id": record.operation_id, "message": record.message},
                        sort_keys=True,
                    ),
                ),
            )
        if record.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED:
            # The desired account moved (the outcome every macOS build
            # produces): the pill tracks it, so wake the watcher here too.
            _wake_usage_watchers()

    @staticmethod
    def _pending_from_row(row: dict) -> PendingSwitchRecord:
        try:
            context = SwitchContext(row["context"])
        except ValueError:
            context = SwitchContext.MANUAL
        try:
            capability_mode = CapabilityMode(row["capability_mode"])
        except (KeyError, ValueError):
            capability_mode = CapabilityMode.UNSUPPORTED
        return PendingSwitchRecord(
            operation_id=row["operation_id"],
            account_id=row["account_id"],
            organization_id=row.get("organization_id"),
            context=context,
            capability_mode=capability_mode,
            machine_install_id=row.get("machine_install_id") or "local-install",
            backend_locator=row["backend_locator"],
            capability_epoch=int(row["capability_epoch"]),
            canonicalizer_version=row["canonicalizer_version"],
            before_hmac=row.get("before_hmac") or "",
            target_hmac=row["target_hmac"],
            # Rows written before these columns existed carry no identity;
            # recovery then finalizes without republishing it.
            email=row.get("email"),
            display_name=row.get("display_name"),
            organization_name=row.get("organization_name"),
        )

    def get_pending(self, operation_id: str) -> PendingSwitchRecord | None:
        row = self.db.get_credential_switch(operation_id)
        if not row or row["phase"] not in {"pending", "indeterminate"}:
            return None
        return self._pending_from_row(row)

    def list_pending(self) -> tuple[PendingSwitchRecord, ...]:
        return tuple(
            self._pending_from_row(row)
            for row in self.db.list_pending_credential_switches()
        )

    def count_interrupted(self) -> int:
        """How many rows are in phase ``pending``, parsed or not.

        This is the cheap first look of the startup sweep. It logs nothing:
        ``list_interrupted`` reports a malformed row, once, under the lease.
        """
        return sum(
            row.get("phase") == "pending"
            for row in self.db.list_pending_credential_switches()
        )

    def list_interrupted(self) -> tuple[PendingSwitchRecord, ...]:
        """Rows an interrupted switch left in phase ``pending``, and only those.

        ``list_pending`` also returns ``indeterminate`` rows, which recovery
        already observed once and could not classify (a shipped build has no
        verifier key, so such a row can never change). The startup sweep uses
        this narrower list, so one crash does not add a decision-log row on
        every later service start.
        """
        records = []
        for row in self.db.list_pending_credential_switches():
            if row.get("phase") != "pending":
                continue
            try:
                records.append(self._pending_from_row(row))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                # One corrupt row must not hide every other interrupted switch.
                logger.warning(
                    "Skipping malformed credential switch row %r: %s",
                    row.get("operation_id"),
                    type(exc).__name__,
                )
        return tuple(records)
