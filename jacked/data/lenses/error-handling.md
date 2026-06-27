---
name: Error Handling
description: Exception strategy, error propagation, failure recovery, user-facing error messages
triggers: [error, exception, catch, try, handler, middleware, fault, failure, retry]
---

# Error Handling Lens

## What to check

- Catch blocks handle specific exception types, not bare except/catch-all
- Error context is preserved when re-raising (use `raise ... from e` or equivalent)
- User-facing error messages are helpful without leaking internals
- Transient failures have retry logic with exponential backoff and jitter
- Resource cleanup happens in finally blocks or context managers
- Error boundaries exist at system boundaries (API handlers, message consumers, job runners)
- Validation errors are collected and returned together, not one at a time
- Expected errors (user input, network) are handled differently from unexpected errors (bugs)
- Async operations have timeout and cancellation handling
- Error responses include enough context to debug (correlation ID, timestamp, error code).
  Prefer a structured machine-readable body (RFC 9457 `application/problem+json`:
  `type`/`title`/`status`/`detail`/`instance`)
- **Resilience for external calls:** retries are **bounded** (max attempts AND max total
  elapsed time) and applied at **exactly one layer**, not stacked across layers; a
  **circuit breaker** (or equivalent fail-fast) protects calls to a dependency that may be
  down; **connection timeout and request timeout are set separately**, the request timeout
  derived from the downstream's p99/p99.9, and inbound deadlines are propagated downstream
- **Idempotent writes:** a write that may be retried carries an idempotency key
  (client token / `Idempotency-Key`), the server dedupes on it (store-with-TTL, replay the
  original result, reject same-key/different-body), and **a timeout on a write is treated as
  INDETERMINATE** (requires reconciliation), never assumed-failed
- **Async / queue & workflows:** consumers cap retries and route poison messages to a
  **Dead Letter Queue** (with depth/age monitoring + a reprocessing plan) instead of retrying
  forever; consumers dedupe (delivery is at-least-once); multi-step workflows that partially
  succeed define a rollback via **compensating transactions (saga)**
- **Graceful degradation:** when retries/breaker exhaust, there is a defined fallback (stale
  cache, simplified response, disable a non-essential feature, or fail fast) — and the fallback
  path is simpler and more reliable than the primary
- **Observability of error handling:** emit + alert on retry-rate vs success-rate (and
  max-attempt-cap hits), circuit-breaker state transitions/flapping, DLQ depth + message age,
  timeout rate, and fallback invocation/success — so a retry storm is visible before customers feel it

## Common anti-patterns

- Swallowing exceptions silently (empty catch blocks)
- Logging the error but returning success to the caller
- Using exceptions for flow control (try/catch instead of if/else)
- Retrying a non-idempotent write with no idempotency key (duplicates / double effects)
- Catch-all at the top level that hides the real error
- String-matching on error messages instead of using typed errors
- Missing timeout on external calls (HTTP, database, file I/O)
- Returning generic "Something went wrong" to users for all errors
- Nested try/catch that makes control flow unreadable
- **Invoking a remote/network call while holding an open DB transaction or pooled connection**
  (exhausts the pool under downstream slowness)
- **No defined unit-of-failure for batch/partial-success operations** — the caller can't tell
  what succeeded and what didn't

## When to apply

Any change that adds error handling, modifies exception flow, or touches
code that calls external services. Especially important for: API handlers,
background jobs, database operations, and any multi-step workflows.
