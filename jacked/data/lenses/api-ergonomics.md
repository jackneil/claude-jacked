---
name: API Ergonomics
description: Consumer-friendly API design — naming, error contracts, discoverability, consistency
triggers: [api, route, endpoint, handler, rest, graphql, controller, resource]
---

# API Ergonomics Lens

## What to check

- Resource naming is consistent (plural nouns, kebab-case or snake_case — pick one)
- HTTP methods match semantics (GET reads, POST creates, PUT replaces, PATCH updates, DELETE removes)
- Error responses use consistent structure with machine-readable codes and human-readable messages
- Pagination is consistent across all list endpoints (cursor-based or offset-based — pick one)
- Filtering and sorting parameters follow a uniform convention
- Partial responses / field selection available for large resources
- Versioning strategy is explicit (URL path, header, or query param)
- Authentication errors (401) vs authorization errors (403) are distinct
- Rate limiting headers are present (X-RateLimit-Limit, X-RateLimit-Remaining)
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

## When to apply

Any change that adds, modifies, or extends API endpoints — REST routes,
GraphQL resolvers, RPC handlers. Especially important for public-facing APIs
or APIs consumed by external teams.
