"""Diagnostic helpers — labels, tier-aware threshold, and the 7d-deficit summary."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from jacked.web.auto_swap.burn import (
    compute_burn_per_window,
    compute_effective_working_hours,
)

from .tiers import (
    TIER_EXCLUDED,
    deficit_vs_target,
    target_7d,
    tier_for,
    white_bar,
)


def tier_label(account: dict) -> str:
    """Return a human-readable tier label like '(tier 20x)' or ''."""
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    return f" (tier {match.group(1)}x)" if match else ""


def format_account_label(account: dict) -> str:
    """Human-readable account label for swap notifications and logs.

    Format: [Label — ] email [(org)]
    - Personal orgs (ending "'s Organization") show as "(personal)"
    - Real org names shown as-is: "(Acme)"
    - Custom display_name prepended only if it differs from the default
      (default = first name matching email prefix, or generic names)
    """
    email = account.get("email") or "unknown"
    org_name = account.get("organization_name") or ""
    display_name = (account.get("display_name") or "").strip()

    # Build org suffix
    org_suffix = ""
    if org_name:
        if org_name.endswith("\u2019s Organization") or org_name.endswith("'s Organization"):
            org_suffix = " (personal)"
        else:
            org_suffix = f" ({org_name})"

    # Check if display_name is custom (not the Anthropic default)
    label_prefix = ""
    if display_name:
        # Default display_name is typically the first name from the email
        # e.g. "Jack" for user1@example.com. Don't show these.
        email_prefix = email.split("@")[0].split(".")[0].lower()
        if display_name.lower() != email_prefix and display_name.lower() != "user":
            label_prefix = f"{display_name} \u2014 "

    return f"{label_prefix}{email}{org_suffix}"


def tier_critical_threshold(account: dict) -> float:
    """Compute the auto-swap critical threshold based on account tier.

    Higher-tier accounts can sustain usage longer before needing a swap.
    Known tiers from Anthropic: default_claude_max_20x, default_claude_max_5x.
    Test fixtures use t1/t2 which are not real tiers — they fall to the
    subscription_type fallback.
    """
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    if match:
        multiplier = int(match.group(1))
        if multiplier >= 20:
            return 95.0
        if multiplier >= 5:
            return 90.0
    # Fallback: subscription_type when tier is missing or unrecognized
    sub = (account.get("subscription_type") or "").lower()
    if sub == "max":
        return 90.0  # max without tier info — conservative
    return 80.0  # pro, free, or unknown


def achievable_burn(
    account: dict,
    now: datetime | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float | None:
    """Max 7d capacity (%) still burnable before this account's 7d expiry.

    Remaining effective working hours (local time, within active hours)
    between now and expiry, in 5h windows, times burn-per-window. 0.0 when
    already expired; None when cached_7d_resets_at is missing or
    unparseable.
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # astimezone() (not wall-clock offset math) so a frozen ``now``
    # converts to local time deterministically.
    now_local = now.astimezone().replace(tzinfo=None)
    expiry_local = resets_at.astimezone().replace(tzinfo=None)
    remaining_hours = compute_effective_working_hours(
        now_local, expiry_local, active_start, active_end,
    )
    return (remaining_hours / 5.0) * compute_burn_per_window(
        active_start, active_end,
    )


def stranding_estimate(
    account: dict,
    now: datetime | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float | None:
    """Projected 7d capacity (%) that will expire unused.

    max(0, deficit_vs_target - achievable_burn): the part of the deficit
    the remaining working hours can no longer absorb. None when either
    the deficit or the achievable burn is unavailable.
    """
    deficit = deficit_vs_target(
        account, now=now, active_start=active_start, active_end=active_end,
    )
    burn = achievable_burn(
        account, now=now, active_start=active_start, active_end=active_end,
    )
    if deficit is None or burn is None:
        return None
    return max(0.0, deficit - burn)


def compute_7d_deficit(
    account: dict,
    active_start: str = "06:00",
    active_end: str = "23:00",
    now: datetime | None = None,
) -> dict | None:
    """Diagnostic dict for 7d utilization status of an account.

    Returns dict with: tier, target_7d, deficit_vs_tier_target,
    white_bar, hours_to_expiry, unused_7d, plus legacy fields
    (deficit, effective_hours_remaining, effective_windows_remaining)
    for callers that haven't migrated yet.

    None when 7d data missing or window expired.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    tier = tier_for(account, now=now)
    if tier == TIER_EXCLUDED:
        return None

    resets_at_str = account.get("cached_7d_resets_at")
    usage_7d = account.get("cached_usage_7d")
    if resets_at_str is None or usage_7d is None:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    hours_to_expiry = (resets_at - now).total_seconds() / 3600.0
    wb = white_bar(account, now=now)
    target = target_7d(account, now, active_start, active_end)
    deficit_vs_target_val = (target - usage_7d) if target is not None else 0.0
    deficit_vs_white_bar = (wb * 100.0 - usage_7d) if wb is not None else 0.0

    # Legacy (effective working hours) — kept for analytics/backcompat.
    from datetime import timedelta as _td
    now_local = datetime.now()
    now_utc_naive = now.replace(tzinfo=None)
    utc_offset_seconds = (now_utc_naive - now_local).total_seconds()
    resets_local = resets_at.replace(tzinfo=None) - _td(seconds=utc_offset_seconds)
    remaining_hours = compute_effective_working_hours(
        now_local, resets_local, active_start, active_end,
    )
    remaining_windows = remaining_hours / 5.0

    return {
        "tier": tier,
        "target_7d": target,
        "deficit_vs_tier_target": deficit_vs_target_val,
        "white_bar": wb,
        "hours_to_expiry": hours_to_expiry,
        "unused_7d": 100.0 - usage_7d,
        # Legacy fields (callers in flight migration)
        "deficit": deficit_vs_white_bar,
        "effective_hours_remaining": remaining_hours,
        "effective_windows_remaining": remaining_windows,
    }
