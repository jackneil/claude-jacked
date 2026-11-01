"""Usage-window catalog: which limits each provider plan has, and its name.

Providers change their plans without notice. On 2026-10-30 Anthropic renamed
Max 5x to Max and Max 20x to Ultra, and removed the 5-hour limit for verified
Ultra accounts. ChatGPT Pro has no 5-hour limit in Codex. This module reads
``jacked/data/usage_plans.json``, which ships with each jacked release, so the
next change of that kind is a data edit instead of a code hunt.

Precedence, the rule every consumer relies on:

1. The provider's live payload is the truth for one account. A payload that
   reports a 5-hour window wins over the catalog.
2. The catalog decides how to read SILENCE for a plan (a payload with no
   5-hour window), names the plan, and gives the window lengths.
3. The provider's own plan name (the Claude profile ``plan_display_name``)
   wins over the catalog label, so a rename shows up without a release.

A payload that contradicts the catalog is catalog DRIFT: one WARNING per shape
per process, plus a code in the account's ``plan.drift`` list, so a stale
catalog is visible instead of silently wrong. Drift codes:

- ``session_missing``: the plan should have a 5-hour window, and an
  authoritative payload reports none.
- ``session_on_never_plan``: the plan should not have one, and the payload
  reports one.
- ``unknown_tier``: the subscription type is split by rate-limit tier in the
  catalog, and this tier is not listed.
- ``renamed``: the provider calls the plan something the catalog does not.

Session policies (``session`` in the catalog):

- ``always``: the plan has a 5-hour window. Claude silence means no window is
  open right now (idle), which the window keeper may open.
- ``never``: the plan has no 5-hour window.
- ``until_verified``: the provider removes the window once it verifies the
  account (Ultra). A reported window is temporary.
- ``reported``: no fixed expectation. An authoritative payload decides; Claude
  silence without one keeps the legacy "idle" reading.

Stdlib only at import time (the Codex payload parser is imported lazily), so
any jacked surface can use it without pulling in the web stack.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

SUPPORTED_VERSION = 1
CATALOG_FILE = "usage_plans.json"
DATA_ROOT = Path(__file__).parent / "data"

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
_PROSE_DASHES = ("—", "–")  # em-dash, en-dash: banned in UI copy

_MISSING: Any = object()


# --------------------------------------------------------------------------- #
# Catalog model
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #


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
    windows = dict(parent.windows)
    raw_windows = spec.get("windows")
    if raw_windows is not None:
        if not isinstance(raw_windows, dict):
            raise _EntryError("windows is not an object")
        for kind, seconds in raw_windows.items():
            if kind not in WINDOW_KINDS:
                raise _EntryError(f"unknown window kind {kind!r}")
            if not (isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0):
                raise _EntryError(f"invalid {kind} window length {seconds!r}")
            windows[kind] = seconds
    return PlanEntry(
        id=entry_id,
        label=label,
        formerly=optional_text("formerly", _LABEL_MAX, None),
        session=session,
        session_critical_pct=float(critical),
        session_note=optional_text("session_note", _NOTE_MAX, None),
        session_temporary_note=optional_text("session_temporary_note", _NOTE_MAX, None),
        verified_at=verified_at,
        sources=tuple(sources),
        windows=tuple(sorted(windows.items())),
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
        tiers: dict[str, PlanEntry] = {}
        raw_tiers = sub_spec.get("tiers") or {}
        if not isinstance(raw_tiers, dict):
            logger.warning(
                "Plan catalog: %s subscription %r tiers is not an object", name, sub_name
            )
            raw_tiers = {}
        for tier_name, tier_spec in raw_tiers.items():
            tier_key = str(tier_name).strip().lower()
            tier_id = tier_spec.get("id") if isinstance(tier_spec, dict) else None
            if not (isinstance(tier_id, str) and tier_id.strip()):
                logger.warning(
                    "Plan catalog: skipping %s tier %r: missing id", name, tier_name
                )
                continue
            try:
                tiers[tier_key] = _entry(tier_spec, plan, tier_id.strip())
            except _EntryError as e:
                logger.warning("Plan catalog: skipping %s tier %r: %s", name, tier_name, e)
        subscriptions[key] = SubscriptionEntry(plan=plan, tiers=tiers)
    return ProviderCatalog(
        windows=_windows(name, spec.get("windows")),
        default=default,
        subscriptions=subscriptions,
    )


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
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Plan catalog not found at %s; following live payloads only", path)
        return _EMPTY_CATALOG
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable plan catalog %s: %s", path, e)
        return _EMPTY_CATALOG
    if not isinstance(raw, dict):
        logger.warning("Plan catalog %s is not a JSON object; ignoring it", path)
        return _EMPTY_CATALOG
    version = raw.get("version")
    if not (isinstance(version, int) and not isinstance(version, bool) and version >= 1):
        logger.warning("Plan catalog %s has an invalid version %r; ignoring it", path, version)
        return _EMPTY_CATALOG
    if version > SUPPORTED_VERSION:
        logger.warning(
            "Plan catalog %s is version %d, newer than this jacked reads (%d); "
            "reading it best-effort",
            path, version, SUPPORTED_VERSION,
        )
    verified_at = raw.get("verified_at")
    if not (isinstance(verified_at, str) and _DATE_RE.match(verified_at)):
        verified_at = None
    providers: dict[str, ProviderCatalog] = {}
    raw_providers = raw.get("providers") or {}
    if not isinstance(raw_providers, dict):
        logger.warning("Plan catalog %s: providers is not an object", path)
        raw_providers = {}
    for name, spec in raw_providers.items():
        pcat = _provider(str(name).strip().lower(), spec)
        if pcat is not None:
            providers[str(name).strip().lower()] = pcat
    return Catalog(version=version, verified_at=verified_at, providers=providers)


_catalog_cache: Optional[Catalog] = None


def catalog() -> Catalog:
    """The bundled catalog, loaded once per process."""
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = load_catalog()
    return _catalog_cache


def window_seconds(provider: Optional[str], kind: str) -> int:
    """Length of a window kind for a provider, in seconds.

    >>> window_seconds("claude", "session"), window_seconds("claude", "weekly")
    (18000, 604800)
    """
    if kind not in WINDOW_KINDS:
        raise ValueError(f"unknown window kind {kind!r}")
    pcat = catalog().providers.get(_provider_key(provider))
    if pcat is None:
        return _BUILTIN_WINDOWS[kind]
    return int(pcat.windows.get(kind, _BUILTIN_WINDOWS[kind]))


# --------------------------------------------------------------------------- #
# Drift reporting
# --------------------------------------------------------------------------- #

_drift_logged: set = set()


def _note_drift(provider: str, plan_key: str, code: str, detail: str) -> None:
    """Log one WARNING per (provider, plan, code, detail) per process."""
    key = (provider, plan_key, code, detail)
    if key in _drift_logged:
        return
    _drift_logged.add(key)
    logger.warning(
        "Plan catalog drift (%s) for %s plan %r: %s. This jacked release's plan "
        "catalog (verified %s) may be out of date; upgrade jacked when a newer "
        "release is available.",
        code, provider, plan_key, detail, catalog().verified_at or "unknown",
    )


# --------------------------------------------------------------------------- #
# Plan resolution
# --------------------------------------------------------------------------- #


def _provider_key(provider) -> str:
    return (str(provider).strip().lower() if provider else "") or "claude"


def _norm(value) -> str:
    return str(value).strip().lower() if isinstance(value, str) else ""


def _raw_label(subscription: str) -> Optional[str]:
    if not subscription:
        return None
    words = re.split(r"[_\s-]+", subscription)
    label = " ".join(w.capitalize() for w in words if w)
    return label[:_LABEL_MAX] or None


def _profile_plan_name(row: Mapping) -> Optional[str]:
    """The plan name Anthropic puts on the account's profile, if usable."""
    profile = row.get("profile_json")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            return None
    if not isinstance(profile, dict):
        return None
    org = profile.get("organization")
    if not isinstance(org, dict):
        return None
    return _text(org.get("plan_display_name"), _LABEL_MAX)


@dataclass(frozen=True)
class PlanMatch:
    """What jacked knows about an account's plan."""

    provider: str
    id: Optional[str]
    label: Optional[str]
    formerly: Optional[str]
    label_source: str  # provider | catalog | raw | none
    session: str
    session_critical_pct: float
    session_note: Optional[str]
    session_temporary_note: Optional[str]
    drift: tuple = ()
    plan_key: str = ""
    windows: Mapping[str, int] = field(default_factory=lambda: dict(_BUILTIN_WINDOWS))

    def window_seconds(self, kind: str) -> int:
        """This plan's length for a window kind (plan, then provider, then built-in)."""
        if kind not in WINDOW_KINDS:
            raise ValueError(f"unknown window kind {kind!r}")
        return int(self.windows.get(kind, _BUILTIN_WINDOWS[kind]))


def resolve_plan(row: Mapping) -> PlanMatch:
    """Match an account row to its catalog plan.

    Lookup: the subscription type first, then its tier when the catalog
    splits that subscription by tier. A row with a tier but no subscription
    type looks the tier up across subscriptions.

    >>> p = resolve_plan({"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"})
    >>> p.id, p.label, p.formerly, p.session, p.session_critical_pct
    ('ultra', 'Ultra', 'Max 20x', 'until_verified', 95.0)
    >>> resolve_plan({"provider": "codex", "subscription_type": "pro"}).session
    'never'
    """
    provider = _provider_key(row.get("provider"))
    sub = _norm(row.get("subscription_type"))
    tier = _norm(row.get("rate_limit_tier"))
    pcat = catalog().providers.get(provider)
    entry: Optional[PlanEntry] = None
    drift: list[str] = []
    if pcat is not None:
        sub_entry = pcat.subscriptions.get(sub) if sub else None
        if sub_entry is not None:
            if tier and tier in sub_entry.tiers:
                entry = sub_entry.tiers[tier]
            else:
                entry = sub_entry.plan
                if tier and sub_entry.tiers:
                    drift.append("unknown_tier")
        elif not sub and tier:
            for candidate in pcat.subscriptions.values():
                if tier in candidate.tiers:
                    entry = candidate.tiers[tier]
                    break
    base = entry or (pcat.default if pcat is not None else _BUILTIN_DEFAULT)
    plan_key = (entry.id if entry and entry.id else sub) or "unknown"

    provider_name = _profile_plan_name(row) if provider == "claude" else None
    catalog_label = entry.label if entry is not None else None
    if provider_name:
        label, source = provider_name, "provider"
        if catalog_label and provider_name.casefold() != catalog_label.casefold():
            drift.append("renamed")
    elif catalog_label:
        label, source = catalog_label, "catalog"
    else:
        label = _raw_label(sub)
        source = "raw" if label else "none"

    for code in drift:
        detail = {
            "unknown_tier": f"rate limit tier {tier!r} is not in the catalog",
            "renamed": f"the provider calls it {provider_name!r}, the catalog {catalog_label!r}",
        }[code]
        _note_drift(provider, plan_key, code, detail)

    windows = dict(pcat.windows) if pcat is not None else dict(_BUILTIN_WINDOWS)
    windows.update(dict(base.windows))
    return PlanMatch(
        provider=provider,
        id=entry.id if entry is not None else None,
        label=label,
        formerly=entry.formerly if entry is not None else None,
        label_source=source,
        session=base.session,
        session_critical_pct=base.session_critical_pct,
        session_note=base.session_note,
        session_temporary_note=base.session_temporary_note,
        drift=tuple(drift),
        plan_key=plan_key,
        windows=windows,
    )


# --------------------------------------------------------------------------- #
# Session (5-hour) window resolution
# --------------------------------------------------------------------------- #


def raw_usage(row: Mapping) -> Optional[dict]:
    """The row's cached usage payload as a dict, or None (never raises).

    >>> raw_usage({"cached_usage_raw": '{"five_hour": null}'})
    {'five_hour': None}
    >>> raw_usage({"cached_usage_raw": "garbage"}) is None
    True
    """
    raw = row.get("cached_usage_raw")
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, (str, bytes)):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# Kinds of entries in the Claude ``limits`` list that jacked understands. A
# list with any other kind is a payload shape jacked has not seen: its silence
# about a session window proves nothing, and the new kind is reported as drift.
KNOWN_CLAUDE_LIMIT_KINDS = frozenset({"session", "weekly_all", "weekly_scoped"})


def _claude_evidence(raw: Optional[dict]) -> tuple[bool, bool, tuple]:
    """(reports a session window, is authoritative, unrecognized limit kinds).

    Authoritative means the payload carries a ``limits`` list in which every
    entry has a kind jacked recognizes: such a list names every limit the
    account has. Older payloads without it used a null ``five_hour`` for "no
    window open", so their silence proves nothing; neither does the silence
    of a list that holds a kind jacked does not know (a renamed session kind
    must never read as "no 5-hour limit").
    """
    if not isinstance(raw, dict):
        return False, False, ()
    limits = raw.get("limits")
    reported = False
    unknown: list[str] = []
    if isinstance(limits, list):
        for lim in limits:
            kind = lim.get("kind") if isinstance(lim, dict) else None
            if not isinstance(kind, str):
                # A dict or list kind is unhashable; any non-string is a shape
                # jacked does not know.
                unknown.append(f"<{type(kind).__name__}>")
            elif kind == "session":
                reported = True
            elif kind not in KNOWN_CLAUDE_LIMIT_KINDS:
                unknown.append(kind[:40])
    authoritative = isinstance(limits, list) and not unknown
    five = raw.get("five_hour")
    if isinstance(five, dict) and (
        five.get("utilization") is not None or five.get("resets_at") is not None
    ):
        reported = True
    return reported, authoritative, tuple(sorted(set(unknown)))


def _cached_session_evidence(row: Mapping) -> bool:
    """True when the row still holds a 5-hour reading or reset.

    The cache keeps the last reading until a payload clears it, so this is
    prior evidence of a window that a partial payload must not erase.
    """
    return row.get("cached_usage_5h") is not None or bool(row.get("cached_5h_resets_at"))


def _codex_observed_seconds(raw: Optional[dict]) -> dict[str, int]:
    """Lengths of the Codex windows jacked actually uses, by kind.

    Taken from :func:`jacked.codex.usage.normalize_rate_limits`, the one
    classifier of Codex windows, so a window it drops (a slot collision) or
    maps by position can never lend its length to another window.
    """
    norm = _codex_windows(raw)
    out: dict[str, int] = {}
    if norm is None:
        return out
    for slot, kind in (("five_hour", "session"), ("seven_day", "weekly")):
        mins = norm[slot].get("window_minutes")
        if _is_number(mins) and mins > 0:
            out[kind] = int(mins * 60)
    return out


def _codex_windows(raw: Optional[dict]) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("rateLimits"), dict):
        return None
    from jacked.codex.usage import normalize_rate_limits  # lazy: keeps import light

    return normalize_rate_limits(raw)


@dataclass(frozen=True)
class SessionWindow:
    """Whether an account has a 5-hour (session) window right now."""

    state: str  # present | absent | unknown
    temporary: bool
    seconds: int
    note: Optional[str]
    drift: tuple = ()


_SESSION_DRIFT_DETAIL = {
    "session_missing": "the plan should have a 5-hour window and the payload reports none",
    "session_on_never_plan": "the plan should have no 5-hour window and the payload reports one",
}


def session_window(
    row: Mapping, raw: Any = _MISSING, plan: Optional[PlanMatch] = None
) -> SessionWindow:
    """Resolve the account's 5-hour window from its payload and its plan.

    *raw* defaults to the row's cached payload; pass None for "no payload".
    Evidence order: a payload that reports the window; an authoritative
    payload's silence; the row's cached 5-hour reading (prior evidence a
    partial payload must not erase); then the plan's policy.

    >>> ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}
    >>> session_window(ultra, {"five_hour": None, "limits": []}).state
    'absent'
    >>> session_window(ultra, {"limits": [{"kind": "session"}]}).temporary
    True
    """
    plan = plan or resolve_plan(row)
    provider = plan.provider
    if raw is _MISSING:
        raw = raw_usage(row)
    seconds = plan.window_seconds("session")
    if provider == "codex":
        seconds = _codex_observed_seconds(raw).get("session", seconds)
    absent_note = plan.session_note or GENERIC_NO_SESSION_NOTE
    temporary_policy = plan.session == "until_verified"
    drift: list[str] = []

    if provider == "codex":
        norm = _codex_windows(raw)
        reported = bool(norm and norm["five_hour"]["reported"])
        authoritative = bool(norm and norm["windows_complete"])
    else:
        reported, authoritative, unknown_kinds = _claude_evidence(raw)
        if unknown_kinds:
            drift.append("unknown_limit_kind")
            _note_drift(
                provider, plan.plan_key, "unknown_limit_kind",
                f"the usage payload has limit kinds jacked does not know: {', '.join(unknown_kinds)}",
            )

    def present(temporary: bool = False) -> tuple:
        return ("present", temporary, plan.session_temporary_note if temporary else None)

    if reported:
        if plan.session == "never":
            drift.append("session_on_never_plan")
        state, temporary, note = present(temporary_policy)
    elif authoritative:
        if plan.session == "always" and provider == "claude":
            # Claude's silence can mean "no window open": keep the window, flag it.
            drift.append("session_missing")
            state, temporary, note = present()
        else:
            # The payload lists every limit the account has, and no 5-hour one.
            if plan.session == "always":
                drift.append("session_missing")
            state, temporary, note = "absent", False, absent_note
    elif _cached_session_evidence(row):
        state, temporary, note = present(temporary_policy)
    elif plan.session == "never":
        state, temporary, note = "absent", False, absent_note
    elif provider == "codex" or plan.session == "until_verified":
        state, temporary, note = "unknown", False, None
    else:
        # always / reported with an old payload shape: no window open (idle).
        state, temporary, note = present()

    for code in drift:
        if code in _SESSION_DRIFT_DETAIL:
            _note_drift(provider, plan.plan_key, code, _SESSION_DRIFT_DETAIL[code])
    return SessionWindow(state, temporary, seconds, note, tuple(drift))


def has_session(row: Mapping, raw: Any = _MISSING) -> bool:
    """False only when the account has no 5-hour window (state absent).

    An unknown state counts as a window: jacked never skips a window it
    cannot rule out.

    >>> ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"}
    >>> has_session(ultra, {"five_hour": None, "limits": []})
    False
    >>> has_session(ultra, None)
    True
    """
    return session_window(row, raw).state != "absent"


def model_window_seconds(
    provider: Optional[str],
    window: Optional[Mapping] = None,
    slot: str = "weekly_model",
    plan: Optional[PlanMatch] = None,
) -> int:
    """Length of a model-scoped limit's window, in seconds.

    A Codex named limit carries ``windowDurationMins`` when the payload has
    it, and that observed length wins. Otherwise the length of *slot* for the
    account's *plan* (a plan may override its provider's lengths), or for the
    provider when no plan is given. Claude model limits (``weekly_scoped``)
    use the ``weekly_model`` slot; a Codex limit uses ``session`` for a
    primary window and ``weekly`` for a secondary one.

    >>> model_window_seconds("claude")
    604800
    >>> model_window_seconds("codex", {"windowDurationMins": 300}, "weekly")
    18000
    """
    if isinstance(window, Mapping):
        mins = window.get("windowDurationMins")
        if _is_number(mins) and mins > 0:
            return int(mins * 60)
    if slot not in WINDOW_KINDS:
        slot = "weekly_model"
    if plan is not None:
        return plan.window_seconds(slot)
    return window_seconds(provider, slot)


# --------------------------------------------------------------------------- #
# Per-account view and payload shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AccountView:
    """Plan and window facts for one account row, resolved once."""

    row: Mapping
    plan: PlanMatch
    session: SessionWindow
    weekly_state: str
    weekly_seconds: int = _BUILTIN_WINDOWS["weekly"]

    def plan_payload(self) -> dict:
        """The ``plan`` object of the account API and WebSocket payloads."""
        drift = list(self.plan.drift)
        drift.extend(code for code in self.session.drift if code not in drift)
        return {
            "id": self.plan.id,
            "label": self.plan.label,
            "formerly": self.plan.formerly,
            "label_source": self.plan.label_source,
            "drift": drift,
            "catalog_verified_at": catalog().verified_at,
        }

    def windows_payload(self) -> dict:
        """The ``windows`` object of the account API and WebSocket payloads."""
        return {
            "five_hour": {
                "state": self.session.state,
                "temporary": self.session.temporary,
                "seconds": self.session.seconds,
                "note": self.session.note,
            },
            "seven_day": {
                "state": self.weekly_state,
                "temporary": False,
                "seconds": self.weekly_seconds,
                "note": None,
            },
        }

    def five_hour_reading(self) -> Optional[float]:
        """The 5-hour value to show: None when the account has no such window.

        A Claude account that has the window but no reading has no window
        open: 0% used. Codex never turns a missing reading into 0%.
        """
        value = self.row.get("cached_usage_5h")
        if self.session.state == "absent":
            return None
        if value is not None:
            return value
        if self.session.state == "present" and self.plan.provider != "codex":
            return 0.0
        return None

    def projected_row(self) -> Mapping:
        """The row as every decision must read it.

        When the account has no 5-hour window, a 5-hour value or reset still
        in the cache is stale by definition (a payload may omit the key, and a
        missing key keeps the old cache): the projection drops both, so the
        dashboard, the menu bar, pacing, polling and auto-swap all agree.
        Otherwise the row is returned unchanged.
        """
        if self.session.state != "absent":
            return self.row
        if self.row.get("cached_usage_5h") is None and not self.row.get("cached_5h_resets_at"):
            return self.row
        projected = dict(self.row)
        projected["cached_usage_5h"] = None
        projected["cached_5h_resets_at"] = None
        return projected


def project_row(row: Mapping) -> Mapping:
    """:meth:`AccountView.projected_row` for one row (no 5-hour value on an absent window).

    >>> ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
    ...          "cached_usage_5h": 100.0, "cached_5h_resets_at": "2026-11-01T20:00:00+00:00",
    ...          "cached_usage_raw": '{"limits": [{"kind": "weekly_all"}]}'}
    >>> p = project_row(ultra)
    >>> p["cached_usage_5h"], p["cached_5h_resets_at"], ultra["cached_usage_5h"]
    (None, None, 100.0)
    """
    return account_view(row).projected_row()


def account_view(row: Mapping, raw: Any = _MISSING) -> AccountView:
    """Resolve plan and windows for one account row (parses its payload once)."""
    if raw is _MISSING:
        raw = raw_usage(row)
    plan = resolve_plan(row)
    session = session_window(row, raw, plan)
    weekly_state = "present"
    weekly_seconds = plan.window_seconds("weekly")
    if plan.provider == "codex":
        weekly_seconds = _codex_observed_seconds(raw).get("weekly", weekly_seconds)
        norm = _codex_windows(raw)
        if norm is not None and norm["seven_day"]["reported"]:
            weekly_state = "present"
        elif norm is not None and norm["windows_complete"]:
            weekly_state = "absent"
        else:
            weekly_state = "unknown" if norm is not None else "present"
    return AccountView(
        row=row, plan=plan, session=session,
        weekly_state=weekly_state, weekly_seconds=weekly_seconds,
    )
