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
- Enum types use string representations, not magic integers
- Timestamps use timezone-aware types (timestamptz, not timestamp)
- Default values are specified for new non-nullable columns in migrations
- Unique constraints exist where business logic requires uniqueness

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

## When to apply

Any change involving database schemas, migrations, model definitions, or
complex queries. Especially important for: new tables, column additions to
large tables, index changes, and multi-table transactions.
