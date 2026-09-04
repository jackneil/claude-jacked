"""Refusal-only compatibility evidence for pre-v2 jacked services."""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyPidEvidence:
    pid: int
    port: int
    alive: bool


def inspect_legacy_pid(path: Path) -> LegacyPidEvidence | None:
    """Read legacy evidence without changing it or treating it as authority."""

    from jacked.service.process import is_process_alive, read_pid

    try:
        info = read_pid(path)
        if info is None:
            return None
        return LegacyPidEvidence(
            pid=info["pid"],
            port=info["port"],
            alive=is_process_alive(info["pid"]),
        )
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("Legacy PID inspection failed: %s", type(exc).__name__)
        return None


def probe_legacy_health(host: str, port: int, timeout: float = 0.75) -> bool:
    """Recognize jacked's stable health response without granting control."""

    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/health", timeout=timeout
        ) as response:
            if getattr(response, "status", 200) != 200:
                return False
            body = response.read(4_097)
        if len(body) > 4_096:
            return False
        payload = json.loads(body)
        return (
            isinstance(payload, dict)
            and set(payload) == {"status", "db"}
            and payload["status"] == "ok"
            and isinstance(payload["db"], bool)
        )
    except Exception as exc:
        logger.debug("Legacy health probe failed: %s", type(exc).__name__)
        return False


def resolve_active_legacy_service(
    path: Path, host: str = "127.0.0.1"
) -> LegacyPidEvidence | None:
    """Return legacy evidence only when its live PID and API corroborate it.

    An untagged PID file alone is never enough: the PID can be stale and later
    reused by an unrelated process. The health fingerprint is refusal-only
    evidence and never authorizes signalling that process.
    """

    from jacked.service.process import is_v2_pid_evidence

    if is_v2_pid_evidence(path):
        return None
    evidence = inspect_legacy_pid(path)
    if (
        evidence is None
        or not evidence.alive
        or not probe_legacy_health(host, evidence.port)
    ):
        return None
    return evidence
