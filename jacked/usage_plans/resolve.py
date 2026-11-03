"""Resolve an account row to its plan and its 5-hour (session) window.

The live payload is the truth for one account; the catalog decides how to
read silence for a plan (see :mod:`jacked.usage_plans`). The catalog and the
drift log live in the package: this module reads them at call time through
``_pkg``, so a test that swaps ``jacked.usage_plans._catalog_cache`` reaches
every resolver.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from jacked import usage_plans as _pkg

from .catalog_file import (
    _BUILTIN_DEFAULT,
    _BUILTIN_WINDOWS,
    _LABEL_MAX,
    _MISSING,
    GENERIC_NO_SESSION_NOTE,
    WINDOW_KINDS,
    PlanEntry,
    _text,
    logger,
    provider_key,
    window_length_ok,
)

# Kinds of entries in the Claude ``limits`` list that jacked understands. A
# list with any other kind is a payload shape jacked has not seen: its silence
# about a session window proves nothing, and the new kind is reported as drift.
KNOWN_CLAUDE_LIMIT_KINDS = frozenset({"session", "weekly_all", "weekly_scoped"})


def _norm(value) -> str:
    return str(value).strip().lower() if isinstance(value, str) else ""


def _raw_label(subscription: str) -> Optional[str]:
    if not subscription:
        return None
    words = re.split(r"[_\s-]+", subscription)
    label = " ".join(w.capitalize() for w in words if w)
    return label[:_LABEL_MAX] or None


# (account id, field) pairs already logged for a foreign stored profile;
# bounded, because the resolver runs per row on every poll.
_FOREIGN_PROFILE_LOGGED: set = set()


def _profile_plan_name(row: Mapping) -> Optional[str]:
    """The plan name Anthropic puts on the account's profile, if usable.

    Only a profile that belongs to this row counts: the codebase's one owner
    rule (:func:`jacked.api.claude_identity.profile_owner_mismatch`, the rule
    the other stored-profile reader applies). A re-auth that moved the row to
    another organization can leave the old organization's profile stored, and
    its plan name must never label this account.
    """
    profile = row.get("profile_json")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            return None
    if not isinstance(profile, dict):
        return None
    from jacked.api.claude_identity import profile_owner_mismatch  # lazy: light, stdlib-only

    mismatch = profile_owner_mismatch(
        row.get("email"), row.get("organization_uuid"), row.get("account_uuid"),
        profile, require_uuid=False,
    )
    if mismatch is not None:
        key = (row.get("id"), mismatch)
        if key not in _FOREIGN_PROFILE_LOGGED and len(_FOREIGN_PROFILE_LOGGED) < 256:
            _FOREIGN_PROFILE_LOGGED.add(key)
            logger.info(
                "Plan label: the stored profile of account %s belongs to another "
                "owner (%s); using the catalog label", row.get("id"), mismatch,
            )
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
    weekly_critical_pct: float
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


def _match_entry(pcat, sub: str, tier: str) -> tuple[Optional[PlanEntry], list[str]]:
    """The catalog entry for a subscription and tier, and any drift codes.

    The subscription type comes first, then its tier when the catalog splits
    that subscription by tier. A tier with no subscription type is looked up
    across subscriptions.
    """
    if pcat is None:
        return None, []
    sub_entry = pcat.subscriptions.get(sub) if sub else None
    if sub_entry is not None:
        if tier and tier in sub_entry.tiers:
            return sub_entry.tiers[tier], []
        return sub_entry.plan, (["unknown_tier"] if tier and sub_entry.tiers else [])
    if not sub and tier:
        for candidate in pcat.subscriptions.values():
            if tier in candidate.tiers:
                return candidate.tiers[tier], []
    return None, []


def _plan_label(
    row: Mapping, provider: str, entry: Optional[PlanEntry], sub: str
) -> tuple[Optional[str], str, Optional[str]]:
    """(label, label_source, provider name): provider, then catalog, then raw."""
    provider_name = _profile_plan_name(row) if provider == "claude" else None
    if provider_name:
        return provider_name, "provider", provider_name
    if entry is not None and entry.label:
        return entry.label, "catalog", None
    label = _raw_label(sub)
    return label, ("raw" if label else "none"), None


def resolve_plan(row: Mapping) -> PlanMatch:
    """Match an account row to its catalog plan.

    >>> p = resolve_plan({"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x"})
    >>> p.id, p.label, p.formerly, p.session, p.session_critical_pct, p.weekly_critical_pct
    ('ultra', 'Ultra', 'Max 20x', 'until_verified', 95.0, 98.0)
    >>> resolve_plan({"provider": "codex", "subscription_type": "pro"}).session
    'never'
    """
    provider = provider_key(row.get("provider"))
    sub, tier = _norm(row.get("subscription_type")), _norm(row.get("rate_limit_tier"))
    pcat = _pkg.catalog().providers.get(provider)
    entry, drift = _match_entry(pcat, sub, tier)
    base = entry or (pcat.default if pcat is not None else _BUILTIN_DEFAULT)
    plan_key = (entry.id if entry and entry.id else sub) or "unknown"
    label, source, provider_name = _plan_label(row, provider, entry, sub)
    catalog_label = entry.label if entry is not None else None
    if provider_name and catalog_label and provider_name.casefold() != catalog_label.casefold():
        drift.append("renamed")
    for code in drift:
        detail = {
            "unknown_tier": f"rate limit tier {tier!r} is not in the catalog",
            "renamed": f"the provider calls it {provider_name!r}, the catalog {catalog_label!r}",
        }[code]
        _pkg._note_drift(provider, plan_key, code, detail)
    windows = dict(pcat.windows) if pcat is not None else dict(_BUILTIN_WINDOWS)
    windows.update(dict(base.windows))
    return PlanMatch(
        provider=provider, id=entry.id if entry is not None else None,
        label=label, formerly=entry.formerly if entry is not None else None,
        label_source=source, session=base.session,
        session_critical_pct=base.session_critical_pct,
        weekly_critical_pct=base.weekly_critical_pct,
        session_note=base.session_note,
        session_temporary_note=base.session_temporary_note,
        drift=tuple(drift), plan_key=plan_key, windows=windows,
    )


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
                # ascii(): a provider string never writes raw control
                # characters (a forged log line) into the drift WARNING.
                unknown.append(ascii(kind[:40]))
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


def _codex_windows(raw: Optional[dict]) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("rateLimits"), dict):
        return None
    from jacked.codex.usage import normalize_rate_limits  # lazy: keeps import light

    return normalize_rate_limits(raw)


def codex_observed_seconds(raw: Optional[dict]) -> dict[str, int]:
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
        if window_length_ok(mins) and window_length_ok(mins * 60):
            out[kind] = int(mins * 60)
    return out


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


def _session_evidence(plan: PlanMatch, raw) -> tuple[bool, bool, list[str]]:
    """(payload reports a session window, payload is authoritative, drift)."""
    if plan.provider == "codex":
        norm = _codex_windows(raw)
        return bool(norm and norm["five_hour"]["reported"]), bool(
            norm and norm["windows_complete"]
        ), []
    reported, authoritative, unknown_kinds = _claude_evidence(raw)
    if not unknown_kinds:
        return reported, authoritative, []
    _pkg._note_drift(
        plan.provider, plan.plan_key, "unknown_limit_kind",
        f"the usage payload has limit kinds jacked does not know: {', '.join(unknown_kinds)}",
    )
    return reported, authoritative, ["unknown_limit_kind"]


def _decide_session(
    plan: PlanMatch, row: Mapping, reported: bool, authoritative: bool
) -> tuple[str, bool, Optional[str], list[str]]:
    """(state, temporary, note, drift) from the evidence, in order."""
    temp = plan.session == "until_verified"
    absent = ("absent", False, plan.session_note or GENERIC_NO_SESSION_NOTE)

    def present(temporary: bool = False) -> tuple:
        return ("present", temporary, plan.session_temporary_note if temporary else None)

    if reported:
        drift = ["session_on_never_plan"] if plan.session == "never" else []
        return (*present(temp), drift)
    if authoritative:
        drift = ["session_missing"] if plan.session == "always" else []
        # Claude's silence on an "always" plan can mean "no window open".
        claude_idle = plan.session == "always" and plan.provider == "claude"
        return (*(present() if claude_idle else absent), drift)
    if _cached_session_evidence(row):
        return (*present(temp), [])
    if plan.session == "never":
        return (*absent, [])
    if plan.provider == "codex" or temp:
        return ("unknown", False, None, [])
    # always / reported with an old payload shape: no window open (idle).
    return (*present(), [])


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
    if raw is _MISSING:
        raw = raw_usage(row)
    seconds = plan.window_seconds("session")
    if plan.provider == "codex":
        seconds = codex_observed_seconds(raw).get("session", seconds)
    reported, authoritative, drift = _session_evidence(plan, raw)
    state, temporary, note, decided = _decide_session(plan, row, reported, authoritative)
    for code in decided:
        _pkg._note_drift(plan.provider, plan.plan_key, code, _SESSION_DRIFT_DETAIL[code])
    return SessionWindow(state, temporary, seconds, note, tuple(drift + decided))


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
        if window_length_ok(mins) and window_length_ok(mins * 60):
            return int(mins * 60)
    if slot not in WINDOW_KINDS:
        slot = "weekly_model"
    if plan is not None:
        return plan.window_seconds(slot)
    return _pkg.window_seconds(provider, slot)
