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
- Error responses include enough context to debug (correlation ID, timestamp, error code)

## Common anti-patterns

- Swallowing exceptions silently (empty catch blocks)
- Logging the error but returning success to the caller
- Using exceptions for flow control (try/catch instead of if/else)
- Retrying non-idempotent operations on failure
- Catch-all at the top level that hides the real error
- String-matching on error messages instead of using typed errors
- Missing timeout on external calls (HTTP, database, file I/O)
- Returning generic "Something went wrong" to users for all errors
- Nested try/catch that makes control flow unreadable

## When to apply

Any change that adds error handling, modifies exception flow, or touches
code that calls external services. Especially important for: API handlers,
background jobs, database operations, and any multi-step workflows.
