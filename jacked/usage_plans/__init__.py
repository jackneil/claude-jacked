"""Usage-window catalog: which limits each provider plan has, and its name.

Providers change their plans without notice. On 2026-10-30 Anthropic renamed
Max 5x to Max and Max 20x to Ultra, and removed the 5-hour limit for verified
Ultra accounts. ChatGPT Pro has no 5-hour limit in Codex. This package reads
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
- ``unknown_limit_kind``: the usage payload lists a limit kind jacked does
  not know (its silence about a session window then proves nothing).
- ``renamed``: the provider calls the plan something the catalog does not.

Session policies (``session`` in the catalog):

- ``always``: the plan has a 5-hour window. Claude silence means no window is
  open right now (idle), which the window keeper may open.
- ``never``: the plan has no 5-hour window.
- ``until_verified``: the provider removes the window once it verifies the
  account (Ultra). A reported window is temporary.
- ``reported``: no fixed expectation. An authoritative payload decides; Claude
  silence without one keeps the legacy "idle" reading.

Layout: :mod:`.catalog_file` loads and validates the file (stateless),
:mod:`.resolve` matches a row to its plan and its 5-hour window, and
:mod:`.view` builds the per-account view and payloads. This module holds the
process state (the cached catalog and the drift log) and the public names.
Stdlib only at import time (the Codex payload parser is imported lazily), so
any jacked surface can use it without pulling in the web stack.
"""

from __future__ import annotations

from typing import Optional

from .catalog_file import (
    _BUILTIN_WINDOWS,
    CATALOG_FILE,
    DATA_ROOT,
    GENERIC_NO_SESSION_NOTE,
    MAX_WINDOW_SECONDS,
    SESSION_POLICIES,
    SUPPORTED_VERSION,
    WINDOW_KINDS,
    Catalog,
    PlanEntry,
    ProviderCatalog,
    SubscriptionEntry,
    clean_label,
    load_catalog,
    logger,
    provider_key,
    window_length_ok,
)

_catalog_cache: Optional[Catalog] = None
_drift_logged: set = set()
_DRIFT_LOG_MAX = 512


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
    pcat = catalog().providers.get(provider_key(provider))
    if pcat is None:
        return _BUILTIN_WINDOWS[kind]
    return int(pcat.windows.get(kind, _BUILTIN_WINDOWS[kind]))


def _note_drift(provider: str, plan_key: str, code: str, detail: str) -> None:
    """Log one WARNING per (provider, plan, code, detail) per process."""
    key = (provider, plan_key, code, detail)
    if key in _drift_logged:
        return
    if len(_drift_logged) >= _DRIFT_LOG_MAX:
        # A payload that varies its shape on every read must not grow this
        # set without bound; the first shapes are already logged.
        logger.debug("Plan catalog drift (%s) for %s plan %r", code, provider, plan_key)
        return
    _drift_logged.add(key)
    logger.warning(
        "Plan catalog drift (%s) for %s plan %r: %s. This jacked release's plan "
        "catalog (verified %s) may be out of date; upgrade jacked when a newer "
        "release is available.",
        code, provider, plan_key, detail, catalog().verified_at or "unknown",
    )


# The submodules read the state above through this package at call time, so
# they are imported after it.
from .resolve import (  # noqa: E402
    KNOWN_CLAUDE_LIMIT_KINDS,
    PlanMatch,
    SessionWindow,
    has_session,
    model_window_seconds,
    raw_usage,
    resolve_plan,
    session_window,
)
from .view import AccountView, account_view, project_row  # noqa: E402

__all__ = [
    "CATALOG_FILE", "DATA_ROOT", "GENERIC_NO_SESSION_NOTE", "KNOWN_CLAUDE_LIMIT_KINDS",
    "MAX_WINDOW_SECONDS", "SESSION_POLICIES", "SUPPORTED_VERSION", "WINDOW_KINDS",
    "clean_label", "window_length_ok",
    "AccountView", "Catalog", "PlanEntry", "PlanMatch", "ProviderCatalog",
    "SessionWindow", "SubscriptionEntry",
    "account_view", "catalog", "has_session", "load_catalog", "model_window_seconds",
    "project_row", "raw_usage", "resolve_plan", "session_window", "window_seconds",
]
