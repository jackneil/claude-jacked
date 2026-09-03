# Auto-Swap System Architecture

**Last source-wide audit:** 2026-09-03

**Status:** Selection and recommendation are enabled. Automatic Claude
credential mutation is recommendation-only in the shipped runtime.

## 1. Scope

The auto-swap system has two separate responsibilities:

1. observe usage and recommend which account should be used;
2. activate the recommendation only when a certified cooperative credential
   transaction engine is explicitly installed.

The first responsibility runs in production. The second has no production
engine today. A recommendation is not a switch, does not change an active or
desired account pointer, and must not be presented as though a running session
moved.

This document focuses on the account-truth boundary around the existing
tier-strict selection algorithm. Detailed utilization formulas remain in
`jacked/web/auto_swap/` and its tests.

## 2. Decision model

`active_account_poll_loop()` uses the canonical active-credential resolver,
refreshes usage, evaluates candidates, applies departure and anti-jitter gates,
and records one decision per tick.

At a high level:

1. Resolve the observed active account. If exact-build store consensus is not
   available, there is no safe active identity for a credential switch.
2. Refresh active usage within the polling and rate-limit bounds.
3. Refresh stale candidate usage within per-candidate timeouts.
4. Call `pick_best_target()` to rank eligible non-active accounts by urgency
   tier, reset deadline, and target deficit.
5. Call `should_swap_now()` to decide whether the active account should be
   left on this tick.
6. Apply emergence persistence, minimum residency, cooldown, no-target, and
   failure-backoff gates.
7. When a departure remains, call `_execute_swap()`.
8. Record the decision and publish applicable WebSocket events.

The selection algorithm may conclude that a target is best even when the
credential layer is not allowed to activate it. That conclusion becomes a
recommendation-only audit event.

## 3. Candidate and departure policy

`pick_best_target()` filters out the current account, disabled or deleted
accounts, accounts excluded from auto-swap, invalid or repeatedly failing
accounts, accounts without Claude Code access tokens, accounts without usable
7-day data, accounts at or above their tier target, and accounts without
sufficient 5-hour headroom. T0 drain candidates have the implementation's
documented narrow headroom exception.

The remaining candidates sort by:

1. urgency tier;
2. earliest 7-day reset within that tier;
3. largest deficit from that tier's target.

`should_swap_now()` defaults to staying on the current account. Its departure
families include higher-tier emergence, guarded intra-T0 preemption, a drained
T0/T1 account, 5-hour critical usage, and a burn-rate projection. Reset
suppression, hysteresis, emergence persistence, minimum residency, committed
swap cooldown, and failure backoff constrain churn.

These policy decisions operate on cached and fetched usage. They do not prove
credential placement, process activation, or provider use.

## 4. Canonical active identity

The monitor's `_read_active_account_id()` calls `resolve_active_identity()` and
returns an ID only when it reports `resolved` for the exact certified store
topology. The similarly named helper in `jacked/api/credential_helpers.py`
enforces the same resolver-only contract for its callers.

There is no file-first fallback. A matching `_jackedAccountId` in
`~/.claude/.credentials.json` is insufficient when the certified authority is
missing, unusable, or in conflict with its required mirror. A database setting
is also insufficient because it records preference or a prior commit, not a
fresh store observation.

The `_execute_swap()` TOCTOU guard re-resolves immediately before either a
recommendation or mutation path. If the observed account no longer matches the
account evaluated by the loop, the attempt is aborted.

## 5. Recommendation-only production behavior

`_certified_auto_swap_engine(db)` returns an engine only when
`db._certified_auto_swap_engine` exists and is a
`CredentialTransactionEngine`. The production `Database` does not install this
attribute.

Therefore the normal `_execute_swap()` path:

1. records a `swap_log` row with `status="recommendation_only"`;
2. logs that no certified cooperative capability and writer fence are
   installed;
3. broadcasts `auto_swap_recommended` with the source ID, target ID, and
   reason when a WebSocket registry is available;
4. returns false without invoking a legacy credential writer.

This path does not:

- write Keychain or `.credentials.json`;
- create a credential transaction pending row;
- update `active_account_id` or `desired_account_id`;
- broadcast `auto_swap_triggered`;
- increment credential-write failure backoff;
- clear burn-rate history;
- mark sessions pending or change any session account label.

The decision log records the action as `recommend`, the trigger as
`recommendation_only`, and the candidate as the decision target.

## 6. Why background mutation is not certified

Credential capabilities are keyed to exact executable bytes, version, config
mode, platform, and architecture. The only shipped production record is Claude
`2.1.259` with a specific SHA-256 on macOS arm64 in global mode.

That record is `global_uncooperative`: macOS Keychain is authority,
`~/.claude/.credentials.json` is a required mirror, and all possible competing
writers cannot be fenced. `CredentialTransactionEngine` refuses a background
request for this mode with `restart_required` before writing.

Auto-swap requests use `InteractionMode.BACKGROUND`. Even if the shipped
global-uncooperative engine were incorrectly attached to the database, it
would not be a certified automatic switch path.

Linux, Windows, Intel macOS, other Claude builds, and scoped modes also have no
shipped mutation capability. The portable file-store code does not itself
certify Claude's consumption behavior on those platforms.

## 7. Requirements for a future automatic switch

A bootstrap may install a transaction engine only after resolving a matching
cooperative capability. That engine must declare:

- the runtime authority;
- every required mirror and optional metadata store;
- the consumers covered by the capability;
- a positive capability epoch;
- the minimum writer protocol epoch;
- a complete writer inspection;
- an installation recovery key and machine identity;
- a switch lease and secret-free snapshot sink.

For a cooperative transaction, the engine checks the writer fence before the
write and again after authority readback. It creates a secret-free pending
journal record before mutation and finalizes database pointers and audits only
after authority verification and required-mirror publication.

The auto-swap wrapper treats only `committed` and `committed_degraded` as a
successful automatic switch. `observed_target_unfenced` is not sufficient.

## 8. Certified-engine outcome handling

When an explicitly installed cooperative engine returns a committed outcome,
the wrapper:

- marks only known-global open sessions `pending`;
- records the committed time and clears swap failure state;
- invalidates the live credential cache;
- clears burn-rate history for the source and target;
- broadcasts `auto_swap_triggered`.

All journal rows, desired and committed pointers, and committed swap and
decision audits are owned by `DatabaseCredentialSwitchRepository.finalize()`.
The wrapper does not duplicate that bookkeeping.

Any non-committed result from an installed engine is treated as a failed
attempt for retry pacing. The wrapper arms exponential failure backoff,
preserves burn-rate history, does not broadcast `auto_swap_triggered`, and may
broadcast `auto_swap_failed`.

## 9. Session truth

An automatic credential commit does not prove that existing Claude processes
started using the new account. `mark_global_sessions_pending()` updates only
the observation state of open, known-global session rows. It does not change
their account ID or email, and it does not touch scoped or unknown rows.

Session hooks consume a fresh, secret-free resolver snapshot. A later hook
event may open a new configuration span only when it has resolved identity
evidence and a new transaction revision. Until then, the historical account
label remains unchanged.

The transaction result itself separates storage, committed authority,
existing-session activation, and provider verification. Current transactions
do not verify a provider request, so `provider_verification` remains
`unverified`.

## 10. Server event and audit contract

Credential-related WebSocket events have distinct meanings:

| Event | Meaning |
| --- | --- |
| `auto_swap_recommended` | Policy selected a target, but no certified automatic credential mutation occurred |
| `auto_swap_triggered` | A certified transaction returned `committed` or `committed_degraded` |
| `auto_swap_failed` | An installed transaction engine ran but did not commit |
| `all_accounts_exhausted` | A departure was needed but no eligible target existed |
| `auto_swap_stall` | The decision loop met its stale-data/no-candidate watchdog condition |
| `decision_log_entry` | Per-tick decision and candidate evidence |

`swap_log.status` must be interpreted, not merely counted. In particular,
`recommendation_only` is not a committed switch. The committed 24-hour churn
metric must remain committed-only.

The server broadcasts `auto_swap_recommended`, but the current dashboard
JavaScript has no handler for it. Persistence and server logs retain the
recommendation; the browser must not be described as showing a live
recommendation until a handler exists.

## 11. Usage refresh remains separate

The usage subsystem still owns primary-token refresh, 401 recovery, 429
backoff, circuit-breaker state, invalid-account healing, and window-keeper
activity. Those mechanisms update usage and account health, but they do not
authorize auto-swap credential mutation.

Legacy compatibility helpers still exist for narrowly scoped refresh and
reconciliation paths. `_execute_swap()` must not fall back to them when no
certified transaction engine exists.

## 12. Invariants

1. Selection is advisory until a cooperative exact-build capability and
   complete writer fence are installed.
2. A recommendation never changes credential stores or active/desired
   pointers.
3. `global_uncooperative` is foreground-only and cannot back auto-swap.
4. Only `committed` and `committed_degraded` count as automatic switch
   success.
5. Resolver conflict, missing authority, unsupported build, or unusable store
   means there is no safe active ID for the TOCTOU check.
6. The credential repository owns pending and final transaction publication;
   the monitor owns scheduling, runtime caches, backoff, and broadcasts.
7. A credential commit marks known-global sessions pending but never relabels
   existing sessions.
8. Provider use remains unverified until a provider-level observation proves
   it.
9. `auto_swap_recommended`, `auto_swap_triggered`, and `auto_swap_failed` are
   not interchangeable.
10. Portable storage primitives do not imply a certified cross-platform
    consumer capability.

## 13. Primary implementation map

| File | Responsibility |
| --- | --- |
| `jacked/api/usage_monitor.py` | Decision loop, TOCTOU guard, recommendation-only gate, runtime backoff and broadcasts |
| `jacked/web/auto_swap/selection.py` | Candidate ranking and departure rules |
| `jacked/web/auto_swap/tiers.py` | Urgency tiers, target usage, deficit calculations |
| `jacked/web/auto_swap/burn.py` | Burn projection and reset suppression helpers |
| `jacked/web/auto_swap/diagnostics.py` | Decision evidence and advisory calculations |
| `jacked/credentials/runtime.py` | Shipped exact-build capabilities |
| `jacked/credentials/transaction.py` | Certified switch outcomes and state machine |
| `jacked/credentials/writer_fence.py` | Complete-writer protocol and capability fence |
| `jacked/web/credential_repository.py` | Pending/final journal and pointer publication |
| `jacked/web/database.py` | Swap, decision, transaction, action, and session tables |
| `jacked/data/hooks/session_account_tracker.py` | Evidence-based session spans without existing-session relabelling |
