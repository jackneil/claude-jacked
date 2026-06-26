---
name: API Ergonomics
description: Consumer-friendly API design — naming, error contracts, discoverability, consistency
triggers: [api, route, endpoint, handler, rest, graphql, controller, resource]
---

# API Ergonomics Lens

## What to check

- Resource naming is consistent (plural nouns, kebab-case or snake_case — pick one)
- HTTP methods match semantics (GET reads, POST creates, PUT replaces, PATCH updates, DELETE removes)
- Error responses use consistent structure with machine-readable codes and human-readable messages.
  Prefer **RFC 9457 Problem Details** (`application/problem+json` with `type`/`title`/`detail`/
  `instance` + extension members), or a documented consistent house format
- **Validation errors come back as ONE response with a `validation[]` array** (each item
  `{field, message}`) — not multiple top-level errors, and not one failure at a time
- **Idempotency:** state-changing endpoints are safe to retry — naturally-idempotent verbs
  (PUT/DELETE), or an `Idempotency-Key` header for POSTs that create resources or trigger
  side effects (payments, sends)
- Pagination: **prefer cursor-based for large or append-heavy/real-time collections** (avoids
  skip/duplicate drift under concurrent writes); offset is acceptable for small, stable sets —
  make it a deliberate choice, and keep it consistent across list endpoints
- Filtering and sorting parameters follow a uniform convention
- Partial responses / field selection available for large resources
- **Versioning & deprecation lifecycle:** prefer additive, non-breaking changes (add fields;
  never repurpose/remove); when a break is unavoidable, bump the version AND signal end-of-life
  via `Deprecation`/`Sunset` headers with advance notice
- Authentication errors (401) vs authorization errors (403) are distinct
- Rate limiting headers are present (X-RateLimit-Limit, X-RateLimit-Remaining), and
  **429 (rate limited) / 503 (overloaded) responses include `Retry-After`** so clients back off
- **Long-running operations** that can't complete synchronously return `202 Accepted` with a
  status/polling URL (or a webhook), rather than blocking or timing out
- Request/response schemas are documented or self-describing

## Common anti-patterns

- Inconsistent naming across endpoints (users vs user vs getUsers)
- Returning 200 with an error body instead of proper HTTP status codes
- Nested URLs deeper than 2 levels (/orgs/123/teams/456/members/789/roles)
- Requiring clients to make multiple calls for data that naturally belongs together
- Breaking changes without version bump
- Different error formats from different endpoints
- Exposing internal IDs or implementation details in URLs
- Missing or incorrect Content-Type headers
- Accepting GET requests with side effects
- Retried POST creates duplicate resources / double-charges because no idempotency key is honored
- Returns the first validation failure only, forcing fix-resubmit-fail loops
- **Field/object-level authz gaps:** responses expose fields or objects the caller isn't
  authorized to see (over-fetching, IDOR via guessable IDs) even when 401/403 are otherwise correct

## When to apply

Any change that adds, modifies, or extends API endpoints — REST routes,
GraphQL resolvers, RPC handlers. Especially important for public-facing APIs
or APIs consumed by external teams.
