"""One account's plan and window facts, and the payload shapes built from them.

:func:`account_view` resolves a row once; the API, the WebSocket payloads,
the menu bar, pacing and auto-swap all read the same view, so they cannot
disagree about a window. :func:`project_row` is the row as every decision
must read it (no 5-hour value on an account without a 5-hour window).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from jacked import usage_plans as _pkg

from .catalog_file import _BUILTIN_WINDOWS, _MISSING
from .resolve import (
    PlanMatch,
    SessionWindow,
    _codex_windows,
    codex_observed_seconds,
    raw_usage,
    resolve_plan,
    session_window,
)


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
            "catalog_verified_at": _pkg.catalog().verified_at,
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


def _codex_weekly_state(raw) -> str:
    """present / absent / unknown for a Codex account's weekly window."""
    norm = _codex_windows(raw)
    if norm is None:
        return "present"
    if norm["seven_day"]["reported"]:
        return "present"
    return "absent" if norm["windows_complete"] else "unknown"


def account_view(row: Mapping, raw: Any = _MISSING) -> AccountView:
    """Resolve plan and windows for one account row (parses its payload once)."""
    if raw is _MISSING:
        raw = raw_usage(row)
    plan = resolve_plan(row)
    session = session_window(row, raw, plan)
    weekly_state = "present"
    weekly_seconds = plan.window_seconds("weekly")
    if plan.provider == "codex":
        weekly_seconds = codex_observed_seconds(raw).get("weekly", weekly_seconds)
        weekly_state = _codex_weekly_state(raw)
    return AccountView(
        row=row, plan=plan, session=session,
        weekly_state=weekly_state, weekly_seconds=weekly_seconds,
    )


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
