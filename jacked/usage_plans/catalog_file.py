"""The plan catalog file: its model, its validation, and its loader.

``jacked/data/usage_plans.json`` ships with each jacked release. This module
turns it into frozen dataclasses and never raises: an invalid entry is
skipped with a WARNING that names it, and a missing or unreadable file gives
an empty catalog (every resolution then follows the live payload).
Stateless; :mod:`jacked.usage_plans` caches the bundled catalog.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

# One logger for the whole package: callers and tests filter on
# "jacked.usage_plans".
logger = logging.getLogger("jacked.usage_plans")

SUPPORTED_VERSION = 1
CATALOG_FILE = "usage_plans.json"
DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

SESSION_POLICIES = ("always", "never", "until_verified", "reported")
WINDOW_KINDS = ("session", "weekly", "weekly_model")

# Built-in values when the catalog file is missing or a field is invalid. A
# missing catalog must degrade to "follow the payload", never to a crash.
_BUILTIN_WINDOWS = {"session": 5 * 3600, "weekly": 7 * 86400, "weekly_model": 7 * 86400}
_BUILTIN_SESSION = "reported"
_BUILTIN_CRITICAL_PCT = 80.0

# Shown when a plan has no 5-hour window and its catalog entry has no note.
GENERIC_NO_SESSION_NOTE = "This plan has no 5-hour limit. Weekly limits still apply."

_LABEL_MAX = 40
_NOTE_MAX = 300
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Em-dash and en-dash, banned as prose dashes in UI copy. Built with chr() so
# the repo's em-dash guard (tests/unit/test_no_em_dash_in_user_text.py), which
# reads string constants, does not mistake this detector for UI text.
_PROSE_DASHES = (chr(0x2014), chr(0x2013))

_MISSING: Any = object()


@dataclass(frozen=True)
class PlanEntry:
    """One plan in the catalog, with inheritance already applied."""

    id: Optional[str]
    label: Optional[str]
    formerly: Optional[str]
    session: str
    session_critical_pct: float
    session_note: Optional[str] = None
    session_temporary_note: Optional[str] = None
    verified_at: Optional[str] = None
    sources: tuple = ()
    windows: tuple = ()  # ((kind, seconds), ...) overrides of the provider lengths


@dataclass(frozen=True)
class SubscriptionEntry:
    """A subscription type, and its tier variants when the catalog splits it."""

    plan: PlanEntry
    tiers: Mapping[str, PlanEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCatalog:
    windows: Mapping[str, int]
    default: PlanEntry
    subscriptions: Mapping[str, SubscriptionEntry]


@dataclass(frozen=True)
class Catalog:
    version: int
    verified_at: Optional[str]
    providers: Mapping[str, ProviderCatalog]


_BUILTIN_DEFAULT = PlanEntry(
    id=None, label=None, formerly=None,
    session=_BUILTIN_SESSION, session_critical_pct=_BUILTIN_CRITICAL_PCT,
)
_EMPTY_CATALOG = Catalog(version=SUPPORTED_VERSION, verified_at=None, providers={})


def provider_key(provider) -> str:
    """A provider name as the catalog keys it ("claude" when unset).

    >>> provider_key(" Codex "), provider_key(None)
    ('codex', 'claude')
    """
    return (str(provider).strip().lower() if provider else "") or "claude"


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _text(value, limit: int) -> Optional[str]:
    """A clean single-line string, or None when the value is not usable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or any(ch in text for ch in _PROSE_DASHES):
        return None
    if any(ord(ch) < 32 for ch in text):
        return None
    return text


class _EntryError(ValueError):
    """A catalog entry failed validation (the message names the field)."""


def _entry_windows(spec: dict, parent: PlanEntry) -> tuple:
    """The plan's window lengths: the parent's, overridden by *spec*.

    An unknown kind is logged and ignored: it is never a reason to drop an
    otherwise valid plan. An invalid length rejects the entry.
    """
    windows = dict(parent.windows)
    raw_windows = spec.get("windows")
    if raw_windows is None:
        return tuple(sorted(windows.items()))
    if not isinstance(raw_windows, dict):
        raise _EntryError("windows is not an object")
    for kind, seconds in raw_windows.items():
        if kind not in WINDOW_KINDS:
            logger.warning("Plan catalog: ignoring unknown window kind %r", kind)
            continue
        if not (isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0):
            raise _EntryError(f"invalid {kind} window length {seconds!r}")
        windows[kind] = seconds
    return tuple(sorted(windows.items()))


def _entry(spec, parent: PlanEntry, entry_id: Optional[str]) -> PlanEntry:
    """Validate one plan object and apply inheritance from *parent*."""
    if not isinstance(spec, dict):
        raise _EntryError("entry is not an object")

    def optional_text(key: str, limit: int, inherited: Optional[str]) -> Optional[str]:
        if key not in spec or spec[key] is None:
            return inherited
        text = _text(spec[key], limit)
        if text is None:
            raise _EntryError(f"invalid {key} {spec[key]!r}")
        return text

    label = optional_text("label", _LABEL_MAX, parent.label)
    if "label" in spec and label is None:
        raise _EntryError("empty label")
    session = spec.get("session", parent.session)
    if session not in SESSION_POLICIES:
        raise _EntryError(f"invalid session policy {session!r}")
    critical = spec.get("session_critical_pct", parent.session_critical_pct)
    if not _is_number(critical) or not 0 < critical <= 100:
        raise _EntryError(f"invalid session_critical_pct {critical!r}")
    verified_at = spec.get("verified_at", parent.verified_at)
    if verified_at is not None and not (
        isinstance(verified_at, str) and _DATE_RE.match(verified_at)
    ):
        raise _EntryError(f"invalid verified_at {verified_at!r}")
    sources = spec.get("sources", list(parent.sources))
    if not isinstance(sources, list) or not all(
        isinstance(s, str) and s.startswith("https://") for s in sources
    ):
        raise _EntryError(f"invalid sources {sources!r} (want a list of https URLs)")
    return PlanEntry(
        id=entry_id, label=label,
        formerly=optional_text("formerly", _LABEL_MAX, None),
        session=session, session_critical_pct=float(critical),
        session_note=optional_text("session_note", _NOTE_MAX, None),
        session_temporary_note=optional_text("session_temporary_note", _NOTE_MAX, None),
        verified_at=verified_at, sources=tuple(sources),
        windows=_entry_windows(spec, parent),
    )


def _windows(provider: str, spec) -> dict[str, int]:
    windows = dict(_BUILTIN_WINDOWS)
    if spec is None:
        return windows
    if not isinstance(spec, dict):
        logger.warning("Plan catalog: %s windows is not an object; using defaults", provider)
        return windows
    for kind, seconds in spec.items():
        if kind not in WINDOW_KINDS:
            logger.warning("Plan catalog: %s has unknown window kind %r", provider, kind)
            continue
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
            windows[kind] = seconds
        else:
            logger.warning(
                "Plan catalog: %s window %r has invalid length %r; using %d s",
                provider, kind, seconds, windows[kind],
            )
    return windows


def _tiers(name: str, sub_name, sub_spec: dict, plan: PlanEntry) -> dict[str, PlanEntry]:
    """The tier variants of one subscription; a bad tier is skipped."""
    tiers: dict[str, PlanEntry] = {}
    raw_tiers = sub_spec.get("tiers") or {}
    if not isinstance(raw_tiers, dict):
        logger.warning("Plan catalog: %s subscription %r tiers is not an object", name, sub_name)
        return tiers
    for tier_name, tier_spec in raw_tiers.items():
        tier_id = tier_spec.get("id") if isinstance(tier_spec, dict) else None
        if not (isinstance(tier_id, str) and tier_id.strip()):
            logger.warning("Plan catalog: skipping %s tier %r: missing id", name, tier_name)
            continue
        try:
            tiers[str(tier_name).strip().lower()] = _entry(tier_spec, plan, tier_id.strip())
        except _EntryError as e:
            logger.warning("Plan catalog: skipping %s tier %r: %s", name, tier_name, e)
    return tiers


def _provider(name: str, spec) -> Optional[ProviderCatalog]:
    if not isinstance(spec, dict):
        logger.warning("Plan catalog: provider %r is not an object; skipping it", name)
        return None
    try:
        default = _entry(spec.get("default") or {}, _BUILTIN_DEFAULT, None)
    except _EntryError as e:
        logger.warning("Plan catalog: %s default is invalid (%s); using built-ins", name, e)
        default = _BUILTIN_DEFAULT
    subscriptions: dict[str, SubscriptionEntry] = {}
    raw_subs = spec.get("subscriptions") or {}
    if not isinstance(raw_subs, dict):
        logger.warning("Plan catalog: %s subscriptions is not an object", name)
        raw_subs = {}
    for sub_name, sub_spec in raw_subs.items():
        key = str(sub_name).strip().lower()
        try:
            plan = _entry(sub_spec, default, key)
        except _EntryError as e:
            logger.warning("Plan catalog: skipping %s subscription %r: %s", name, sub_name, e)
            continue
        subscriptions[key] = SubscriptionEntry(
            plan=plan, tiers=_tiers(name, sub_name, sub_spec, plan)
        )
    return ProviderCatalog(
        windows=_windows(name, spec.get("windows")),
        default=default,
        subscriptions=subscriptions,
    )


def _read_catalog(path: Path) -> Optional[tuple[dict, int]]:
    """The parsed catalog object and its version, or None (with a WARNING)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Plan catalog not found at %s; following live payloads only", path)
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable plan catalog %s: %s", path, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("Plan catalog %s is not a JSON object; ignoring it", path)
        return None
    version = raw.get("version")
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 1):
        logger.warning("Plan catalog %s has an invalid version %r; ignoring it", path, version)
        return None
    if version > SUPPORTED_VERSION:
        logger.warning(
            "Plan catalog %s is version %d, newer than this jacked reads (%d); "
            "reading it best-effort",
            path, version, SUPPORTED_VERSION,
        )
    return raw, version


def load_catalog(data_root: Optional[Path] = None) -> Catalog:
    """Load and validate ``usage_plans.json`` from *data_root*.

    Never raises. A missing or unreadable file, or a bad ``version``, gives
    an empty catalog (every resolution then follows the live payload) and a
    WARNING. A newer ``version`` warns and is read best-effort. Each invalid
    subscription or tier is skipped with a WARNING that names it, so one bad
    entry never takes the rest down.

    >>> cat = load_catalog()
    >>> cat.version, cat.providers["claude"].subscriptions["max"].tiers["default_claude_max_20x"].label
    (1, 'Ultra')
    """
    path = Path(data_root or DATA_ROOT) / CATALOG_FILE
    found = _read_catalog(path)
    if found is None:
        return _EMPTY_CATALOG
    raw, version = found
    verified_at = raw.get("verified_at")
    if not (isinstance(verified_at, str) and _DATE_RE.match(verified_at)):
        verified_at = None
    raw_providers = raw.get("providers") or {}
    if not isinstance(raw_providers, dict):
        logger.warning("Plan catalog %s: providers is not an object", path)
        raw_providers = {}
    providers: dict[str, ProviderCatalog] = {}
    for name, spec in raw_providers.items():
        key = str(name).strip().lower()
        pcat = _provider(key, spec)
        if pcat is not None:
            providers[key] = pcat
    return Catalog(version=version, verified_at=verified_at, providers=providers)
