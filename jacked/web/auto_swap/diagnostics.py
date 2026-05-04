"""Diagnostic helpers — labels, tier-aware threshold, and the 7d-deficit summary."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from jacked.web.auto_swap.burn import compute_effective_working_hours


def tier_label(account: dict) -> str:
    """Return a human-readable tier label like '(tier 20x)' or ''."""
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    return f" (tier {match.group(1)}x)" if match else ""


def format_account_label(account: dict) -> str:
    """Human-readable account label for swap notifications and logs.

    Format: [Label — ] email [(org)]
    - Personal orgs (ending "'s Organization") show as "(personal)"
    - Real org names shown as-is: "(Hank.ai)"
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
        # e.g. "Jack" for jack.neil@hank.ai. Don't show these.
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


def compute_7d_deficit(
    account: dict,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> dict | None:
    """Compute 7-day utilization deficit for an account.

    Returns dict with deficit, effective_hours_remaining,
    effective_windows_remaining, unused_7d. Or None if insufficient data.

    Deficit > 0 means behind schedule (underutilized, wasting capacity).
    """
    resets_at_str = account.get("cached_7d_resets_at")
    usage_7d = account.get("cached_usage_7d")

    if resets_at_str is None or usage_7d is None:
        return None

    try:
        resets_at_utc = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at_utc.tzinfo is None:
            resets_at_utc = resets_at_utc.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    now_utc = datetime.now(timezone.utc)
    if resets_at_utc <= now_utc:
        return None  # window already expired

    # Convert to local time for working-hours calculation.
    # Uses rough offset: diff between datetime.now() (local naive) and
    # datetime.now(timezone.utc) stripped of tzinfo. Avoids pytz/zoneinfo.
    from datetime import timedelta as _td

    now_local = datetime.now()
    now_utc_naive = now_utc.replace(tzinfo=None)
    utc_offset_seconds = (now_utc_naive - now_local).total_seconds()
    # Convert resets_at to local by subtracting the UTC offset
    resets_local = resets_at_utc.replace(tzinfo=None) - _td(
        seconds=utc_offset_seconds
    )
    window_start_local = resets_local - _td(days=7)

    # Elapsed and total working hours
    elapsed_hours = compute_effective_working_hours(
        window_start_local, now_local, active_start, active_end,
    )
    total_hours = compute_effective_working_hours(
        window_start_local, resets_local, active_start, active_end,
    )

    if total_hours <= 0:
        return None

    elapsed_fraction = min(elapsed_hours / total_hours, 1.0)
    expected_usage = elapsed_fraction * 100.0
    deficit = expected_usage - usage_7d

    # Remaining capacity
    remaining_hours = compute_effective_working_hours(
        now_local, resets_local, active_start, active_end,
    )
    remaining_windows = remaining_hours / 5.0

    return {
        "deficit": deficit,
        "effective_hours_remaining": remaining_hours,
        "effective_windows_remaining": remaining_windows,
        "unused_7d": 100.0 - usage_7d,
    }
