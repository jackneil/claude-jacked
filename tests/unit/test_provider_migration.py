"""M1: provider-aware account model + crash-safe migration.

Covers:
- fresh DBs get a `provider` column defaulting to 'claude'
- UNIQUE is provider-scoped: same (email, org) can exist under claude AND codex
- an existing (pre-provider) DB migrates: every row -> provider='claude',
  and a codex row then coexists with a same-email claude row
- create_account upsert is provider-keyed
- get_account_by_email can scope by provider (no cross-provider ambiguity)
- per-provider active-account setting (claude key stays 'active_account_id')
"""

import sqlite3

import pytest

from jacked.web.database import Database

# The accounts DDL exactly as it shipped BEFORE the provider column existed —
# a realistic "existing user DB" (composite UNIQUE on email+org, no provider).
LEGACY_ACCOUNTS_DDL = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    organization_uuid TEXT DEFAULT '',
    organization_name TEXT,
    display_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,
    scopes TEXT,
    subscription_type TEXT,
    rate_limit_tier TEXT,
    has_extra_usage BOOLEAN DEFAULT FALSE,
    priority INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    is_deleted BOOLEAN DEFAULT FALSE,
    last_used_at TIMESTAMP,
    cached_usage_5h REAL,
    cached_usage_7d REAL,
    cached_5h_resets_at TEXT,
    cached_7d_resets_at TEXT,
    usage_cached_at INTEGER,
    cached_usage_raw TEXT,
    last_error TEXT,
    last_error_at TIMESTAMP,
    consecutive_failures INTEGER DEFAULT 0,
    last_validated_at INTEGER,
    validation_status TEXT DEFAULT 'unknown',
    cc_access_token TEXT,
    cc_refresh_token TEXT,
    cc_expires_at INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(email, organization_uuid)
);
"""


def _columns(db: Database, table: str = "accounts") -> set:
    with db._reader() as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _unique_indexes_cover_provider(db: Database) -> bool:
    """True if some UNIQUE index on accounts includes the provider column."""
    with db._reader() as conn:
        for idx in conn.execute("PRAGMA index_list(accounts)").fetchall():
            # idx = (seq, name, unique, origin, partial)
            if not idx[2]:
                continue
            cols = [r[2] for r in conn.execute(
                f"PRAGMA index_info('{idx[1]}')"
            ).fetchall()]
            if "provider" in cols and "email" in cols:
                return True
    return False


# --------------------------------------------------------------------------
# Fresh DB
# --------------------------------------------------------------------------

def test_fresh_db_has_provider_column():
    db = Database(":memory:")
    assert "provider" in _columns(db)


def test_fresh_account_defaults_to_claude():
    db = Database(":memory:")
    acct = db.create_account("a@example.com", "tok", 9999999999)
    assert acct["provider"] == "claude"


def test_fresh_db_unique_is_provider_scoped():
    db = Database(":memory:")
    claude = db.create_account("dup@example.com", "ctok", 9999999999, provider="claude")
    codex = db.create_account("dup@example.com", "xtok", 9999999999, provider="codex")
    assert claude["id"] != codex["id"]
    assert claude["provider"] == "claude"
    assert codex["provider"] == "codex"
    # both live, distinct rows
    rows = db.list_accounts()
    providers = sorted(r["provider"] for r in rows if r["email"] == "dup@example.com")
    assert providers == ["claude", "codex"]


def test_unique_index_includes_provider():
    db = Database(":memory:")
    assert _unique_indexes_cover_provider(db)


# --------------------------------------------------------------------------
# Migration of an existing (pre-provider) DB
# --------------------------------------------------------------------------

def test_migration_adds_provider_and_backfills_claude(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    # Build a pre-provider DB by hand and seed two rows.
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_ACCOUNTS_DDL)
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, access_token, expires_at) "
        "VALUES ('legacy@example.com', '', 'tok', 9999999999)"
    )
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, access_token, expires_at) "
        "VALUES ('other@example.com', 'org-123', 'tok2', 9999999999)"
    )
    conn.commit()
    conn.close()

    # Opening through Database triggers the migration.
    db = Database(db_path)
    assert "provider" in _columns(db)

    rows = db.list_accounts()
    assert len(rows) == 2
    assert all(r["provider"] == "claude" for r in rows)
    assert _unique_indexes_cover_provider(db)


def test_migration_preserves_existing_data(tmp_path):
    db_path = str(tmp_path / "legacy2.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_ACCOUNTS_DDL)
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, organization_name, "
        "access_token, refresh_token, expires_at, subscription_type, priority) "
        "VALUES ('keep@example.com', 'org-x', 'Acme', 'tok', 'rtok', 9999999999, 'max', 3)"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    acct = db.get_account_by_email("keep@example.com", provider="claude")
    assert acct is not None
    assert acct["organization_name"] == "Acme"
    assert acct["subscription_type"] == "max"
    assert acct["priority"] == 3
    assert acct["provider"] == "claude"


def test_migration_then_codex_coexists_with_same_email(tmp_path):
    db_path = str(tmp_path / "legacy3.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_ACCOUNTS_DDL)
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, access_token, expires_at) "
        "VALUES ('shared@example.com', '', 'tok', 9999999999)"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    # The migrated row is the claude one; adding a codex with the SAME email+org
    # must NOT collide (it would under the old UNIQUE(email, organization_uuid)).
    codex = db.create_account("shared@example.com", "xtok", 9999999999, provider="codex")
    assert codex["provider"] == "codex"
    rows = sorted(
        (r for r in db.list_accounts() if r["email"] == "shared@example.com"),
        key=lambda r: r["provider"],
    )
    assert [r["provider"] for r in rows] == ["claude", "codex"]


ACCOUNTS_NEW_DDL = (
    LEGACY_ACCOUNTS_DDL
    .replace("CREATE TABLE accounts", "CREATE TABLE accounts_new")
    .replace(
        "    email TEXT NOT NULL,",
        "    provider TEXT NOT NULL DEFAULT 'claude',\n    email TEXT NOT NULL,",
    )
    .replace("UNIQUE(email, organization_uuid)", "UNIQUE(provider, email, organization_uuid)")
)


def test_migration_recovers_from_crashed_rebuild(tmp_path):
    """Simulate a crash between DROP accounts and RENAME accounts_new: an
    accounts_new (already provider-aware) exists and accounts does NOT. Opening
    must recover via the pre-schema rename — the crash-safe half of the migration."""
    db_path = str(tmp_path / "crashed.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(ACCOUNTS_NEW_DDL)  # only accounts_new, no accounts
    conn.execute(
        "INSERT INTO accounts_new (provider, email, organization_uuid, access_token, expires_at) "
        "VALUES ('claude', 'recover@example.com', '', 'tok', 9999999999)"
    )
    conn.commit()
    conn.close()

    db = Database(db_path)  # pre-schema recovery renames accounts_new -> accounts
    assert "provider" in _columns(db)
    rows = db.list_accounts()
    assert len(rows) == 1
    assert rows[0]["email"] == "recover@example.com"
    assert rows[0]["provider"] == "claude"


def test_migration_idempotent(tmp_path):
    """Re-opening an already-migrated DB must not rebuild or error."""
    db_path = str(tmp_path / "legacy4.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_ACCOUNTS_DDL)
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, access_token, expires_at) "
        "VALUES ('idem@example.com', '', 'tok', 9999999999)"
    )
    conn.commit()
    conn.close()

    db1 = Database(db_path)
    db1.close()
    db2 = Database(db_path)  # second open — provider already present
    rows = db2.list_accounts()
    assert len(rows) == 1
    assert rows[0]["provider"] == "claude"


# --------------------------------------------------------------------------
# create_account upsert keyed on provider
# --------------------------------------------------------------------------

def test_create_account_upsert_same_provider_updates_in_place():
    db = Database(":memory:")
    first = db.create_account("u@example.com", "tok1", 9999999999, provider="codex")
    second = db.create_account("u@example.com", "tok2", 9999999999, provider="codex")
    assert first["id"] == second["id"]
    assert len([r for r in db.list_accounts() if r["email"] == "u@example.com"]) == 1


# --------------------------------------------------------------------------
# get_account_by_email provider scoping
# --------------------------------------------------------------------------

def test_get_account_by_email_scoped_by_provider():
    db = Database(":memory:")
    db.create_account("two@example.com", "ctok", 9999999999, provider="claude")
    db.create_account("two@example.com", "xtok", 9999999999, provider="codex")

    claude = db.get_account_by_email("two@example.com", provider="claude")
    codex = db.get_account_by_email("two@example.com", provider="codex")
    assert claude["provider"] == "claude"
    assert codex["provider"] == "codex"
    # Without a provider filter, the cross-provider duplicate is ambiguous.
    with pytest.raises(ValueError):
        db.get_account_by_email("two@example.com")


# --------------------------------------------------------------------------
# Per-provider active-account setting
# --------------------------------------------------------------------------

def test_active_account_id_is_per_provider():
    db = Database(":memory:")
    db.set_active_account_id(5, provider="claude")
    db.set_active_account_id(9, provider="codex")
    assert db.get_active_account_id("claude") == 5
    assert db.get_active_account_id("codex") == 9
    # back-compat: the claude active account is stored under the legacy key
    assert db.get_setting("active_account_id") == "5"


def test_active_account_id_absent_returns_none():
    db = Database(":memory:")
    assert db.get_active_account_id("codex") is None


# --------------------------------------------------------------------------
# AccountResponse carries provider
# --------------------------------------------------------------------------

def test_account_response_includes_provider():
    from jacked.api.routes.auth import _account_to_response

    row = {
        "id": 1,
        "email": "x@example.com",
        "provider": "codex",
        "expires_at": 9999999999,
        "priority": 0,
    }
    resp = _account_to_response(row)
    assert resp.provider == "codex"


def test_account_response_provider_defaults_claude_when_missing():
    from jacked.api.routes.auth import _account_to_response

    row = {"id": 1, "email": "x@example.com", "expires_at": 9999999999, "priority": 0}
    resp = _account_to_response(row)
    assert resp.provider == "claude"
