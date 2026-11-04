"""Settle credential switches an earlier process left pending.

A switch is journaled before its stores are written; a crash or an upgrade's
exec between the two leaves the row in phase ``pending``. At service start
this observes the authority and classifies each such row (committed,
failed_preserved or indeterminate) without rewriting any credential, once:
a row classified indeterminate is recorded as such and is not observed
again on later starts. It never raises; a refusal or an error is logged and
the rows stay for the next start.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jacked.web.credential_repository import DatabaseCredentialSwitchRepository

from . import runtime
from .recovery import CredentialRecovery
from .transaction import TransactionDependencies

logger = logging.getLogger(__name__)


def _prepare(db, pending_count: int) -> TransactionDependencies | None:
    """Resolve the Claude install and assemble the stores a sweep observes.

    Returns None, after one WARNING, when this install cannot be classified
    (an unknown build, an unfenced capability without a key, a missing
    recovery key); the rows then wait for a later start.
    """
    home = Path.home()
    identity = runtime.detect_claude_identity(home)
    resolution = runtime.SHIPPED_REGISTRY.resolve(identity)
    if not resolution.can_mutate:
        logger.warning(
            "Pending credential switch recovery skipped (%d row(s)): %s",
            pending_count,
            resolution.reason,
        )
        return None
    dependencies = runtime._dependencies_for(db, resolution, home)
    if isinstance(dependencies, str):
        logger.warning(
            "Pending credential switch recovery skipped (%d row(s)): %s",
            pending_count,
            dependencies,
        )
        return None
    return dependencies


def _sweep(repository, dependencies: TransactionDependencies, pending) -> list[tuple[str, str]]:
    """Classify each pending row under the switch lease; returns what settled."""
    settled: list[tuple[str, str]] = []
    with dependencies.switch_lease.acquire() as held:
        if not held:
            logger.info("Pending credential switch recovery skipped: a switch is in progress")
            return settled
        recovery = CredentialRecovery(
            repository,
            dependencies.authority,
            dependencies.install_key,
            dependencies.machine_install_id,
            dependencies.identity_publisher,
        )
        for record in pending:
            try:
                result = recovery.recover(record.operation_id)
            except Exception as exc:  # noqa: BLE001 - one row must not stop the rest
                logger.warning(
                    "Settling credential switch %r failed: %s",
                    record.operation_id,
                    type(exc).__name__,
                )
                continue
            logger.info(
                "Settled credential switch %r: %s", record.operation_id, result.outcome.value
            )
            settled.append((record.operation_id, result.outcome.value))
    return settled


def recover_pending_switches(db) -> list[tuple[str, str]]:
    """Settle every switch left in phase ``pending``; ``(operation_id, outcome)`` per row.

    Returns ``[]`` at once when nothing is pending, without probing the
    Claude install. Never raises: every failure is logged by its type name
    and the rows stay for the next start.
    """
    repository = DatabaseCredentialSwitchRepository(db)
    try:
        pending = repository.list_interrupted()
    except Exception as exc:  # noqa: BLE001 - startup must continue
        logger.warning(
            "Pending credential switch recovery skipped: %s while listing",
            type(exc).__name__,
        )
        return []
    if not pending:
        return []
    settled: list[tuple[str, str]] = []
    try:
        dependencies = _prepare(db, len(pending))
        if dependencies is None:
            return settled
        settled = _sweep(repository, dependencies, pending)
    except Exception as exc:  # noqa: BLE001 - startup must continue
        logger.warning(
            "Pending credential switch recovery stopped after %d row(s): %s",
            len(settled),
            type(exc).__name__,
        )
    return settled
