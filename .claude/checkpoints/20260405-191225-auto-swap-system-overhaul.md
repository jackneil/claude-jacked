---
status: in-progress
branch: master
timestamp: 2026-04-05T19:12:25-04:00
releases: [v0.35.0, v0.35.1, v0.35.3, v0.35.4, v0.36.0]
files_modified:
  - jacked/web/auto_swap.py
  - jacked/api/usage_monitor.py
  - jacked/web/auth.py
  - jacked/web/database.py
  - jacked/web/window_keeper.py
  - jacked/api/routes/auth.py
  - jacked/api/routes/settings_swap.py
  - jacked/api/credential_helpers.py
  - jacked/data/web/js/websocket.js
  - jacked/data/web/js/components/auto-swap.js
  - jacked/data/web/js/components/accounts.js
  - jacked/data/web/js/app.js
  - tests/unit/test_auto_swap.py
  - tests/unit/test_window_keeper.py
  - docs/architecture/auto-swap-system.md
plans_in_progress: []
---

# Checkpoint: Auto-Swap System Overhaul

## What We're Working On

Complete overhaul of the multi-account auto-swap system for claude-jacked. The system manages multiple Claude AI accounts to maximize total usable capacity by intelligently rotating between them based on usage windows, rate limits, and 7-day capacity scheduling. This session was a continuation of a crashed previous session and evolved into a massive sprint.

## Accomplished This Session

- **Unified Swap Decision Engine** (v0.35.0): urgency-based 7d filter relaxation, deficit bonus in scoring, anti-ping-pong 7d suppression, simplified proactive scheduler
- **Org Identity in Notifications** (v0.35.0): format_account_label helper, WebSocket payloads with from_label/to_label, swap history shows org names
- **DCR Fix Round 1** (v0.35.0): escape hatch deficit suppression, WebSocket token leak (blocklist→whitelist), active hours default mismatch
- **DCR Fix Round 2 — Deep Structural** (v0.35.1): _execute_swap helper (TOCTOU guard, cross-process lock, canonical ordering, partial-swap recovery), initial fetch retry, double fetch removal, proactive skip logging
- **429 Recovery Credential Fix** (v0.35.1): _try_refresh_on_429 was writing non-active account credentials to .credentials.json, silently switching Claude Code to the wrong account. Fixed to only write for active account.
- **Capacity Waste Model** (v0.35.3): compute_urgency_threshold with tiered thresholds based on remaining 5h windows (0%/4%/8%/15%), proactive scanner uses urgency scan not best-scored, time-of-day guard (skip within 30 min of active_end)
- **Exhaustion Guard** (v0.35.3): has_viable_headroom — never swap to accounts with less than one window's worth of unused 7d capacity. Minimum recoverable guard in proactive scanner.
- **7d Window Keeper** (v0.35.3): needs_7d_ping detects when 7d windows reset mid-5h-window and pings to start the new one
- **Decision Log** (v0.35.4): Full tick trace every poll, manual switch logging, filterable API, expandable frontend table with color-coded badges
- **Manual Switch Logging** (v0.35.4): Writes to swap_log + decision_log + WebSocket broadcast
- **401 Auto-Refresh** (v0.36.0): _exchange_refresh_token shared helper (eliminates 3x duplicate POST), _try_refresh_primary_token with per-account locking + circuit breaker + stale-token detection, all 401 handlers auto-refresh before marking invalid
- **Candidate Staleness** (v0.35.1): Only fetch non-active accounts when data >10 min old
- **Checkpoint Skill**: Created `/checkpoint` skill (save/resume/list) for session continuity
- **94 auto_swap tests**, 1559 total tests passing

## Decisions Made

- **Urgency > Deficit for proactive swaps** — deficit (how far behind schedule) is wrong for end-of-window urgency. The capacity waste model uses remaining 5h windows as the urgency signal, with thresholds that scale down as time runs out.
- **Separate scan for proactive vs defensive** — proactive uses urgency scan (finds most-urgent expiring capacity), defensive uses pick_best_target (finds best overall account). They share _execute_swap but have different target selection logic.
- **has_viable_headroom as universal guard** — applied in pick_best_target filter AND proactive scanner. No swap path can target a near-exhausted account.
- **Primary vs CC tokens stay separate** — primary tokens for jacked's own API calls (usage fetching), CC tokens for Claude Code's credential file. 429 recovery prefers CC tokens; 401 auto-refresh uses primary refresh token.
- **Per-account asyncio locks for token refresh** — prevents concurrent refresh races that consume refresh tokens.
- **Circuit breaker for dead refresh tokens** — permanent skip on invalid_grant, 10-min cooldown on transient failures. Prevents API call amplification.
- **Whitelist not blocklist for WebSocket data** — new DB columns don't auto-leak.
- **Decision log records ALL candidates** — including non-passing ones with skip_reason, so you can debug "why wasn't X chosen?"

## Remaining Work

Nothing critical remains — all planned features are implemented and released through v0.36.0. Lower-priority items noted during DCR passes:

1. **Refactor remaining token exchange callers** — `refresh_cc_token` and `_try_refresh_on_429` still have inline POST logic that could use `_exchange_refresh_token`. Started with `refresh_account_token` but didn't finish the other two.
2. **Decision log frontend QA** — the decision log UI was implemented but never browser-tested with `/qa`. Should verify expandable rows, filtering, badge colors.
3. **Architecture doc may have minor drift** — updated extensively but the doc is 280+ lines and some DCR-era changes may not be reflected.
4. **`active_account_poll_loop` is ~850 lines** — the proactive scheduler could be extracted into a helper function for readability.
5. **Test for time-dependent scenarios** — several tests use `datetime.now()` which makes them time-of-day-dependent. Could be made deterministic with mocked time.

## Current State

All work is **released and deployed** (v0.36.0). No in-progress plans, no uncommitted code changes (just a staged research file). The session ended naturally after completing the 401 auto-refresh feature and creating the checkpoint skill.

## Gotchas & Notes

- **The user gets frustrated when you hack fixes without brainstorming/planning first.** Always use the process: brainstorm → plan → implement → DCR. Saved to memory.
- **Always use Opus for code-writing subagents** — never Sonnet/Haiku. Saved to memory.
- **The user runs `jacked webux` from a separate terminal** — never start the server from Claude sessions.
- **DCR after every plan** — the user expects /dcr on plans, not just implementations. DCR findings become a new plan → implement cycle.
- **The 429 recovery credential clobber bug** was the sneakiest issue — it silently switched Claude Code to the wrong account by writing non-active credentials during _try_refresh_on_429. Took tracing the swap log gaps to find it.
- **Primary access tokens go stale when anything exchanges a refresh token** — Anthropic invalidates old access tokens immediately on rotation, even if expires_at hasn't passed.
- **The proactive scheduler ping-ponged because it picked the best-SCORED target, not the most-URGENT one** — Account 2 at 126 points beat Account 1 at 50 points, even though Account 1 had expiring capacity.
- **`compute_effective_working_hours` handles overnight gaps correctly** but has a ~1 hour DST edge case from the manual UTC offset calculation.

## Key Files

- `docs/architecture/auto-swap-system.md` — the source of truth for how the entire system works
- `jacked/web/auto_swap.py` — pure decision functions (should_swap, score_candidate, pick_best_target, compute_7d_deficit, compute_urgency_threshold, has_viable_headroom, format_account_label)
- `jacked/api/usage_monitor.py` — poll loops, _execute_swap, decision log recording, proactive urgency scanner
- `jacked/web/auth.py` — fetch_usage, _exchange_refresh_token, _try_refresh_primary_token, _try_refresh_on_429, token refresh with circuit breaker
- `tests/unit/test_auto_swap.py` — 94 tests covering all decision functions
