"""Resolver-snapshot account facts for the statusline."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from jacked.resolver_snapshot import (
    SNAPSHOT_FILENAME,
    read_resolver_snapshot,
    snapshot_identity,
)
from jacked.statusline_common import MIDDOT

EMPTY_ACCOUNT_FACTS = {
    "segment": "",
    "email": "",
    "org_uuid": "",
    "state": "missing",
}


def snapshot_path(home: str) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (
        Path(configured) if configured else Path(home) / ".claude"
    ) / SNAPSHOT_FILENAME


def _scoped_launch_certified(snapshot: dict) -> bool:
    """Require an exact launch/revision binding before trusting scoped identity."""
    if os.environ.get("JACKED_SCOPED_CREDENTIAL_CERTIFIED") != "1":
        return False
    launch_nonce = os.environ.get("JACKED_LAUNCH_NONCE")
    env_revision = os.environ.get("JACKED_CREDENTIAL_REVISION")
    snapshot_revision = snapshot.get("credential_revision")
    if not launch_nonce or not env_revision or env_revision != snapshot_revision:
        return False
    evidence = snapshot.get("evidence")
    return isinstance(evidence, list) and f"launch_binding:{launch_nonce}" in evidence


def _desired_default_conflict(snapshot: dict, observed: dict | None) -> bool:
    """True when the stores agree on an identity that is not the desired default.

    An organization conflict also reports ``conflict`` but its observed
    identity is known to be wrong, so it must keep the unknown rendering.
    """
    evidence = snapshot.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    return (
        snapshot.get("state") == "conflict"
        and observed is not None
        and "desired-default:conflict" in evidence
        and "account-metadata:organization-conflict" not in evidence
    )


def _observed_with_desired(observed: dict, desired_label: str) -> dict:
    """Facts naming what the runtime will actually use; the conflict is not hidden."""
    return {
        "segment": f"{observed['email']} {MIDDOT} desired {desired_label}",
        "email": observed["email"],
        "org_uuid": observed["organization_id"],
        "state": "credential conflict",
    }


def _service_is_discoverable(home: str) -> bool:
    """True when the owned service leaves discoverable evidence under ``home``.

    The statusline must never invent a failure, so an unreadable or unexpected
    discovery result reads as discoverable. Paths are derived from the given
    home, which keeps this read hermetic and matches where the service writes.
    """
    try:
        from jacked.service.instance_models import ServicePaths
        from jacked.service.lifecycle import discover_service

        claude_dir = Path(home) / ".claude"
        paths = ServicePaths.in_directory(claude_dir / "jacked-service-v2")
        paths = ServicePaths(
            root=paths.root,
            lease=paths.lease,
            manifest=paths.manifest,
            control=paths.control,
            legacy_pid=claude_dir / "jacked-service.pid",
        )
        source = discover_service(paths).source
        if source == "manifest":
            from jacked.service.instance import manifest_is_proven_stale

            # A crashed service leaves a valid manifest behind; only a live,
            # identity-matched PID counts as the service being up.
            return not manifest_is_proven_stale(paths.manifest)
        return source == "legacy"
    except Exception:  # noqa: BLE001 - a broken probe must never claim a failure
        return True


def account_facts(
    home: str,
    now: float,
    *,
    json_load=json.load,
    service_discoverable: Callable[[], bool] | None = None,
) -> dict:
    snapshot = read_resolver_snapshot(snapshot_path(home), json_load=json_load)
    if snapshot is None:
        return dict(EMPTY_ACCOUNT_FACTS)
    desired = snapshot_identity(snapshot.get("desired"))
    observed = snapshot_identity(snapshot.get("observed"))
    published_at = snapshot.get("published_at")
    fresh_until = snapshot.get("fresh_until")
    valid_clock = (
        not isinstance(published_at, bool)
        and isinstance(published_at, (int, float))
        and not isinstance(fresh_until, bool)
        and isinstance(fresh_until, (int, float))
        and published_at <= now + 300
        and fresh_until >= now
    )
    state = snapshot.get("state")
    scoped_unverified = snapshot.get("scope") == "scoped" and not (
        _scoped_launch_certified(snapshot)
    )
    facts = dict(EMPTY_ACCOUNT_FACTS)
    if (
        state == "resolved"
        and valid_clock
        and observed is not None
        and not scoped_unverified
    ):
        facts.update(
            segment=observed["email"],
            email=observed["email"],
            org_uuid=observed["organization_id"],
            state="resolved",
        )
        return facts
    desired_label = desired["email"] if desired is not None else "account"
    if valid_clock and not scoped_unverified and _desired_default_conflict(snapshot, observed):
        return {**facts, **_observed_with_desired(observed, desired_label)}
    if scoped_unverified:
        reason = "scoped unverified"
    elif not valid_clock or state == "stale":
        # A stale snapshot has one common cause: the service that refreshes it
        # is not running. Name that cause instead of the symptom.
        probe = service_discoverable or (lambda: _service_is_discoverable(home))
        if not probe():
            facts["segment"] = (
                f"desired {desired_label} {MIDDOT} jacked service down"
            )
            facts["state"] = "service down"
            return facts
        reason = "stale"
    elif state == "conflict":
        reason = "credential conflict"
    elif state in {"missing", "unusable"}:
        reason = state
    else:
        reason = "unknown"
    facts["segment"] = f"desired {desired_label} {MIDDOT} runtime unknown ({reason})"
    facts["state"] = reason
    return facts
