---
name: Database Design
description: Schema normalization, index strategy, migration safety, data integrity
triggers: [schema, migration, model, sql, database, orm, table, column, index, query]
---

# Database Design Lens

## What to check

- New columns have appropriate NOT NULL constraints (nullable only when semantically correct)
- Foreign keys have ON DELETE behavior specified (CASCADE, SET NULL, RESTRICT)
- Indexes exist for columns used in WHERE, JOIN, and ORDER BY clauses
- Composite indexes have columns in selectivity order (most selective first)
- Migrations are backward-compatible (can roll back without data loss)
- Large table migrations avoid locking (use batched updates, not ALTER TABLE on hot tables)
- **Index builds on large/production tables use `CREATE INDEX CONCURRENTLY`** (and
  `DROP INDEX CONCURRENTLY`) — a plain `CREATE INDEX` takes an ACCESS EXCLUSIVE lock and blocks
  all writes for the whole build. Note `CONCURRENTLY` cannot run inside a transaction
- **Add constraints as `NOT VALID` first, then `VALIDATE CONSTRAINT` separately:** FKs, CHECKs,
  and `SET NOT NULL` on existing columns. The `NOT VALID` add is instant; `VALIDATE` takes a
  non-blocking ShareUpdateExclusive lock. For NOT NULL on PG12+, add a `CHECK (col IS NOT NULL)
  NOT VALID`, validate it, then promote
- **Migrations set a short `lock_timeout` (and a `statement_timeout`)** so a migration blocked
  behind a long transaction fails fast instead of queuing all traffic; prefer lock-timeout
  retries so a timed-out non-concurrent op doesn't leave a half-applied/INVALID index
- Enum types use string representations, not magic integers
- Timestamps use timezone-aware types (timestamptz, not timestamp)
- Default values are specified for new non-nullable columns in migrations — but a **CONSTANT**
  default is instant on PG11+, while a **VOLATILE** default (`now()`, `gen_random_uuid()`,
  `random()`) rewrites the whole table → use add-nullable → batched backfill → SET NOT NULL
- **Expand–contract for renames/drops/type changes:** split into add-new → dual-write/backfill →
  drop-old across separate deploys; the app must work against BOTH old and new schema during
  rollout (drain old app instances before dropping a column)
- Unique constraints exist where business logic requires uniqueness
- **After any `CONCURRENTLY` op, verify the index is valid** (`pg_index.indisvalid`) and
  `DROP INDEX CONCURRENTLY` any leftover INVALID index before retrying

## Common anti-patterns

- Adding NOT NULL column without default to existing table (breaks migration on non-empty tables)
- Missing indexes on foreign key columns (causes slow joins)
- Using LIKE '%term%' on unindexed text columns
- N+1 queries from ORM lazy loading
- Storing JSON blobs instead of normalized columns for structured data
- Using FLOAT for money (use DECIMAL or integer cents)
- Missing created_at/updated_at columns on mutable tables
- Cascading deletes that could wipe large amounts of data unexpectedly
- Schema migrations that are not idempotent
- **Backfilling in one giant `UPDATE`** instead of committing per batch, sleeping between
  batches, and watching WAL bloat / replication lag

## When to apply

Any change involving database schemas, migrations, model definitions, or
complex queries. Especially important for: new tables, column additions to
large tables, index changes, and multi-table transactions.

**Tip:** wire a migration linter (`squawk` for Postgres, or `strong_migrations` /
`safe-pg-migrations` in Ruby) into CI to fail PRs containing unsafe DDL automatically.
