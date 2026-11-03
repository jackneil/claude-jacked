"""Background usage monitor — two independent loops for active-account
polling and full-sweep (window keeper + bulk usage refresh).

Started from main.py lifespan as separate asyncio tasks.  Each loop has
its own ``while True`` with ``try/except`` — one loop crashing does NOT
affect the other.  Both read settings from DB each tick so changes take
effect without restart.

The window keeper (the full sweep) lives in :mod:`jacked.api.keeper`. This
module re-exports the keeper names that callers import from it.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jacked.api.keeper_confirmation import (
    _KEEPER_CONFIRM_MIN_SECONDS as _KEEPER_CONFIRM_MIN_SECONDS,
    _PENDING_REASON_TEXT as _PENDING_REASON_TEXT,
    _confirm_pending as _confirm_pending,
    _confirm_pending_from_poll,
    _keeper_usage_read as _keeper_usage_read,
    _pending_reason as _pending_reason,
    _read_landed,
    _window_phrase as _window_phrase,
    _window_read_after as _window_read_after,
)

# The window keeper (loop 2, the full sweep) lives in jacked.api.keeper. The
# names below stay importable from this module, and each one is the keeper
# module's own object. A patch on this module does not reach the keeper code:
# patch the keeper module that reads the name. There is no copy of the sweep
# wake Event here. keeper.state.reset_locks rebinds it, so a copy would go
# stale: read it through jacked.api.keeper.state.sweep_wake().
from jacked.api.keeper.auth_ladder import (
    _KEEPER_MAX_7D_RESET_SECONDS as _KEEPER_MAX_7D_RESET_SECONDS,
    _KEEPER_MAX_RETRY_SECONDS as _KEEPER_MAX_RETRY_SECONDS,
    _ROTATION_TERMINAL_ERRORS as _ROTATION_TERMINAL_ERRORS,
    _LadderOutcome as _LadderOutcome,
    _candidate_fingerprint as _candidate_fingerprint,
    _keeper_auth_ladder as _keeper_auth_ladder,
    _keeper_candidates as _keeper_candidates,
    _keeper_primary as _keeper_primary,
    _rotation_is_transient as _rotation_is_transient,
)
from jacked.api.keeper.confirm import (
    _bounded_reset as _bounded_reset,
    _keeper_record_success as _keeper_record_success,
    _keeper_window_needed as _keeper_window_needed,
    _ping_window_cache_fields as _ping_window_cache_fields,
    _window_confirmed as _window_confirmed,
)
from jacked.api.keeper.process import (
    _keeper_process_account as _keeper_process_account,
    _keeper_record_failure as _keeper_record_failure,
)
from jacked.api.keeper.state import (
    _KEEPER_PING_PACING_SECONDS as _KEEPER_PING_PACING_SECONDS,
    _PING_BACKOFF_CAP_SECONDS as _PING_BACKOFF_CAP_SECONDS,
    _PING_BACKOFF_MAX_DOUBLINGS as _PING_BACKOFF_MAX_DOUBLINGS,
    _PING_BACKOFF_MAX_FAILS as _PING_BACKOFF_MAX_FAILS,
    _PREWAKE_DONE_KEY as _PREWAKE_DONE_KEY,
    _RESTORED_SCHEDULE_STATUSES as _RESTORED_SCHEDULE_STATUSES,
    _SWEEP_SLEEP_SLICE_SECONDS as _SWEEP_SLEEP_SLICE_SECONDS,
    _arm_ping_backoff as _arm_ping_backoff,
    _backoff_step as _backoff_step,
    _keeper_state_of as _keeper_state_of,
    _note_keeper_skip as _note_keeper_skip,
    _ping_backoff as _ping_backoff,
    _restore_ping_backoff as _restore_ping_backoff,
    _sync_backoff as _sync_backoff,
    _write_keeper_state as _write_keeper_state,
    clear_ping_backoff as clear_ping_backoff,
)
from jacked.api.keeper.sweep import (
    _next_active_start_epoch as _next_active_start_epoch,
    _note_keeper_disabled as _note_keeper_disabled,
    _run_window_keeper as _run_window_keeper,
    _sleep_until as _sleep_until,
    full_sweep_loop as full_sweep_loop,
)

if TYPE_CHECKING:
    from jacked.web.auto_swap import BurnRate

logger = logging.getLogger(__name__)

# Module-level state — shared between loops.
# Only the active poll loop writes to _burn_rates.
_burn_rates: dict[int, "BurnRate"] = {}
_last_exhaustion_warning: float = 0.0
_EXHAUSTION_COOLDOWN_SECONDS = 1800  # 30 minutes
_last_swap_time: float = 0.0
_SWAP_COOLDOWN_SECONDS = 300  # 5 minutes between swaps to prevent ping-ponging

# Wall-clock time of the last COMMITTED swap (credential write succeeded)
# or manual switch (see note_external_swap). Feeds the min-residency gate
# and the residency_seconds audit column on swap_log rows.
_last_committed_swap_time: float = 0.0
# Proactive reasons (higher-tier / intra-tier / burn-rate) must let the
# active account settle this long before another departure. Forced
# departures (drained, 5h critical, a weekly or model weekly wall) are
# exempt, and so is any departure off a walled active account, whichever
# reason wins (#224i).
_MIN_RESIDENCY_SECONDS = 900

# Exponential backoff for failed swap attempts (credential write
# failures; TOCTOU aborts bail before the attempt is recorded). Window:
# base * 2**(count-1), capped. Replaces the old retry-next-tick behavior;
# the regular swap cooldown only governs committed swaps.
_swap_failure_count: int = 0
_last_swap_failure_at: float = 0.0
_SWAP_FAILURE_BACKOFF_BASE_SECONDS = 60.0
_SWAP_FAILURE_BACKOFF_CAP_SECONDS = 600.0

# Drain advisor: per-account cooldown for 'expiring_with_stranded_capacity'
# broadcasts. Advisor only — no auto-burn worker; routing real work beats
# burning quota on no-ops.
_drain_advisor_last_sent: dict[int, float] = {}
_DRAIN_ADVISOR_COOLDOWN_SECONDS = 1800
_DRAIN_ADVISOR_STRANDING_THRESHOLD = 2.0

# Same-tier-deficit advisory (split out of stall pattern d): per spec,
# same-tier-never-overrides is intended behavior, so it must not page at
# ERROR — it gets its own INFO + WS advisory with an independent cooldown.
_same_tier_advisory_last: float = 0.0
_SAME_TIER_ADVISORY_COOLDOWN_SECONDS = 1800
_SAME_TIER_DEFICIT_THRESHOLD = 15.0

# Track consecutive unchanged ticks per account for burn-rate decay.
_burn_rate_unchanged_ticks: dict[int, int] = {}
_initial_fetch_done = False
_ticks_since_prune = 0

# Per-account last-observed tier for hysteresis. Persists across ticks;
# cleared when an account is removed or a swap occurs to/from it.
_last_observed_tiers: dict[int, int] = {}

# Anti-jitter: require an emerged TIER (higher-tier or intra-tier reason)
# to persist across ``_EMERGENCE_PERSISTENCE_TICKS`` consecutive ticks
# before swapping. Keyed by tier rather than account id — two near-tied
# candidates in the same tier alternating as best must not reset each
# other's streak. Shape: {"tier": int | None, "count": int}.
_emerged_tier_streak: dict = {"tier": None, "count": 0}
_EMERGENCE_PERSISTENCE_TICKS = 2

# Silent-stall watchdog state.
_consecutive_no_best_ticks: int = 0
_last_stall_warning: float = 0.0
_STALL_TICK_THRESHOLD = 10
_STALL_USAGE_STALENESS_SECONDS = 1800
_STALL_WARNING_COOLDOWN_SECONDS = 1800


def reset_locks() -> None:
    """Rebind module-level asyncio primitives + clear per-account state.

    See jacked.api.routes.auth.reset_locks for the full explanation of
    why in-process tray restarts require this. The keeper state (the
    sweep wake Event and the ping backoff) is not reset here: it has its
    own owner, jacked.api.keeper.state, whose ``reset_locks`` the
    lifespan calls separately.

    Tier hysteresis and emergence streak counters depend on consecutive
    ticks; a lifespan restart resets the count so we observe fresh data
    from scratch instead of acting on remembered tiers from before the
    restart.
    """
    _last_observed_tiers.clear()
    _emerged_tier_streak["tier"] = None
    _emerged_tier_streak["count"] = 0
    global _consecutive_no_best_ticks, _last_stall_warning
    _consecutive_no_best_ticks = 0
    _last_stall_warning = 0.0
    global _swap_failure_count, _last_swap_failure_at
    global _last_committed_swap_time, _same_tier_advisory_last
    _swap_failure_count = 0
    _last_swap_failure_at = 0.0
    _last_committed_swap_time = 0.0
    _same_tier_advisory_last = 0.0
    _drain_advisor_last_sent.clear()


def note_external_swap() -> None:
    """Record a credential switch performed OUTSIDE the auto-swap loop
    (manual switch in routes/auth.py).

    Arms the swap cooldown and the min-residency clock, and clears the
    emergence streak so the loop re-observes from scratch instead of
    immediately swapping away from the account the user just chose.
    """
    global _last_swap_time, _last_committed_swap_time
    now_ts = time.time()
    _last_swap_time = now_ts
    _last_committed_swap_time = now_ts
    _emerged_tier_streak["tier"] = None
    _emerged_tier_streak["count"] = 0


def _swap_backoff_remaining(now: float | None = None) -> float:
    """Seconds remaining in the exponential failure-backoff window.

    0.0 when no failures are recorded or the window has elapsed.
    Window: ``_SWAP_FAILURE_BACKOFF_BASE_SECONDS * 2**(count-1)``, capped
    at ``_SWAP_FAILURE_BACKOFF_CAP_SECONDS``.
    """
    if _swap_failure_count <= 0:
        return 0.0
    window = min(
        _SWAP_FAILURE_BACKOFF_CAP_SECONDS,
        _SWAP_FAILURE_BACKOFF_BASE_SECONDS * (2 ** (_swap_failure_count - 1)),
    )
    elapsed = (now if now is not None else time.time()) - _last_swap_failure_at
    return max(0.0, window - elapsed)


def _read_active_account_id() -> int | None:
    """Return the active identity the authority store resolves, else None.

    Required-mirror drift is evidence on the observation, not a veto, so this
    reports what the runtime will actually use.
    """
    try:
        from jacked.credentials.resolver import ResolverState
        from jacked.credentials.runtime import resolve_active_identity

        observation = resolve_active_identity()
        if observation.state is ResolverState.RESOLVED:
            return observation.identity.account_id
    except (OSError, RuntimeError):
        return None
    return None


class _Snapshot:
    """Settings read in ONE statement (Database.get_settings), with the
    ``get_setting`` shape the _setting_* readers expect. Related settings
    (a threshold pair, a window's start and end) read through one snapshot
    can never mix two saves made by another thread."""

    def __init__(self, db, keys) -> None:
        self._values = db.get_settings(list(keys))

    def get_setting(self, key: str):
        return self._values[key]


def _setting_bool(db, key: str, default: bool = False) -> bool:
    """Read a boolean setting from DB (stored as 'true'/'false' strings)."""
    val = db.get_setting(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _setting_float(db, key: str, default: float) -> float:
    """Read a float setting from DB."""
    val = db.get_setting(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _setting_str(db, key: str, default: str) -> str:
    """Read a string setting from DB."""
    val = db.get_setting(key)
    return val if val is not None else default


def _pct_text(value) -> str:
    """A usage percent for a log line; "n/a" when there is no reading.

    A null 5h value is an account without a 5-hour window (or no reading
    yet), never 0% used.

    >>> _pct_text(95), _pct_text(None), _pct_text("junk")
    ('95.0%', 'n/a', 'n/a')
    """
    from jacked.service.usage_pacing import coerce_pct

    pct = coerce_pct(value)
    return "n/a" if pct is None else f"{pct:.1f}%"


def _apply_emergence_persistence(
    reason: str | None,
    best_tier: int | None,
    streak: dict,
    persistence_ticks: int,
) -> str | None:
    """Gate emerge reasons (higher-tier or intra-tier) behind a multi-tick
    streak keyed by the BEST candidate's TIER, not its account id.

    Anti-jitter: the emerged tier must remain the best's tier
    ``persistence_ticks`` times in a row before the swap actually fires.
    Tier-keyed because two near-tied candidates in the same tier
    alternating as best would reset each other's id-keyed streak forever.
    Mutates ``streak`` ({"tier": int | None, "count": int}) in place.

    Streak handling rules:

    - ``reason`` starts with ``REASON_PREFIX_HIGHER_TIER`` or
      ``REASON_PREFIX_INTRA_TIER`` and ``best_tier`` is set: increment the
      count when ``best_tier`` equals the tracked tier (regardless of which
      account is best), else restart the streak at 1 for the new tier.
      Return reason if the count met the threshold, else None.
    - ``reason`` is None and ``best_tier`` is None (transient absence,
      e.g. _fetch_candidate_usage hiccup): PRESERVE the streak. A
      single-tick candidate fetch glitch should not reset the 2-minute
      clock. Next tick with the same emerged tier resumes incrementing.
      Return None.
    - ``reason`` is set but is NOT an emerge reason (drained / weekly or
      model weekly wall / 5h critical / burn-rate), or is None with a
      classified candidate present: clear
      the streak (different decision path now). Return reason.
    - emerge ``reason`` with ``best_tier`` missing: shouldn't normally
      occur because `should_swap_now` only emits emerge reasons when best
      is non-None. Defensive clear + return None.

    Returns the original ``reason`` when persistence is met (let the
    swap fire), or None to suppress this tick.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_INTRA_TIER,
    )

    if reason is None and best_tier is None:
        # Transient: no candidate, no swap reason. Preserve streak so a
        # single-tick fetch glitch doesn't restart the persistence clock.
        return None
    is_emerge = reason is not None and reason.startswith(
        (REASON_PREFIX_HIGHER_TIER, REASON_PREFIX_INTRA_TIER),
    )
    if not is_emerge:
        # Explicit non-emerge outcome — drained/critical/burn-rate fired
        # OR active was decisively classified as no-swap-needed. Clear.
        streak["tier"] = None
        streak["count"] = 0
        return reason
    if best_tier is None:
        # Defensive: should not happen (emerge reasons require best != None).
        streak["tier"] = None
        streak["count"] = 0
        return None
    if streak["tier"] == best_tier:
        streak["count"] += 1
    else:
        streak["tier"] = best_tier
        streak["count"] = 1
    if streak["count"] < persistence_ticks:
        return None
    return reason


def _evaluate_stall(
    *,
    decision_action: str,
    best: dict | None,
    usage_cached_at_age_seconds: int,
    has_other_accounts: bool,
    reason: str | None,
    wall: str | None,
    staleness_threshold: int,
) -> bool:
    """Return True if this tick qualifies as a stall pattern.

    Three patterns trigger a bump (any one):
      (a) Multi-account stale: stay+no-best+stale active+has others
      (b) Single-account forced-out: only one account, departure reason fired
      (c) Drained-no-candidate: any reason fired but no eligible target

    A walled active account (``wall``: rule 2a or 2b holds, whichever
    reason won) with a fresh usage reading is exhaustion, not a stall:
    patterns (b) and (c) skip it. The ``no_target`` decision, the
    exhaustion WARNING and the ``all_accounts_exhausted`` banner report
    that state already, and a weekly wall can hold for days. A reading
    older than ``staleness_threshold`` proves nothing about the wall, so
    every pattern applies to it. A drained account under its wall keeps
    patterns (b) and (c).

    Same-tier-stay-with-deficit (the old pattern d) is NOT a stall — per
    spec, same-tier-never-overrides is intended behavior, so flagging it
    at ERROR trains the operator to ignore the watchdog. It lives in the
    separate ``_same_tier_advisory_applies`` advisory path instead.

    Returns False otherwise (caller resets the counter).
    """
    if decision_action != "stay":
        return False
    stale = usage_cached_at_age_seconds > staleness_threshold
    exhausted = wall is not None and not stale
    forced_out = reason is not None and not exhausted
    pattern_a = best is None and stale and has_other_accounts
    pattern_b = not has_other_accounts and forced_out
    pattern_c = best is None and forced_out
    return pattern_a or pattern_b or pattern_c


def _same_tier_advisory_applies(
    *,
    decision_action: str,
    best: dict | None,
    reason: str | None,
    best_deficit: float | None,
    best_tier: int | None,
    threshold: float = _SAME_TIER_DEFICIT_THRESHOLD,
) -> bool:
    """Advisory condition (split from stall pattern d): a candidate exists
    but same-tier-never-overrides keeps the loop on stay even though the
    candidate is materially behind its tier target.

    Gated on the candidate tier being T0 or T1 — those are harvestable
    drain-to targets; T2/T3 targets are floors, so sitting behind them is
    not actionable. With intra-T0 preemption implemented, T0-vs-T0 cases
    may legitimately swap; this advisory covers the remaining
    same-tier-stay cases. Caller applies the 30-min cooldown.
    """
    from jacked.web.auto_swap import TIER_T0, TIER_T1

    return (
        decision_action == "stay"
        and best is not None
        and reason is None
        and best_deficit is not None
        and best_deficit >= threshold
        and best_tier in (TIER_T0, TIER_T1)
    )


def _trigger_for_reason(reason: str | None) -> str:
    """Map a should_swap_now reason-string to a decision-log trigger
    taxonomy value. Stable contract — see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_INTRA_TIER,
        REASON_PREFIX_DRAINED,
        REASON_PREFIX_WEEKLY_CRITICAL,
        REASON_PREFIX_MODEL_WEEKLY,
        REASON_PREFIX_FIVE_H,
        REASON_PREFIX_BURN_RATE,
    )

    if reason is None:
        return "tick"
    if reason.startswith(REASON_PREFIX_HIGHER_TIER):
        return "higher_tier_emerged"
    if reason.startswith(REASON_PREFIX_INTRA_TIER):
        return "intra_tier_preempted"
    if reason.startswith(REASON_PREFIX_DRAINED):
        return "tier_drained"
    if reason.startswith(REASON_PREFIX_WEEKLY_CRITICAL):
        return "weekly_critical"
    if reason.startswith(REASON_PREFIX_MODEL_WEEKLY):
        return "model_weekly_critical"
    if reason.startswith(REASON_PREFIX_FIVE_H):
        return "forced_critical"
    if reason.startswith(REASON_PREFIX_BURN_RATE):
        return "burn_rate"
    return "tier_aware"


# -----------------------------------------------------------------------
# Loop 1 — Active account poll (60s)
# -----------------------------------------------------------------------


def _project_rows(rows) -> list[dict]:
    """Account rows as every polling and auto-swap decision must read them.

    The cache keeps a 5h reading until a payload clears it, so an account
    without a 5-hour window (a verified Ultra, ChatGPT Pro) can still carry a
    stale 5h=100 and its reset. :func:`jacked.usage_plans.project_row` drops
    both; any other row comes back unchanged. Applied once where the rows are
    read from the database, so the departure rules, the candidate pick, the
    poll clamps, the burn rate and the recovery time all see the same row.
    A projected row is a copy: never write one back to the database. A row
    the resolver cannot read is kept as stored, with a WARNING: jacked never
    drops a window it cannot rule out.
    """
    from jacked.usage_plans import project_row

    out: list[dict] = []
    for row in rows:
        try:
            out.append(project_row(row))
        except Exception:
            logger.warning(
                "Plan resolver failed for account %s; its decisions read the "
                "cached 5h value as stored",
                row.get("id") if isinstance(row, dict) else "?",
                exc_info=True,
            )
            out.append(row)
    return out


def _list_decision_accounts(db) -> list[dict]:
    """The enabled accounts, projected for decisions (see _project_rows).

    Sync (a DB read plus payload parsing): the poll loop runs it off the
    event loop.
    """
    return _project_rows(db.list_accounts(include_inactive=False))


def urgency_inputs(row: dict | None) -> dict:
    """The account-dependent arguments of ``compute_urgency_tier`` for a row.

    ``usage_5h`` / ``usage_7d`` come from the projected row (no 5h value on
    an account without a 5-hour window); ``has_session`` is False only for an
    absent window; ``usage_model`` is the utilization of the binding
    model-weekly limit (for example the weekly Fable limit), or None when the
    payload reports none. A missing row reads as an idle account with a
    window. Parses the cached payload once.

    >>> ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
    ...          "cached_usage_5h": 100.0, "cached_usage_7d": 40.0,
    ...          "cached_usage_raw": '{"limits": [{"kind": "weekly_all", "percent": 40}]}'}
    >>> urgency_inputs(ultra)
    {'usage_5h': None, 'usage_7d': 40.0, 'has_session': False, 'usage_model': None}
    >>> urgency_inputs(None)["has_session"]
    True
    """
    from jacked.service.menubar_summary import binding_model, parse_per_model
    from jacked.service.usage_pacing import coerce_pct
    from jacked.usage_plans import account_view, raw_usage

    if not row:
        return {"usage_5h": None, "usage_7d": None, "has_session": True, "usage_model": None}
    raw = raw_usage(row)
    view = account_view(row, raw)
    projected = view.projected_row()
    model = binding_model(parse_per_model(raw))
    return {
        "usage_5h": projected.get("cached_usage_5h"),
        "usage_7d": projected.get("cached_usage_7d"),
        "has_session": view.session.state != "absent",
        "usage_model": coerce_pct(model.get("utilization")) if model else None,
    }


def _poll_interval_inputs(active_id: int | None, db) -> tuple | None:
    """The DB reads behind ``_compute_poll_interval``: (urgency_inputs of the
    active row, critical_5h). Sync, so the poll loop runs it off the event
    loop. None when there is nothing to read or a read fails (the interval
    falls back).
    """
    if active_id is None or db is None:
        return None
    try:
        return (
            urgency_inputs(db.get_account(active_id)),
            _setting_float(db, "auto_swap_5h_critical", 90),
        )
    except Exception:
        return None


def _compute_poll_interval(
    active_id: int | None,
    inputs: tuple | None,
    burn_rates: dict,
) -> tuple[float, str]:
    """Compute the adaptive poll interval and urgency tier.

    ``inputs`` is what ``_poll_interval_inputs`` read from the DB. This part
    stays on the event loop: it writes the per-account usage state that the
    fetch coroutines share.

    Returns (interval_seconds, tier_name). Falls back to (60, "unknown")
    on any error.
    """
    if active_id is None or inputs is None:
        return 60.0, "unknown"
    try:
        from jacked.web.auth import (
            compute_urgency_tier,
            _get_usage_state,
            _TIER_INTERVALS,
        )

        account_inputs, critical_5h = inputs
        br = burn_rates.get(active_id)
        state = _get_usage_state(active_id)
        tier, base = compute_urgency_tier(
            **account_inputs,
            burn_rate_5h=br.rate_5h_per_min if br else 0.0,
            critical_5h=critical_5h,
        )
        # Override: force idle if stuck in 429 cycle — stale data makes
        # urgency tiers unreliable, and polling faster is pointless.
        if state.get("consecutive_429s", 0) >= 3:
            tier = "idle"
            base = _TIER_INTERVALS["idle"]
        state["tier"] = tier
        state["interval"] = base
        jitter = base * 0.15
        interval = base + random.uniform(-jitter, jitter)
        return interval, tier
    except Exception:
        return 60.0, "unknown"


def _clamp_poll_interval(
    interval: float,
    accounts: list[dict],
    active_acct_id: int,
    now: datetime,
    active_start: str,
    active_end: str,
    prev_tiers: dict[int, int],
) -> float:
    """Clamp the adaptive poll interval when a T0 candidate is in play.

    - <= 90s whenever any otherwise-eligible NON-active account is TIER_T0
      with positive deficit — a draining deadline must not wait out a 5-min
      idle interval. A walled account (#224i, ``_cannot_serve``: the same
      test ``pick_best_target`` applies) is no target, so it never clamps.
    - <= 60s when such a T0 candidate's 5h window is saturated
      (>= ``_FIVE_H_HEADROOM_LIMIT``) and its 5h reset lands within the
      next poll interval — event-driven re-entry approximation: be on time
      for the reset instead of discovering it up to a full interval late.
      (The literal "excluded by _has_5h_headroom" condition is
      unsatisfiable here: headroom already admits accounts within
      RESET_SUPPRESS_MINUTES of reset, which dwarfs any poll interval.)

    ``accounts`` are the projected rows (``_project_rows``): an account
    without a 5-hour window has no 5h value, so it never takes the 60 s
    clamp.
    """
    from jacked.web.auto_swap import (
        TIER_T0,
        target_for_tier,
        tier_for,
    )
    from jacked.web.auto_swap.selection import _FIVE_H_HEADROOM_LIMIT
    from jacked.web.auto_swap.walls import _cannot_serve, _protected_model_key

    clamped = interval
    saturated_5h: list[dict] = []
    protected_key = _protected_model_key(accounts, active_acct_id)
    for a in accounts:
        if a["id"] == active_acct_id:
            continue
        if a.get("is_active") == 0 or a.get("is_deleted") == 1:
            continue
        if (a.get("consecutive_failures") or 0) >= 3:
            continue
        if a.get("validation_status") == "invalid":
            continue
        if a.get("cc_access_token") is None:
            continue
        if a.get("cc_refresh_failure_type") == "invalid_grant":
            continue  # same eligibility as select_target: no dead cc chain
        if a.get("auto_swap_enabled") == 0:
            continue
        tier = tier_for(a, now=now, prev_tier=prev_tiers.get(a["id"]))
        if tier != TIER_T0:
            continue
        target = target_for_tier(tier, a, now, active_start, active_end)
        usage_7d = a.get("cached_usage_7d")
        if target is None or usage_7d is None:
            continue
        if target - usage_7d <= 0:
            continue
        if _cannot_serve(a, protected_key, now):
            continue  # walled (#224i): pick_best_target never offers it
        clamped = min(clamped, 90.0)
        if (a.get("cached_usage_5h") or 0) >= _FIVE_H_HEADROOM_LIMIT:
            saturated_5h.append(a)

    for a in saturated_5h:
        resets_at = a.get("cached_5h_resets_at")
        if not resets_at:
            continue
        try:
            reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        remaining = (reset_dt - now).total_seconds()
        if 0 < remaining <= clamped:
            clamped = min(clamped, 60.0)
            break

    return clamped


_CANDIDATE_STALENESS_SECONDS = 600  # 10 minutes — non-active accounts rarely change
_CANDIDATE_FETCH_TIMEOUT_SECONDS = 30


def _candidate_staleness_override(acct: dict) -> bool:
    """True when a candidate's cached snapshot is provably stale and must
    be re-fetched regardless of ``_CANDIDATE_STALENESS_SECONDS``.

    Two conditions (either suffices):
    - cached 5h usage >= 90 but the 5h reset is already in the past —
      the saturation is stale; the account may now be the most usable
      candidate. Removes the 5-10 min blind spot right after a window
      reset, exactly when an account becomes most useful.
    - ``needs_7d_ping`` says the 7d window reset but the cache predates
      the reset (a fresh 7d window means a fresh tier classification).

    ``acct`` is a projected row (``_project_rows``): a stale 5h reading on
    an account without a 5-hour window never forces a re-fetch.
    """
    from jacked.web.window_keeper import needs_7d_ping

    if (acct.get("cached_usage_5h") or 0) >= 90:
        resets_at = acct.get("cached_5h_resets_at")
        if resets_at:
            try:
                reset_dt = datetime.fromisoformat(
                    resets_at.replace("Z", "+00:00"),
                )
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                if reset_dt <= datetime.now(timezone.utc):
                    return True
            except (ValueError, TypeError):
                pass
    return needs_7d_ping(
        acct.get("cached_7d_resets_at"),
        acct.get("usage_cached_at"),
    )


async def _fetch_candidate_usage(accounts: list, active_acct_id: int, db) -> list:
    """Fetch fresh usage for non-active candidate accounts with stale data.

    Only fetches accounts whose usage_cached_at is older than
    _CANDIDATE_STALENESS_SECONDS — unless ``_candidate_staleness_override``
    forces a refresh (past 5h reset with saturated cache, or 7d window
    reset). Non-active accounts rarely change, so there's no need to hit
    the API every tick. Each fetch is bounded by a 30s timeout and a
    catch-all so one bad candidate can't stall the pass.
    Returns the refreshed accounts list from DB, projected for decisions
    (``_project_rows``).
    """
    from jacked.web.auth import fetch_usage

    now = int(time.time())
    fetched = 0
    for acct in accounts:
        if acct["id"] == active_acct_id:
            continue
        if acct.get("validation_status") == "invalid":
            continue  # Don't waste API calls on invalid accounts
        cached_at = acct.get("usage_cached_at")
        if (
            cached_at
            and (now - int(cached_at)) < _CANDIDATE_STALENESS_SECONDS
            and not _candidate_staleness_override(acct)
        ):
            continue  # data is fresh enough
        try:
            read = await asyncio.wait_for(
                fetch_usage(acct["id"], db),
                timeout=_CANDIDATE_FETCH_TIMEOUT_SECONDS,
            )
            fetched += 1
            if _read_landed(read):
                await _confirm_pending_from_poll(db, acct["id"])
        except Exception:
            logger.warning(
                "Candidate usage: fetch for account %d failed or timed "
                "out — skipping this tick",
                acct["id"],
            )
        await asyncio.sleep(1)

    if fetched:
        logger.debug("Candidate usage: refreshed %d stale accounts", fetched)

    return await asyncio.to_thread(_list_decision_accounts, db)


async def _drain_advisor_tick(
    accounts: list[dict],
    now_utc: datetime,
    active_start: str,
    active_end: str,
    ws_registry,
    prev_tiers: dict[int, int],
) -> None:
    """Broadcast 'expiring_with_stranded_capacity' for every T0 account
    (including the active one) projected to strand more than
    ``_DRAIN_ADVISOR_STRANDING_THRESHOLD`` percent of 7d capacity.

    Per-account 30-min cooldown via ``_drain_advisor_last_sent``.
    Advisor only by design — no auto-burn worker; routing real work beats
    burning quota on no-ops.
    """
    from jacked.web.auto_swap import (
        TIER_T0,
        achievable_burn,
        deficit_vs_target,
        format_account_label,
        stranding_estimate,
        tier_for,
    )

    now_ts = time.time()
    for acct in accounts:
        tier = tier_for(acct, now=now_utc, prev_tier=prev_tiers.get(acct["id"]))
        if tier != TIER_T0:
            continue
        stranding = stranding_estimate(
            acct,
            now_utc,
            active_start,
            active_end,
        )
        if stranding is None or stranding <= _DRAIN_ADVISOR_STRANDING_THRESHOLD:
            continue
        last_sent = _drain_advisor_last_sent.get(acct["id"], 0.0)
        if now_ts - last_sent < _DRAIN_ADVISOR_COOLDOWN_SECONDS:
            continue
        _drain_advisor_last_sent[acct["id"]] = now_ts
        deficit = deficit_vs_target(
            acct,
            now=now_utc,
            active_start=active_start,
            active_end=active_end,
        )
        achievable = achievable_burn(
            acct,
            now_utc,
            active_start,
            active_end,
        )
        logger.info(
            "Drain advisor: account %d (%s) expires with ~%.1f%% stranded "
            "capacity (deficit=%.1f%%, achievable=%.1f%%, resets_at=%s)",
            acct["id"],
            acct.get("email", "?"),
            stranding,
            deficit or 0.0,
            achievable or 0.0,
            acct.get("cached_7d_resets_at"),
        )
        if ws_registry:
            await ws_registry.broadcast(
                "expiring_with_stranded_capacity",
                {
                    "account_id": acct["id"],
                    "email": acct.get("email", ""),
                    "label": format_account_label(acct),
                    "deficit": round(deficit, 1) if deficit is not None else None,
                    "achievable": (
                        round(achievable, 1) if achievable is not None else None
                    ),
                    "stranding": round(stranding, 1),
                    "resets_at": acct.get("cached_7d_resets_at"),
                },
            )


def _build_tick_detail(
    active_acct: dict,
    usage_5h: float | None,
    usage_7d: float | None,
    want_swap: bool,
    suppression: dict | None,
    escape_override: bool,
    candidates: list[dict] | None,
    proactive_target_id: int | None,
    cooldown_active: bool,
    decision: str,
    wall: str | None,
) -> dict:
    """Build the detail JSON for a decision log entry.

    ``wall`` is the active account's walled departure reason (rule 2a or
    2b), or None. The tick carries it apart from the departure reason,
    because rule 1 or 1b can win the reason on a walled account.
    """
    from jacked.web.auto_swap import format_account_label

    detail = {
        "active": {
            "id": active_acct.get("id"),
            "email": active_acct.get("email", ""),
            "label": format_account_label(active_acct),
            "5h": usage_5h,
            "7d": usage_7d,
        },
        "should_swap": want_swap,
        "escape_override": escape_override,
        "cooldown_active": cooldown_active,
        "decision": decision,
        "wall": wall,
    }
    if suppression:
        detail["suppression"] = suppression
    if candidates is not None:
        detail["candidates"] = candidates
    if proactive_target_id is not None:
        detail["proactive_target_id"] = proactive_target_id
    return detail


def _certified_auto_swap_engine(db):
    """Return an explicitly installed transaction engine, never a guess.

    The production Database intentionally has no such attribute today. A
    capability bootstrap may install one only after exact-build capability
    resolution. ``CredentialTransactionEngine.activate`` then rechecks the
    writer fence before and after the write. Until that exists, auto-swap is
    recommendation-only.
    """
    from jacked.credentials.transaction import CredentialTransactionEngine

    engine = getattr(db, "_certified_auto_swap_engine", None)
    return engine if isinstance(engine, CredentialTransactionEngine) else None


def _auto_swap_request(target: dict, db=None):
    """Build a background transaction request from one database account.

    Raises ``ForeignPrimaryTokens`` for a target whose primary tokens belong
    to another account (``build_oauth_data``): the caller skips the target.
    """
    import uuid

    from jacked.api.credential_helpers import build_oauth_data
    from jacked.credentials.canonical import CredentialPayload
    from jacked.credentials.models import InteractionMode, SwitchContext
    from jacked.credentials.refresh_guard import downgrade_check_for, lineage_finder
    from jacked.credentials.transaction import SwitchRequest

    payload = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": target["id"],
            "claudeAiOauth": build_oauth_data(target),
        }
    )
    return SwitchRequest(
        operation_id=f"auto-{uuid.uuid4().hex}",
        account_id=target["id"],
        email=target.get("email") or "",
        organization_id=target.get("organization_uuid") or None,
        payload=payload,
        context=SwitchContext.AUTO_SWAP,
        interaction=InteractionMode.BACKGROUND,
        # Every engine activation carries the check (#149i): a target row
        # whose cc chain is dead must not put a null refresh token over a
        # live one of the same account.
        refresh_downgrade_check=downgrade_check_for(target, lineage_known=lineage_finder(db)),
        display_name=target.get("display_name") or None,
        organization_name=target.get("organization_name") or None,
    )


async def _execute_swap(
    db,
    active_acct_id: int,
    active_acct: dict,
    target: dict,
    reason: str,
    trigger: str,
    usage_5h: float | None,
    usage_7d: float | None,
    ws_registry=None,
    transaction_engine=None,
) -> bool:
    """Execute a swap. Returns True if credential write succeeded.

    The transaction repository owns pending/final journal state, committed
    pointers, and swap audit publication. This wrapper owns only scheduling,
    recommendation-only audits, runtime backoff, cache cleanup, and broadcasts.
    """
    global _last_swap_time, _last_committed_swap_time
    global _swap_failure_count, _last_swap_failure_at

    from jacked.api.credential_helpers import (
        ForeignPrimaryTokens,
        invalidate_live_cred_cache,
    )
    from jacked.web.auto_swap import format_account_label

    # 1. TOCTOU guard
    current_active = await asyncio.to_thread(_read_active_account_id)
    if current_active != active_acct_id:
        logger.info(
            "Swap aborted: active account changed from %d to %s during evaluation",
            active_acct_id,
            current_active,
        )
        return False

    # No exact-build cooperative capability is registered in current
    # production builds. Persist the recommendation, but do not fall back to
    # the legacy unfenced writer. Global-uncooperative outcomes are likewise
    # not safe for a background mutation.
    transaction_engine = transaction_engine or _certified_auto_swap_engine(db)
    if transaction_engine is None:
        await asyncio.to_thread(
            db.record_swap,
            from_account_id=active_acct_id,
            to_account_id=target["id"],
            reason=reason,
            trigger=trigger,
            from_5h=usage_5h,
            from_7d=usage_7d,
            to_5h=target.get("cached_usage_5h"),
            to_7d=target.get("cached_usage_7d"),
            status="recommendation_only",
        )
        logger.info(
            "Auto-swap recommendation only: account %d -> %d; no certified "
            "cooperative credential capability/writer fence is installed",
            active_acct_id,
            target["id"],
        )
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_recommended",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "reason": reason,
                },
            )
        return False

    # Arm the in-memory attempt cooldown before invoking the transactional
    # authority. The repository creates its durable pending row before writes.
    _last_swap_time = time.time()

    # Credential mutation runs off the event loop. The engine serializes
    # writers and atomically finalizes journal + active/desired pointers.
    from jacked.credentials.models import SwitchOutcome

    try:
        request = _auto_swap_request(target, db)
    except ForeignPrimaryTokens as exc:
        # The payload builder refused: the target's primary tokens belong to
        # another account and it has no Claude Code chain of its own. Nothing
        # was written; the target is skipped until a re-authorization.
        logger.warning("Auto-swap to account %d refused; %s", int(target["id"]), exc)
        return False
    result = await asyncio.to_thread(transaction_engine.activate, request)
    credential_ok = result.outcome in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
    }
    if credential_ok:
        try:
            await asyncio.to_thread(db.mark_global_sessions_pending)
        except Exception:
            logger.exception("Failed to mark global sessions pending after auto-swap")

    # Broadcast and runtime cleanup only after the repository committed.
    if credential_ok:
        _last_committed_swap_time = time.time()
        _swap_failure_count = 0
        # Invalidate live credential cache (new account is active now) and
        # clean up burn-rate state — ONLY on success; a failed attempt
        # must not destroy the active account's burn-rate history.
        invalidate_live_cred_cache()
        _burn_rates.pop(active_acct_id, None)
        _burn_rate_unchanged_ticks.pop(active_acct_id, None)
        _burn_rates.pop(target["id"], None)
        _burn_rate_unchanged_ticks.pop(target["id"], None)
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_triggered",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "from_email": active_acct.get("email", ""),
                    "to_email": target.get("email", ""),
                    "from_label": format_account_label(active_acct),
                    "to_label": format_account_label(target),
                    "reason": reason,
                },
            )
    else:
        # Arm the exponential failure backoff; retry pacing for failed
        # attempts is governed by _swap_backoff_remaining, so un-arm the
        # committed-swap cooldown (it would mask the 60/120/240s windows).
        _swap_failure_count += 1
        _last_swap_failure_at = time.time()
        _last_swap_time = 0.0
        logger.warning(
            "Credential transaction failed — retry gated by "
            "failure backoff, attempt %d (account %d -> %d)",
            _swap_failure_count,
            active_acct_id,
            target["id"],
        )
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_failed",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "reason": reason,
                    "failure": "credential_write_failed",
                },
            )

    return credential_ok


async def active_account_poll_loop(app):
    """Poll the active account with adaptive interval for threshold detection.

    Interval adapts based on urgency tier. The tier thresholds + interval
    values are defined ONCE in jacked.web.auth (compute_urgency_tier +
    _TIER_INTERVALS) — that is the source of truth; keep this doc in sync:
    - Idle (5h ≤50%, no burn): 5 min
    - Normal (5h 50-70%, or low burn): 5 min
    - Warning (5h 70-85%, or projects critical within 15 min): 4 min
    - Critical (5h >85%, or projects critical within 5 min): 3 min
    A weekly value above 80% (the 7d window or the binding model-weekly
    limit, whichever is higher) bumps the tier up one. An account without a
    5-hour window takes its tier from that weekly value alone (>95%
    critical, >90% warning, >80% normal). ±15% jitter on each tick to
    prevent sync patterns; the interval can clamp tighter near a window reset.

    Handles auto-swap decisions, TOCTOU guard, burn-rate tracking with
    decay, and descriptive swap reason strings.  Never crashes — all
    errors are caught and logged per tick.
    """
    global _last_exhaustion_warning, _last_swap_time

    _poll_interval: float = 60.0
    _poll_tier: str = "unknown"
    _last_tick_at: float = 0.0

    while True:
        try:
            # External heartbeat — consumed by active_poll_watchdog_loop.
            # MUST be the first statement of the iteration: every early-exit
            # branch below ends in ``continue`` and skips the rest of the
            # body, so a tail-only write starves the watchdog whenever e.g.
            # auto-swap is disabled (2026-06 incident: 56 pointless respawns
            # at exactly 5-min intervals, ERROR-level log spam). Monotonic
            # clock is immune to wall-clock skew.
            app.state.active_poll_last_tick_at = time.monotonic()

            db = getattr(app.state, "db", None)
            if db is None:
                await asyncio.sleep(60)
                continue

            # -- Settings ------------------------------------------------
            def _read_tick_settings():
                """One thread hop for the tick's settings reads (they ran back
                to back on the loop). None when auto-swap is off or paused."""
                snap = _Snapshot(db, (
                    "auto_swap_enabled", "auto_swap_paused_until",
                    "auto_swap_5h_critical", "auto_swap_5h_warning",
                    "auto_swap_5h_rules_enabled",
                    "usage_check_interval", "window_keeper_active_start",
                    "window_keeper_active_end",
                ))
                auto_swap_enabled = _setting_bool(snap, "auto_swap_enabled", False)
                if not auto_swap_enabled:
                    return None

                # Check pause
                paused_until_str = _setting_str(snap, "auto_swap_paused_until", "")
                if paused_until_str:
                    try:
                        paused_until = datetime.fromisoformat(
                            paused_until_str.replace("Z", "+00:00"),
                        )
                        if paused_until > datetime.now(timezone.utc):
                            logger.info("Auto-swap paused until %s", paused_until_str)
                            return None
                    except (ValueError, TypeError):
                        logger.warning(
                            "Ignoring unparseable pause timestamp: %r",
                            paused_until_str,
                        )

                return (
                    _setting_float(snap, "auto_swap_5h_critical", 90),
                    _setting_float(snap, "auto_swap_5h_warning", 80),
                    _setting_float(snap, "usage_check_interval", 300),
                    _setting_str(snap, "window_keeper_active_start", "06:00"),
                    _setting_str(snap, "window_keeper_active_end", "23:00"),
                    # The 5h critical and burn-rate departures (#224i): off
                    # unless the dashboard toggle stored "true".
                    _setting_bool(snap, "auto_swap_5h_rules_enabled", False),
                )

            tick_settings = await asyncio.to_thread(_read_tick_settings)
            if tick_settings is None:
                await asyncio.sleep(60)
                continue
            (
                critical_5h,
                warning_5h,
                check_interval,
                active_start,
                active_end,
                five_hour_rules,
            ) = tick_settings

            _decision_action = "stay"
            _decision_trigger = "tick"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _suppression = None  # kept for log-schema compat (always None in new flow)

            # -- Late imports (avoid circular deps) ----------------------
            from jacked.web.auth import fetch_usage
            from jacked.api.credential_helpers import read_fresh_active_token
            from jacked.service.usage_pacing import account_recovery
            from jacked.usage_plans import account_view
            from jacked.web.auto_swap import (
                should_swap_now,
                pick_best_target,
                plan_label,
                update_burn_rate,
                tier_critical_threshold,
                tier_for,
                target_for_tier,
                deficit_vs_target,
                stranding_estimate,
                format_account_label,
                REASON_PREFIX_HIGHER_TIER,
                REASON_PREFIX_INTRA_TIER,
                REASON_PREFIX_BURN_RATE,
                TIER_T0,
                TIER_T2,
                TIER_EXCLUDED,
            )
            from jacked.web.auto_swap.walls import (
                _protected_model_key,
                candidate_walls,
                walled_reason,
            )

            # -- Active account ID ---------------------------------------
            active_acct_id = await asyncio.to_thread(_read_active_account_id)
            if active_acct_id is None:
                logger.debug("Active poll: no active account in credential file")
                await asyncio.sleep(60)
                continue

            global _initial_fetch_done
            global _ticks_since_prune
            if not _initial_fetch_done:
                from jacked.web.auth import fetch_usage as _prime_fetch

                logger.info("Auto-swap: priming usage data for all accounts")
                all_accts = await asyncio.to_thread(
                    db.list_accounts, include_inactive=False
                )
                primed = 0
                attempted = 0
                for a in all_accts:
                    if a["id"] != active_acct_id:
                        attempted += 1
                        try:
                            await asyncio.wait_for(
                                _prime_fetch(a["id"], db),
                                timeout=30,
                            )
                            primed += 1
                        except Exception:
                            logger.debug("Prime fetch failed for account %d", a["id"])
                        await asyncio.sleep(1)
                # Nothing to prime (single-account install) counts as done —
                # otherwise this block re-runs every tick forever.
                if attempted == 0 or primed > 0:
                    _initial_fetch_done = True
                    logger.info(
                        "Auto-swap: primed %d/%d accounts",
                        primed,
                        attempted,
                    )

            # -- Fetch usage (fresh token, bypasses cache) ---------------
            effective_token = read_fresh_active_token(active_acct_id)
            active_read = None
            try:
                active_read = await asyncio.wait_for(
                    fetch_usage(
                        active_acct_id,
                        db,
                        access_token=effective_token,
                    ),
                    timeout=50,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Active poll: fetch_usage for account %d exceeded 50s "
                    "— continuing tick with cached data",
                    active_acct_id,
                )
            if _read_landed(active_read):
                await _confirm_pending_from_poll(db, active_acct_id)

            # -- Read active account data from DB ------------------------
            # One thread hop: the account list and the poll-interval reads
            # ran back to back on the loop. Every row is projected here,
            # once (_project_rows): no decision below reads a 5h value on
            # an account without a 5-hour window.
            def _read_tick_accounts():
                return (
                    _list_decision_accounts(db),
                    _poll_interval_inputs(active_acct_id, db),
                )

            accounts, _interval_inputs = await asyncio.to_thread(_read_tick_accounts)
            active_acct = None
            for acct in accounts:
                if acct["id"] == active_acct_id:
                    active_acct = acct
                    break

            if active_acct is None:
                logger.debug(
                    "Active poll: account %d not in active account list",
                    active_acct_id,
                )
                await asyncio.sleep(60)
                continue

            # Compute adaptive poll interval BEFORE broadcast so the
            # frontend receives _poll_interval / _poll_tier / _last_poll_at
            # and can count down accurately instead of guessing.
            _poll_interval, _poll_tier = _compute_poll_interval(
                active_acct_id,
                _interval_inputs,
                _burn_rates,
            )
            _poll_interval = _clamp_poll_interval(
                _poll_interval,
                accounts,
                active_acct_id,
                datetime.now(timezone.utc),
                active_start,
                active_end,
                _last_observed_tiers,
            )

            # Publish the REAL next-poll schedule to app.state so REST callers
            # (the menu-bar panel's "next refresh in …") read the actual interval
            # the loop will wait — not a re-guessed tier. next_poll ≈ this tick's
            # wall-clock + the computed interval.
            app.state.active_poll_account_id = active_acct_id
            app.state.active_poll_at = int(time.time())
            app.state.active_poll_interval = int(_poll_interval)
            app.state.active_poll_tier = _poll_tier

            # Push fresh usage data to connected dashboards so the
            # countdown timer and usage bars update immediately.
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                # Whitelist safe fields — new DB columns won't leak by default.
                # Mirrors _account_to_response in routes/auth.py.
                _WS_SAFE_FIELDS = {
                    "id",
                    "provider",
                    "email",
                    "organization_uuid",
                    "organization_name",
                    "display_name",
                    "expires_at",
                    "scopes",
                    "subscription_type",
                    "rate_limit_tier",
                    "has_extra_usage",
                    "priority",
                    "is_active",
                    "is_deleted",
                    "last_used_at",
                    "cached_usage_5h",
                    "cached_usage_7d",
                    "cached_5h_resets_at",
                    "cached_7d_resets_at",
                    "usage_cached_at",
                    "last_error",
                    "last_error_at",
                    "consecutive_failures",
                    "last_validated_at",
                    "validation_status",
                    "created_at",
                    "updated_at",
                    "cc_expires_at",
                    "auto_swap_enabled",
                }
                safe_acct = {
                    k: v for k, v in active_acct.items() if k in _WS_SAFE_FIELDS
                }
                safe_acct["_poll_interval"] = int(_poll_interval)
                safe_acct["_poll_tier"] = _poll_tier
                safe_acct["_last_poll_at"] = int(time.time())
                # Plan and window facts, the same objects GET
                # /api/auth/accounts carries, so a live refresh draws the
                # bars the plan really has (an absent 5h window included).
                # Guarded like the binding-model block: a resolver failure
                # omits the two keys and never drops the broadcast.
                _view = None
                try:
                    _view = account_view(active_acct)
                    _plan_payload = _view.plan_payload()
                    _windows_payload = _view.windows_payload()
                except Exception as _view_err:
                    logger.warning(
                        "plan/windows payload failed for account %d: %s",
                        active_acct_id,
                        _view_err,
                    )
                else:
                    safe_acct["plan"] = _plan_payload
                    safe_acct["windows"] = _windows_payload
                # Compact binding-model cap so the dashboard can surgically
                # refresh the inline bar (this payload is whitelisted flat
                # fields, not the full AccountResponse the bulk path sends).
                try:
                    from jacked.service.menubar_summary import binding_model_compact

                    _raw = active_acct.get("cached_usage_raw")
                    safe_acct["_binding_model"] = binding_model_compact(
                        json.loads(_raw) if _raw else None,
                        _view.plan if _view is not None else None,
                    )
                except (
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    KeyError,
                ) as _bm_err:
                    # WARNING, not DEBUG: the service runs at effective INFO, so a
                    # DEBUG line here would be invisible and a recurring parse
                    # failure of our own cached payload (schema drift / corruption)
                    # would silently drop the inline bar every tick. Poll interval
                    # is minutes, so this can't spam.
                    logger.warning(
                        "binding_model_compact failed for account %d: %s",
                        active_acct_id,
                        _bm_err,
                    )
                    safe_acct["_binding_model"] = None
                await _ws.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": safe_acct,
                    },
                )

            usage_5h = active_acct.get("cached_usage_5h")
            usage_7d = active_acct.get("cached_usage_7d")

            # -- Burn rate (skip if usage unchanged) ---------------------
            # "Unchanged" needs BOTH windows unchanged: an account without a
            # 5-hour window reads 5h=0 forever, and judging on 5h alone
            # decayed its rate while its weekly usage climbed.
            prev = _burn_rates.get(active_acct_id)
            current_5h_val = usage_5h or 0
            current_7d_val = usage_7d or 0

            if (
                prev is not None
                and current_5h_val == prev.last_check_5h
                and current_7d_val == prev.last_check_7d
            ):
                # Usage unchanged — track consecutive ticks
                ticks = _burn_rate_unchanged_ticks.get(active_acct_id, 0) + 1
                _burn_rate_unchanged_ticks[active_acct_id] = ticks
                # Decay after 5+ unchanged ticks at ANY usage level —
                # unchanged usage is direct evidence burn stopped; a
                # frozen rate fires spurious burn-rate swaps after the
                # user goes idle at >=80%.
                if ticks >= 5:
                    prev.rate_5h_per_min *= 0.8
                    prev.rate_7d_per_min *= 0.8
                    if prev.rate_5h_per_min < 0.001:
                        prev.rate_5h_per_min = 0.0
                    if prev.rate_7d_per_min < 0.001:
                        prev.rate_7d_per_min = 0.0
                br = prev
            else:
                # Usage changed — update burn rate and reset tick counter
                _burn_rate_unchanged_ticks[active_acct_id] = 0
                br = update_burn_rate(
                    _burn_rates,
                    active_acct_id,
                    current_5h=current_5h_val,
                    current_7d=current_7d_val,
                )

            # -- Tier-aware threshold ------------------------------------
            tier_crit = tier_critical_threshold(active_acct)
            effective_critical = max(tier_crit, critical_5h)

            # -- Tier-aware unified decision ----------------------------
            # Single decision per tick: pick the best candidate across
            # the whole pool, then ask should_swap_now whether to leave
            # the active account. Replaces the prior defensive +
            # proactive split. See spec
            # docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md
            now_utc = datetime.now(timezone.utc)

            # Refresh candidate usage if stale (>10 min). Note: this is
            # called every tick now (was previously gated by want_swap);
            # _CANDIDATE_STALENESS_SECONDS keeps the API call-rate the
            # same for stable data — only candidates we haven't fetched
            # in 10+ minutes get re-fetched.
            accounts = await _fetch_candidate_usage(
                accounts,
                active_acct_id,
                db,
            )

            # Hysteresis: pass last-observed tier per non-active account
            # to suppress jitter-driven flips across tier boundaries. Pick
            # judges each candidate's wall by that candidate's own plan
            # field, the threshold rules 2a/2b apply to it once it is
            # active (walls.py: no caller passes a threshold).
            best = pick_best_target(
                accounts,
                current_id=active_acct_id,
                active_start=active_start,
                active_end=active_end,
                now=now_utc,
                prev_tiers=_last_observed_tiers,
            )

            reason = should_swap_now(
                active=active_acct,
                best=best,
                burn_rate=br,
                check_interval_min=check_interval / 60,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                now=now_utc,
                prev_tiers=_last_observed_tiers,
                active_start=active_start,
                active_end=active_end,
                five_hour_rules=five_hour_rules,
            )
            # The active account's wall (rule 2a or 2b), by the predicate
            # should_swap_now applies. Rule 1 or 1b can win the reason on a
            # walled account; the departure is still forced (#224i): the
            # wall skips emergence persistence and the minimum residency,
            # makes a no-target tick exhaustion instead of a stall, and
            # goes into the decision detail.
            wall = walled_reason(active_acct, now_utc)

            # ---- Anti-jitter persistence on emerged tiers ----
            # Hysteresis-damped tiers — must match what should_swap_now
            # evaluated (see TestActiveTierHysteresis in test_auto_swap).
            best_tier_damped = (
                tier_for(
                    best,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(best["id"]),
                )
                if best is not None
                else None
            )
            active_tier_damped = tier_for(
                active_acct,
                now=now_utc,
                prev_tier=_last_observed_tiers.get(active_acct_id),
            )
            _is_emerge_reason = reason is not None and reason.startswith(
                (REASON_PREFIX_HIGHER_TIER, REASON_PREFIX_INTRA_TIER),
            )
            if _is_emerge_reason and (
                wall is not None
                or (best_tier_damped == TIER_T0 and active_tier_damped >= TIER_T2)
            ):
                # Fast paths that skip the persistence requirement entirely:
                # a walled active cannot serve, so its departure is decisive
                # whichever rule's reason won; and a >=2-tier gap (T0 best vs
                # T2+/unclassified active) cannot be boundary jitter.
                pass
            else:
                reason = _apply_emergence_persistence(
                    reason=reason,
                    best_tier=best_tier_damped,
                    streak=_emerged_tier_streak,
                    persistence_ticks=_EMERGENCE_PERSISTENCE_TICKS,
                )

            # Build candidate summaries for decision log. Target/deficit
            # computed against the DAMPED tier (target_for_tier) so the
            # log matches what selection actually evaluated. "wall" and
            # "serves_model" are the two wall checks pick_best_target
            # applies (walls.candidate_walls), so the log explains a
            # candidate the wall filter rejected.
            _candidate_summaries = []
            _protected_key = _protected_model_key(accounts, active_acct_id)
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(
                    cand,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                cand_target = target_for_tier(
                    cand_tier,
                    cand,
                    now_utc,
                    active_start,
                    active_end,
                )
                cand_usage_7d = cand.get("cached_usage_7d")
                cand_deficit = (
                    cand_target - cand_usage_7d
                    if cand_target is not None and cand_usage_7d is not None
                    else None
                )
                cand_stranding = stranding_estimate(
                    cand,
                    now_utc,
                    active_start,
                    active_end,
                )
                cand_wall, cand_serves = candidate_walls(cand, _protected_key, now_utc)
                _candidate_summaries.append(
                    {
                        "id": cand["id"],
                        "email": cand.get("email", ""),
                        "label": format_account_label(cand),
                        "5h": cand.get("cached_usage_5h"),
                        "7d": cand.get("cached_usage_7d"),
                        "tier": cand_tier,
                        "target_7d": (
                            round(cand_target, 1) if cand_target is not None else None
                        ),
                        "deficit": (
                            round(cand_deficit, 1) if cand_deficit is not None else None
                        ),
                        "stranding": (
                            round(cand_stranding, 1)
                            if cand_stranding is not None
                            else None
                        ),
                        "wall": cand_wall,
                        "serves_model": cand_serves,
                        "is_best": (best is not None and cand["id"] == best["id"]),
                    }
                )

            # Refresh hysteresis state + prune dead account ids.
            # The ACTIVE account is included in _last_observed_tiers so
            # should_swap_now's tier_for(active) call picks up hysteresis
            # too — without this, Anthropic API timestamp jitter at the
            # 24h/48h boundary flickers active's tier each tick, causing
            # `best_tier < active_tier` to flip false → reason None →
            # emergence streak clears → 6+ minute swap delays. (Found
            # during DCR cycle on user-reported "6-min swap delay" bug.)
            live_ids = {a["id"] for a in accounts}
            for stale_id in list(_last_observed_tiers.keys()):
                if stale_id not in live_ids:
                    _last_observed_tiers.pop(stale_id, None)
            for stale_id in list(_drain_advisor_last_sent.keys()):
                if stale_id not in live_ids:
                    _drain_advisor_last_sent.pop(stale_id, None)
            for cand in accounts:
                cand_tier = tier_for(
                    cand,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                if cand_tier == TIER_EXCLUDED:
                    _last_observed_tiers.pop(cand["id"], None)
                else:
                    _last_observed_tiers[cand["id"]] = cand_tier

            ws_registry = getattr(app.state, "ws_registry", None)

            global _last_exhaustion_warning, _consecutive_no_best_ticks
            global _last_stall_warning, _same_tier_advisory_last
            global _last_committed_swap_time

            # Drain advisor — purely informational; never affects the
            # decision below, so a failure must not abort the tick.
            try:
                await _drain_advisor_tick(
                    accounts,
                    now_utc,
                    active_start,
                    active_end,
                    ws_registry,
                    _last_observed_tiers,
                )
            except Exception:
                logger.debug("Drain advisor failed", exc_info=True)

            _residency_elapsed = time.time() - _last_committed_swap_time
            # Proactive departures wait out the minimum residency. Forced
            # ones do not: drained, 5h critical, and the weekly and model
            # weekly walls (REASON_PREFIX_WEEKLY_CRITICAL,
            # REASON_PREFIX_MODEL_WEEKLY), where the account cannot serve.
            # Keep those prefixes out of this tuple. A walled active is
            # never held either, even when rule 1 or 1b won the reason.
            _residency_gated = (
                wall is None
                and reason is not None
                and reason.startswith(
                    (
                        REASON_PREFIX_HIGHER_TIER,
                        REASON_PREFIX_INTRA_TIER,
                        REASON_PREFIX_BURN_RATE,
                    )
                )
            )

            if reason is None:
                _decision_action = "stay"
                if best is None:
                    _decision_trigger = "tick"
                    _decision_reason = (
                        f"stay: no candidate has deficit "
                        f"(plan {plan_label(active_acct) or 'unknown'})"
                    )
                else:
                    _decision_target_id = best["id"]
                    # Tier-keyed streak: count applies to ANY account in
                    # the tracked tier. count > 0 here means persistence
                    # suppressed an emerge reason this very tick (the
                    # helper clears the streak on decisive non-emerge).
                    streak_count = (
                        _emerged_tier_streak["count"]
                        if _emerged_tier_streak["tier"] == best_tier_damped
                        else 0
                    )
                    if best_tier_damped != TIER_EXCLUDED and streak_count > 0:
                        _decision_trigger = "emergence_pending"
                        _decision_reason = (
                            f"stay: emergence streak "
                            f"{streak_count}/{_EMERGENCE_PERSISTENCE_TICKS}, "
                            f"awaiting confirmation "
                            f"(best id={best['id']} tier={best_tier_damped} "
                            f"vs active tier={active_tier_damped})"
                        )
                    else:
                        _decision_trigger = "tick"
                        _decision_reason = (
                            f"stay: best is same/lower tier "
                            f"(best id={best['id']} tier={best_tier_damped})"
                        )
            elif _residency_gated and _residency_elapsed < _MIN_RESIDENCY_SECONDS:
                # Min-residency gate: proactive departures must let the
                # active account settle. Drained, 5h-critical and walled
                # (weekly / model weekly) reasons are forced departures and
                # bypass this gate.
                _decision_action = "stay"
                _decision_trigger = "residency_blocked"
                _decision_target_id = best["id"] if best else None
                _decision_reason = (
                    f"swap warranted ({reason}) but minimum residency "
                    f"active "
                    f"({_MIN_RESIDENCY_SECONDS - _residency_elapsed:.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                _decision_action = "stay"
                _decision_trigger = "cooldown_blocked"
                _decision_target_id = best["id"] if best else None
                _decision_reason = (
                    f"swap warranted ({reason}) but cooldown active "
                    f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif best is None:
                _decision_action = "stay"
                _decision_trigger = "no_target"
                _decision_reason = f"swap warranted ({reason}) but no eligible target"

                now_ts = time.time()
                if now_ts - _last_exhaustion_warning > _EXHAUSTION_COOLDOWN_SECONDS:
                    logger.warning(
                        "Auto-swap needed but no eligible target "
                        "(active account %d at 5h=%s, 7d=%s): %s",
                        active_acct_id,
                        _pct_text(usage_5h), _pct_text(usage_7d),
                        wall or reason,
                    )
                    _last_exhaustion_warning = now_ts

                # The earliest time some account becomes workable again:
                # per account, the latest future reset among its constrained
                # windows (a plan without a 5-hour window offers its weekly
                # reset, never a stale 5h one; a binding model limit at the
                # plan's weekly_critical_pct offers its own reset). Accounts
                # with no constrained window, or none with a knowable reset,
                # offer nothing.
                recoveries = [
                    r
                    for r in (account_recovery(a, now_utc) for a in accounts)
                    if r is not None
                ]
                next_recovery = min(recoveries) if recoveries else None

                if ws_registry:
                    await ws_registry.broadcast(
                        "all_accounts_exhausted",
                        {
                            "active_account_id": active_acct_id,
                            "usage_5h": usage_5h,
                            "usage_7d": usage_7d,
                            "next_recovery_at": (
                                next_recovery.at.isoformat()
                                if next_recovery
                                else None
                            ),
                            # "5h", "7d" or "model": the limit whose reset
                            # frees the account, so the banner names it
                            # instead of promising usage in general.
                            "next_recovery_window": (
                                next_recovery.window if next_recovery else None
                            ),
                            # The model's name (for example "Fable") for a
                            # "model" window; None for "5h" and "7d".
                            "next_recovery_label": (
                                next_recovery.label if next_recovery else None
                            ),
                        },
                    )
            elif (_backoff_remaining := _swap_backoff_remaining()) > 0:
                _decision_action = "stay"
                _decision_trigger = "swap_aborted"
                _decision_target_id = best["id"]
                _decision_reason = (
                    f"swap warranted ({reason}) but failure backoff "
                    f"active ({_backoff_remaining:.0f}s remaining after "
                    f"{_swap_failure_count} failed attempt(s))"
                )
                logger.debug("Active poll: %s", _decision_reason)
            else:
                trigger = _trigger_for_reason(reason)
                logger.info(
                    "Auto-swap: switching from account %d (5h=%s) to "
                    "account %d (5h=%s) — %s [%s]",
                    active_acct_id,
                    _pct_text(usage_5h),
                    best["id"],
                    _pct_text(best.get("cached_usage_5h")),
                    reason,
                    trigger,
                )
                transaction_engine = _certified_auto_swap_engine(db)
                swap_committed = await _execute_swap(
                    db,
                    active_acct_id,
                    active_acct,
                    best,
                    reason=reason,
                    trigger=trigger,
                    usage_5h=usage_5h,
                    usage_7d=usage_7d,
                    ws_registry=ws_registry,
                    transaction_engine=transaction_engine,
                )
                if swap_committed:
                    _emerged_tier_streak["tier"] = None
                    _emerged_tier_streak["count"] = 0
                    # Clear hysteresis state for both swap participants —
                    # the new active needs a fresh tier observation, and
                    # the prior active will be re-observed as a candidate
                    # next tick. The next tick's hysteresis-refresh block
                    # repopulates both based on current data.
                    _last_observed_tiers.pop(best["id"], None)
                    _last_observed_tiers.pop(active_acct_id, None)
                    _decision_action = "swap"
                    _decision_trigger = trigger
                    _decision_target_id = best["id"]
                    _decision_reason = reason
                elif transaction_engine is None:
                    _decision_action = "recommend"
                    _decision_trigger = "recommendation_only"
                    _decision_target_id = best["id"]
                    _decision_reason = (
                        "swap recommended but automatic credential mutation "
                        f"is not certified: {reason}"
                    )
                else:
                    # TOCTOU mismatch or credential lock failure. Don't
                    # clear streak/hysteresis — retry pacing is governed
                    # by the failure backoff. Decision log records the
                    # attempt and the failure so operators see "we tried"
                    # rather than silent state-corruption.
                    _decision_action = "stay"
                    _decision_trigger = "swap_aborted"
                    _decision_target_id = best["id"]
                    _decision_reason = (
                        f"swap aborted (credential write or TOCTOU failed): {reason}"
                    )

            # ---- Silent-stall watchdog ------------------------------------
            # `reason` reflects should_swap_now's verdict AS POSSIBLY
            # MUTATED by emergence persistence (which may set it to
            # None to defer a higher-tier swap until streak met).
            # Cooldown does NOT mutate reason — only persistence does.
            cached_at = active_acct.get("usage_cached_at") or 0
            age_seconds = int(time.time()) - int(cached_at)
            has_other_accounts = (
                sum(1 for a in accounts if a["id"] != active_acct_id) > 0
            )
            best_deficit_val = None
            if best is not None:
                _bd = deficit_vs_target(
                    best,
                    now=now_utc,
                    active_start=active_start,
                    active_end=active_end,
                )
                if _bd is not None:
                    best_deficit_val = _bd
            stalled_this_tick = _evaluate_stall(
                decision_action=_decision_action,
                best=best,
                usage_cached_at_age_seconds=age_seconds,
                has_other_accounts=has_other_accounts,
                reason=reason,
                wall=wall,
                staleness_threshold=_STALL_USAGE_STALENESS_SECONDS,
            )

            # Same-tier-deficit advisory (split from stall pattern d):
            # intended-behavior stays must not page at ERROR. T0/T1
            # candidates only — those are harvestable drain-to targets.
            if _same_tier_advisory_applies(
                decision_action=_decision_action,
                best=best,
                reason=reason,
                best_deficit=best_deficit_val,
                best_tier=best_tier_damped,
            ):
                now_ts = time.time()
                if (
                    now_ts - _same_tier_advisory_last
                    > _SAME_TIER_ADVISORY_COOLDOWN_SECONDS
                ):
                    _same_tier_advisory_last = now_ts
                    logger.info(
                        "Same-tier deficit advisory: best id=%s "
                        "(tier=%s) is %.1f%% behind its tier target but "
                        "same-tier-never-overrides keeps the loop on "
                        "stay (active=%d)",
                        best["id"],
                        best_tier_damped,
                        best_deficit_val,
                        active_acct_id,
                    )
                    if ws_registry:
                        await ws_registry.broadcast(
                            "same_tier_deficit_advisory",
                            {
                                "active_account_id": active_acct_id,
                                "best_account_id": best["id"],
                                "best_tier": best_tier_damped,
                                "best_deficit": round(best_deficit_val, 1),
                            },
                        )
            was_stalled = _consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD
            if stalled_this_tick:
                _consecutive_no_best_ticks += 1
            else:
                _consecutive_no_best_ticks = 0

            # Stall transition: was-stalled → now-cleared. Broadcast
            # so the dashboard banner can hide itself instead of
            # remaining stuck until manual dismiss + 30 min cooldown.
            now_stalled = _consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD
            if was_stalled and not now_stalled:
                logger.info(
                    "Auto-swap stall cleared (active=%d)",
                    active_acct_id,
                )
                if ws_registry:
                    await ws_registry.broadcast(
                        "auto_swap_stall_clear",
                        {"active_account_id": active_acct_id},
                    )

            if now_stalled:
                now_ts = time.time()
                if now_ts - _last_stall_warning > _STALL_WARNING_COOLDOWN_SECONDS:
                    last_fetch_age = age_seconds
                    logger.error(
                        "Auto-swap stalled: %d consecutive ticks "
                        "(active=%d, last_fetch=%ss ago, best=%s, "
                        "best_deficit=%s)",
                        _consecutive_no_best_ticks,
                        active_acct_id,
                        last_fetch_age,
                        best["id"] if best else None,
                        f"{best_deficit_val:.1f}%" if best_deficit_val else "n/a",
                    )
                    _last_stall_warning = now_ts
                    if ws_registry:
                        await ws_registry.broadcast(
                            "auto_swap_stall",
                            {
                                "active_account_id": active_acct_id,
                                "consecutive_ticks": _consecutive_no_best_ticks,
                                "last_fetch_age_seconds": last_fetch_age,
                                "best_account_id": best["id"] if best else None,
                                "best_deficit": best_deficit_val,
                            },
                        )

            # Record decision in the log
            if active_acct is not None:
                try:
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=(_decision_action == "swap"),
                        suppression=_suppression,
                        escape_override=False,
                        candidates=_candidate_summaries,
                        proactive_target_id=None,
                        cooldown_active=(time.time() - _last_swap_time)
                        < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                        wall=wall,
                    )
                    decision_id = await asyncio.to_thread(
                        db.record_decision,
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=_decision_trigger,
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
                    if ws_registry and decision_id:
                        try:
                            await ws_registry.broadcast(
                                "decision_log_entry",
                                {
                                    "id": decision_id,
                                    "account_id": active_acct_id,
                                    "email": active_acct.get("email", ""),
                                    "label": format_account_label(active_acct),
                                    "action": _decision_action,
                                    "trigger": _decision_trigger,
                                    "reason": _decision_reason or "no trigger",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "detail": _tick_detail,
                                },
                            )
                        except Exception:
                            logger.debug(
                                "Decision log WS broadcast failed", exc_info=True
                            )
                except Exception:
                    logger.debug("Failed to record decision", exc_info=True)

            # Periodic prune — deterministic fallback every 500 ticks
            _ticks_since_prune += 1
            if _ticks_since_prune >= 500 or random.random() < 0.01:
                try:
                    await asyncio.to_thread(db.prune_decision_log)
                    _ticks_since_prune = 0
                except Exception:
                    logger.warning("Failed to prune decision log", exc_info=True)

        except asyncio.CancelledError:
            logger.info("Active account poll loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Active account poll loop error", exc_info=True)

        # Watchdog: detect if the event loop was blocked or suspended
        now_tick = time.time()
        if (
            _last_tick_at > 0
            and _poll_interval > 0
            and (now_tick - _last_tick_at) > 2 * _poll_interval
        ):
            logger.warning(
                "Active poll loop delayed — last tick %ds ago, expected interval %ds",
                int(now_tick - _last_tick_at),
                int(_poll_interval),
            )
        _last_tick_at = now_tick

        # Refresh the heartbeat after the tick body too. The authoritative
        # write is at the TOP of the iteration (it covers all ``continue``
        # early exits); this tail write only resets the staleness clock so
        # it measures from the start of the upcoming sleep rather than
        # from tick start — a single tick can legitimately run long
        # (priming fetches, swap execution).
        try:
            app.state.active_poll_last_tick_at = time.monotonic()
        except Exception:
            # app.state vanishing means we're in lifespan teardown; let the
            # natural cancellation finish the loop on the next await.
            pass

        logger.debug("Active poll: tier=%s interval=%.0fs", _poll_tier, _poll_interval)
        await asyncio.sleep(_poll_interval)


# -----------------------------------------------------------------------
# Loop 1b — Watchdog for active_account_poll_loop
# -----------------------------------------------------------------------

# The active poll task can be silently dead-on-arrival under a specific
# race observed in production on 2026-05-10: tray force-exits uvicorn,
# lifespan creates a new task, but the task never gets its first slice
# of CPU. No exception fires, no log. Heartbeat-based detection is the
# only reliable signal.
_WATCHDOG_INTERVAL_SECONDS = 60
_HEARTBEAT_STALE_SECONDS = 300  # 5min — 5x default 60s interval, 1x worst-case 300s
_RESPAWN_COOLDOWN_SECONDS = 30  # prevent thrash if respawned task also dies fast


def _respawn_active_poll(app) -> bool:
    """Cancel any stale active_poll_task and start a fresh one.

    Returns True if a new task was created. Idempotent — safe to call
    when the existing task is healthy (it gets cancelled and replaced;
    the new task picks up the next tick).
    """
    old_task = getattr(app.state, "active_poll_task", None)
    if old_task is not None and not old_task.done():
        try:
            old_task.cancel()
        except Exception:
            logger.warning("Failed to cancel stale active_poll_task", exc_info=True)
    try:
        new_task = asyncio.create_task(active_account_poll_loop(app))
    except Exception:
        logger.error("Failed to respawn active_poll_task", exc_info=True)
        return False
    app.state.active_poll_task = new_task
    # Seed heartbeat so the watchdog grants the new task a full interval
    # to tick before declaring it stale again.
    app.state.active_poll_last_tick_at = time.monotonic()
    app.state.active_poll_last_respawn_at = time.monotonic()
    return True


async def active_poll_watchdog_loop(app):
    """Detect and recover from a silently-dead active_account_poll_loop.

    Checks ``app.state.active_poll_last_tick_at`` every minute. If the
    heartbeat is stale (no tick for > 5min) OR the task object is done()
    when it shouldn't be, respawn it via ``_respawn_active_poll``.

    Never crashes — exceptions are caught and logged per tick so the
    watchdog itself doesn't become the dead loop it was built to detect.
    """
    # Grace period: lifespan may still be wiring app.state. Skip first check.
    await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)

    while True:
        try:
            now = time.monotonic()
            last_tick = getattr(app.state, "active_poll_last_tick_at", None)
            last_respawn = getattr(app.state, "active_poll_last_respawn_at", 0.0)
            task = getattr(app.state, "active_poll_task", None)

            # Cooldown: if we just respawned, give the new task a full
            # cycle before considering another respawn.
            if last_respawn and (now - last_respawn) < _RESPAWN_COOLDOWN_SECONDS:
                await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)
                continue

            stale = last_tick is None or (now - last_tick) > _HEARTBEAT_STALE_SECONDS
            task_dead = task is not None and task.done()

            if stale or task_dead:
                logger.error(
                    "active_poll_watchdog: detected dead task — "
                    "heartbeat_age=%s done=%s — respawning",
                    f"{int(now - last_tick)}s" if last_tick else "never",
                    task_dead,
                )
                if _respawn_active_poll(app):
                    logger.info("active_poll_watchdog: respawn succeeded")

        except asyncio.CancelledError:
            logger.info("Active poll watchdog cancelled — shutting down")
            raise
        except Exception:
            logger.warning("active_poll_watchdog error", exc_info=True)

        await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)


# -----------------------------------------------------------------------
# Loop 1b — Non-active account usage refresh (10 min)
# -----------------------------------------------------------------------

_ALL_ACCOUNTS_REFRESH_INTERVAL = 600  # 10 minutes


async def all_accounts_refresh_loop(app):
    """Refresh usage for NON-active accounts every 10 minutes.

    The active account is kept fresh by active_account_poll_loop (≤5 min); this
    loop ensures EVERY other account is also refreshed so quota burned on OTHER
    machines (shared accounts) shows up here — not just the one in use. This is
    unconditional: unlike full_sweep_loop it does NOT depend on the window
    keeper being enabled.

    Pacing/safety: fetch_usage enforces its own hard per-account rate-limit
    ceiling (no 429s), we skip the active account, pick each Claude account's
    token through ``choose_usage_token`` (a live CC token, else the primary
    token; a dead CC chain is never resent), skip a Claude account with no
    usable token (Codex accounts have none but are polled via the app-server),
    and sleep between accounts. Never crashes — errors are logged per
    account/tick.
    """
    while True:
        # Sleep first — at startup the active loop + login already populate caches.
        await asyncio.sleep(_ALL_ACCOUNTS_REFRESH_INTERVAL)
        try:
            db = getattr(app.state, "db", None)
            if db is None:
                continue
            from jacked.web.auth import choose_usage_token, fetch_usage

            active_id = getattr(app.state, "active_poll_account_id", None)
            refreshed = 0
            accounts = await asyncio.to_thread(db.list_accounts, include_inactive=False)
            for acct in accounts:
                if acct.get("id") == active_id:
                    continue  # covered (more often) by active_account_poll_loop
                is_codex = (acct.get("provider") or "claude") == "codex"
                # Claude accounts fetch usage with the token the shared chooser
                # picks (a live CC token, else the primary token). Codex usage
                # comes from the app-server (no token), so it must NOT be
                # skipped here — a token skip is exactly why Codex usage stayed
                # at 0%.
                choice = None if is_codex else choose_usage_token(acct)
                if not is_codex and choice is None:
                    continue
                try:
                    # fetch_usage dispatches on provider; for codex it ignores the
                    # token and only polls the live codex account (others no-op).
                    read = await fetch_usage(
                        acct["id"], db, access_token=None if is_codex else choice.token
                    )
                    refreshed += 1
                    if _read_landed(read):
                        await _confirm_pending_from_poll(db, acct["id"])
                except Exception:
                    logger.warning(
                        "All-accounts refresh: fetch_usage failed for account %s",
                        acct.get("id"),
                        exc_info=True,
                    )
                await asyncio.sleep(2)  # pace between accounts
            if refreshed:
                logger.info(
                    "All-accounts refresh: updated %d non-active account(s)", refreshed
                )
        except asyncio.CancelledError:
            logger.info("All-accounts refresh loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("All-accounts refresh loop error", exc_info=True)
