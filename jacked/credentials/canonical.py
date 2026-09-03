"""Strict, deterministic credential parsing and digesting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import CredentialIdentity

CANONICALIZER_VERSION = 1


class CredentialFormatError(ValueError):
    """Credential input cannot be represented unambiguously."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CredentialFormatError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_strict_json(raw: bytes | str) -> dict[str, Any]:
    """Parse one JSON object while rejecting duplicate keys."""

    def reject_constant(value: str) -> None:
        raise CredentialFormatError(f"non-finite JSON number: {value}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CredentialFormatError("invalid credential JSON") from exc
    if not isinstance(value, dict):
        raise CredentialFormatError("credential JSON must be an object")
    return value


def canonical_bytes(value: Mapping[str, Any], *, version: int = 1) -> bytes:
    """Return the exact canonical byte representation for a known version."""
    if version != CANONICALIZER_VERSION:
        raise CredentialFormatError(f"unknown canonicalizer version: {version}")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CredentialFormatError("credential value is not canonical JSON") from exc
    return encoded.encode("utf-8")


@dataclass(frozen=True)
class CredentialPayload:
    """Secret credential material whose repr never includes its contents."""

    _value: dict[str, Any] = field(repr=False)
    canonicalizer_version: int = CANONICALIZER_VERSION

    @classmethod
    def from_json(cls, raw: bytes | str, *, version: int = 1) -> CredentialPayload:
        return cls(parse_strict_json(raw), version)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, version: int = 1
    ) -> CredentialPayload:
        # Canonical round trip prevents callers from retaining mutable aliases.
        return cls.from_json(canonical_bytes(value, version=version), version=version)

    @property
    def identity(self) -> CredentialIdentity:
        account_id = self._value.get("_jackedAccountId")
        if (
            not isinstance(account_id, int)
            or isinstance(account_id, bool)
            or account_id <= 0
        ):
            account_id = None
        oauth = self._value.get("claudeAiOauth")
        organization_id = (
            oauth.get("organizationUuid") if isinstance(oauth, dict) else None
        )
        return CredentialIdentity(
            account_id=account_id, organization_id=organization_id
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def to_bytes(self) -> bytes:
        return canonical_bytes(self._value, version=self.canonicalizer_version)

    def to_mapping(self) -> dict[str, Any]:
        return parse_strict_json(self.to_bytes())

    def __repr__(self) -> str:
        return (
            "CredentialPayload(identity="
            f"{self.identity!r}, canonicalizer_version={self.canonicalizer_version})"
        )
