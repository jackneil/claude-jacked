"""Owned native-supervisor artifact models and reconciliation."""

from __future__ import annotations

import json
import os
import plistlib
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jacked.service.spec import SupervisorKind
from jacked.service.supervisors._helpers import atomic_write


class ArtifactDisposition(str, Enum):
    MISSING = "missing"
    MATCHING = "matching"
    OWNED_DRIFT = "owned_drift"
    FOREIGN = "foreign"
    INSTALLED = "installed"


@dataclass(frozen=True)
class SupervisorAction:
    ok: bool
    action: str
    reason: str


@dataclass(frozen=True)
class SupervisorArtifact:
    kind: SupervisorKind
    name: str
    marker: dict[str, str | int]
    content: bytes


@dataclass(frozen=True)
class ArtifactInspection:
    disposition: ArtifactDisposition
    reason: str = ""


def inspect_artifact(path: Path, expected: SupervisorArtifact) -> ArtifactInspection:
    if not path.exists() and not path.is_symlink():
        return ArtifactInspection(ArtifactDisposition.MISSING)
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return ArtifactInspection(
                ArtifactDisposition.FOREIGN,
                "artifact is not a regular single-link file",
            )
        if os.name == "posix" and (
            status.st_uid != os.getuid() or status.st_mode & 0o022
        ):
            return ArtifactInspection(
                ArtifactDisposition.FOREIGN,
                "artifact is not privately controlled by the current user",
            )
        current = path.read_bytes()
    except OSError as exc:
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN,
            f"artifact is unreadable: {type(exc).__name__}",
        )
    if current == expected.content:
        return ArtifactInspection(ArtifactDisposition.MATCHING)
    marker = extract_marker(current, expected.kind)
    if marker is None:
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN, "artifact has no valid jacked ownership marker"
        )
    if (
        marker.get("owner") != expected.marker["owner"]
        or marker.get("service_id") != expected.marker["service_id"]
    ):
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN, "artifact owner or service identity differs"
        )
    return ArtifactInspection(
        ArtifactDisposition.OWNED_DRIFT, "owned artifact generation differs"
    )


def reconcile_artifact(path: Path, expected: SupervisorArtifact) -> ArtifactInspection:
    inspection = inspect_artifact(path, expected)
    if inspection.disposition in {
        ArtifactDisposition.MATCHING,
        ArtifactDisposition.FOREIGN,
    }:
        return inspection
    atomic_write(path, expected.content)
    return ArtifactInspection(
        ArtifactDisposition.INSTALLED, "artifact installed from exact ServiceSpec"
    )


def snapshot_artifact(
    path: Path, expected: SupervisorArtifact
) -> tuple[ArtifactInspection, bytes | None]:
    """Inspect an artifact and retain exact owned bytes for rollback."""

    inspection = inspect_artifact(path, expected)
    previous = (
        path.read_bytes()
        if inspection.disposition
        in {ArtifactDisposition.MATCHING, ArtifactDisposition.OWNED_DRIFT}
        else None
    )
    return inspection, previous


def restore_artifact(
    path: Path, expected: SupervisorArtifact, previous: bytes | None
) -> bool:
    """Restore only while the path still contains this transition's bytes."""

    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        return False
    if current == previous:
        return True
    if current != expected.content:
        return False
    try:
        if previous is None:
            path.unlink()
        else:
            atomic_write(path, previous)
    except OSError:
        return False
    try:
        restored = path.read_bytes()
    except FileNotFoundError:
        restored = None
    except OSError:
        return False
    return restored == previous


def extract_marker(content: bytes, kind: SupervisorKind) -> dict[str, Any] | None:
    try:
        if kind is SupervisorKind.LAUNCHD:
            payload = plistlib.loads(content)
            return {
                "owner": payload["JackedOwner"],
                "service_id": payload["JackedServiceID"],
                "schema_version": payload["JackedSchemaVersion"],
                "protocol_version": payload["JackedProtocolVersion"],
                "generation": payload["JackedGeneration"],
            }
        if kind is SupervisorKind.SYSTEMD_USER:
            return _extract_systemd_marker(content)
        return _extract_task_marker(content)
    except (
        KeyError,
        ValueError,
        TypeError,
        plistlib.InvalidFileException,
        ElementTree.ParseError,
        json.JSONDecodeError,
    ):
        return None


def _extract_systemd_marker(content: bytes) -> dict[str, str | int] | None:
    values: dict[str, str | int] = {}
    names = {
        "X-Jacked-Owner": "owner",
        "X-Jacked-Service-ID": "service_id",
        "X-Jacked-Schema-Version": "schema_version",
        "X-Jacked-Protocol-Version": "protocol_version",
        "X-Jacked-Generation": "generation",
    }
    for line in content.decode("utf-8").splitlines():
        if not line.startswith("# ") or ": " not in line:
            continue
        key, value = line[2:].split(": ", 1)
        if key in names:
            values[names[key]] = int(value) if names[key].endswith("version") else value
    return values if set(values) == set(names.values()) else None


def _extract_task_marker(content: bytes) -> dict[str, Any] | None:
    root = ElementTree.fromstring(content)
    description = next(
        (
            item.text or ""
            for item in root.iter()
            if item.tag.rsplit("}", 1)[-1] == "Description"
        ),
        "",
    )
    prefix = "JACKED-MARKER:"
    return json.loads(description[len(prefix) :]) if description.startswith(prefix) else None
