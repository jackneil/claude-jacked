"""Load + validate the vendored agent-reach pin file (the supply-chain trust anchor).

The pin file (``jacked/data/integrations/agent-reach.json``) is the immutable
record of exactly which upstream commit, transitive constraints, skill-file
hashes, and channel-backend versions jacked will install. Everything installable
is pinned here; nothing resolves at install time (threat model V1-V3). This
module turns that JSON into a validated :class:`PinFile` and rejects anything
that would weaken the pin (a tag masquerading as a SHA, an unpinned channel
spec, an empty hash set).

The vetting pipeline (``scripts/vet_agent_reach.py``) produces the JSON; this
module is the consumer that the runner and API trust.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: Default location of the vendored pin file (shipped inside the wheel under
#: ``jacked/data/integrations/``). Overridable for tests.
DEFAULT_PIN_PATH = Path(__file__).parent.parent / "data" / "integrations" / "agent-reach.json"

#: Schema versions this loader understands. Bump when the JSON shape changes.
KNOWN_SCHEMA_VERSIONS = frozenset({1})

#: Channel-backend kinds jacked knows how to install.
VALID_BACKEND_KINDS = frozenset({"npm", "pipx", "pipx-git"})

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
# An EXACT semver: major.minor.patch with an optional -prerelease / +build tail.
# fullmatch-only (see _spec_is_exactly_pinned) so a range/partial/`||` spec such as
# `1.2`, `1.2.x`, or `1.2.3 || 2.0.0` is rejected: the whole V3 posture is that a
# channel backend pins to ONE resolvable version, never a floating range.
_EXACT_SEMVER = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:\+[0-9A-Za-z][0-9A-Za-z.-]*)?"
)


class PinFileError(ValueError):
    """Raised when the pin file is missing, unreadable, or fails validation."""


@dataclass(frozen=True)
class ChannelBackend:
    """One backend for a channel: either an installable spec or a manual note.

    An installable backend carries a ``kind`` (npm/pipx/pipx-git) plus an exactly
    pinned ``spec``. A note-only backend (``kind`` is None) records something the
    user must do by hand, e.g. a Chrome extension Chrome forbids auto-installing.
    """

    kind: str | None
    spec: str | None
    note: str | None = None

    @property
    def is_installable(self) -> bool:
        return self.kind is not None


@dataclass(frozen=True)
class Channel:
    name: str
    backends: tuple[ChannelBackend, ...]

    def installable_backends(self) -> list[ChannelBackend]:
        return [b for b in self.backends if b.is_installable]


@dataclass(frozen=True)
class PinFile:
    schema_version: int
    upstream: str
    commit_sha: str
    version_label: str
    vetted_at: str
    constraints_file: str
    skill_hashes: dict[str, str]
    channels: dict[str, Channel]
    source_path: Path

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:12]

    def constraints_path(self) -> Path:
        """Absolute path to the constraints file, resolved next to the pin JSON."""
        return self.source_path.parent / self.constraints_file


def load_pin(path: Path | str | None = None) -> PinFile:
    """Read + validate the pin file. Raise :class:`PinFileError` on any problem."""
    resolved = Path(path) if path is not None else DEFAULT_PIN_PATH
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise PinFileError(f"pin file not found: {resolved}") from e
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise PinFileError(f"pin file unreadable: {resolved}: {e}") from e
    if not isinstance(raw, dict):
        raise PinFileError(f"pin file is not a JSON object: {resolved}")
    return _validate(raw, resolved)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def _validate(raw: dict, source_path: Path) -> PinFile:
    schema_version = raw.get("schema_version")
    if schema_version not in KNOWN_SCHEMA_VERSIONS:
        raise PinFileError(
            f"unknown schema_version {schema_version!r} "
            f"(this jacked understands {sorted(KNOWN_SCHEMA_VERSIONS)})"
        )

    commit_sha = raw.get("commit_sha")
    if not isinstance(commit_sha, str) or not _HEX40.fullmatch(commit_sha):
        raise PinFileError(
            "commit_sha must be a full 40-char hex SHA (never a tag or branch); "
            f"got {commit_sha!r}"
        )

    upstream = _require_str(raw, "upstream", source_path)
    # Enforce https so a tampered in-wheel pin cannot point git ls-remote / the
    # install URL at an exotic transport (ext::, file://, ssh with a command).
    if not upstream.startswith("https://"):
        raise PinFileError(f"upstream must be an https URL; got {upstream!r}")
    constraints_file = _require_str(raw, "constraints_file", source_path)

    skill_hashes = raw.get("skill_hashes")
    if not isinstance(skill_hashes, dict) or not skill_hashes:
        raise PinFileError("skill_hashes must be a non-empty object of file->sha256")
    for name, digest in skill_hashes.items():
        if not isinstance(name, str) or not isinstance(digest, str) or not digest:
            raise PinFileError(f"skill_hashes entry {name!r} has a non-string/empty hash")

    channels_raw = raw.get("channels", {})
    if not isinstance(channels_raw, dict):
        raise PinFileError("channels must be an object of name->{backends:[...]}")
    channels = {name: _validate_channel(name, spec) for name, spec in channels_raw.items()}

    return PinFile(
        schema_version=schema_version,
        upstream=upstream,
        commit_sha=commit_sha.lower(),
        version_label=str(raw.get("version_label", "")),
        vetted_at=str(raw.get("vetted_at", "")),
        constraints_file=constraints_file,
        skill_hashes=dict(skill_hashes),
        channels=channels,
        source_path=source_path,
    )


def _require_str(raw: dict, key: str, source_path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PinFileError(f"{key} must be a non-empty string in {source_path}")
    return value


def _validate_channel(name: str, spec: object) -> Channel:
    if not isinstance(spec, dict):
        raise PinFileError(f"channel {name!r} must be an object with a 'backends' list")
    backends_raw = spec.get("backends")
    if not isinstance(backends_raw, list) or not backends_raw:
        raise PinFileError(f"channel {name!r} must have a non-empty 'backends' list")
    backends = tuple(_validate_backend(name, b) for b in backends_raw)
    return Channel(name=name, backends=backends)


def _validate_backend(channel: str, raw: object) -> ChannelBackend:
    if not isinstance(raw, dict):
        raise PinFileError(f"channel {channel!r} backend must be an object")
    kind = raw.get("kind")
    if kind is None:
        # Note-only backend (e.g. a manual Chrome extension). Must carry a note
        # so it is not just an empty, meaningless entry.
        note = raw.get("note")
        if not isinstance(note, str) or not note.strip():
            raise PinFileError(
                f"channel {channel!r} has a backend with neither an install 'kind' nor a 'note'"
            )
        return ChannelBackend(kind=None, spec=None, note=note)

    if kind not in VALID_BACKEND_KINDS:
        raise PinFileError(
            f"channel {channel!r} backend kind {kind!r} is not one of {sorted(VALID_BACKEND_KINDS)}"
        )
    spec = raw.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise PinFileError(f"channel {channel!r} {kind} backend is missing a 'spec'")
    # A dash-leading spec would be parsed as an OPTION by npm/uv (argument
    # injection), the same shape guarded on the break-glass ref. Reject it here
    # even though the pin is vendored; the spec lands bare in the install argv.
    if spec.startswith("-"):
        raise PinFileError(f"channel {channel!r} {kind} spec may not start with '-': {spec!r}")
    if not _spec_is_exactly_pinned(kind, spec):
        raise PinFileError(
            f"channel {channel!r} {kind} spec {spec!r} is not exactly pinned "
            f"(npm needs '@<version>', pipx needs '==<version>', pipx-git needs '@<40-hex-sha>')"
        )
    return ChannelBackend(kind=kind, spec=spec, note=raw.get("note"))


def _spec_is_exactly_pinned(kind: str, spec: str) -> bool:
    # A whitespace/`||` compound is never an exact pin regardless of kind (npm
    # `a || b`, a pip environment marker, etc.), so reject it up front.
    if any(c.isspace() for c in spec) or "||" in spec:
        return False
    if kind == "npm":
        # Strip a single leading '@' scope marker, then require a '@<version>'
        # tail that is an EXACT semver (fullmatch) — `@latest`, `@1.2`, `@1.2.x`
        # all fail, so npm cannot resolve a floating version at enable time.
        body = spec[1:] if spec.startswith("@") else spec
        if "@" not in body:
            return False
        version = body.rsplit("@", 1)[1]
        return bool(_EXACT_SEMVER.fullmatch(version))
    if kind == "pipx":
        # Exactly one `==` operator, then an exact version (no `,` compound range,
        # no `.x`, no trailing operators).
        if spec.count("==") != 1:
            return False
        name, _, version = spec.partition("==")
        if not name.strip():
            return False
        version = version.strip()
        return bool(_EXACT_SEMVER.fullmatch(version))
    if kind == "pipx-git":
        # git+https only (same transport-restriction rationale as the upstream
        # field): never git+ssh/git+file/git+ext, which land bare in the uv argv.
        if not spec.startswith("git+https://") or "@" not in spec:
            return False
        sha = spec.rsplit("@", 1)[1]
        return bool(_HEX40.fullmatch(sha))
    return False
