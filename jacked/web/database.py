"""SQLite database layer for jacked web dashboard.

9 tables across three concerns:
- Account management: accounts, installations, settings
- Analytics: gatekeeper_decisions, command_usage, agent_invocations,
             hook_executions, lessons, version_checks

WAL mode for concurrent reads, single writer lock for atomic writes.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field, computed_field


# ---------------------------------------------------------------------------
# Pydantic v2 Models
# ---------------------------------------------------------------------------

class Account(BaseModel):
    """Pydantic v2 model for an account row."""

    id: int
    email: str
    display_name: Optional[str] = None
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: int
    scopes: Optional[str] = None
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    has_extra_usage: bool = False
    priority: int = 0
    is_active: bool = True
    is_deleted: bool = False
    last_used_at: Optional[str] = None
    cached_usage_5h: Optional[float] = None
    cached_usage_7d: Optional[float] = None
    cached_5h_resets_at: Optional[str] = None
    cached_7d_resets_at: Optional[str] = None
    usage_cached_at: Optional[int] = None
    cached_usage_raw: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    consecutive_failures: int = 0
    last_validated_at: Optional[int] = None
    validation_status: str = "unknown"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @computed_field
    @property
    def is_default(self) -> bool:
        """Primary account is the one with priority == 0."""
        return self.priority == 0

    @computed_field
    @property
    def is_expired(self) -> bool:
        """Token is expired when current time >= expires_at."""
        return int(time.time()) >= self.expires_at


class Installation(BaseModel):
    """Pydantic v2 model for an installation row."""

    id: int
    repo_path: str
    repo_name: str
    jacked_version: Optional[str] = None
    hooks_installed: Optional[str] = None
    rules_installed: bool = False
    agents_installed: Optional[str] = None
    commands_installed: Optional[str] = None
    guardrails_installed: bool = False
    env_path: Optional[str] = None
    last_scanned_at: Optional[str] = None
    created_at: Optional[str] = None


class Setting(BaseModel):
    """Pydantic v2 model for a settings row."""

    key: str
    value: str
    updated_at: Optional[str] = None


class GatekeeperDecision(BaseModel):
    """Pydantic v2 model for a gatekeeper_decisions row."""

    id: int
    timestamp: str
    command: Optional[str] = None
    decision: str
    method: Optional[str] = None
    reason: Optional[str] = None
    elapsed_ms: Optional[float] = None
    session_id: Optional[str] = None
    repo_path: Optional[str] = None


class CommandUsage(BaseModel):
    """Pydantic v2 model for a command_usage row."""

    id: int
    command_name: str
    timestamp: str
    session_id: Optional[str] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    repo_path: Optional[str] = None


class AgentInvocation(BaseModel):
    """Pydantic v2 model for an agent_invocations row."""

    id: int
    agent_name: str
    timestamp: str
    session_id: Optional[str] = None
    spawned_by: Optional[str] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    tasks_completed: int = 0
    errors: int = 0
    repo_path: Optional[str] = None


class HookExecution(BaseModel):
    """Pydantic v2 model for a hook_executions row."""

    id: int
    hook_type: str
    hook_name: Optional[str] = None
    timestamp: str
    session_id: Optional[str] = None
    success: Optional[bool] = None
    duration_ms: Optional[float] = None
    error_msg: Optional[str] = None
    repo_path: Optional[str] = None


class Lesson(BaseModel):
    """Pydantic v2 model for a lessons row."""

    id: int
    content: str
    project_id: Optional[str] = None
    failure_count: int = 1
    status: str = "learning"
    graduation_date: Optional[str] = None
    source_session_id: Optional[str] = None
    tags: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class VersionCheck(BaseModel):
    """Pydantic v2 model for a version_checks row."""

    id: int
    timestamp: str
    current_version: str
    latest_version: str
    outdated: Optional[bool] = None
    cache_hit: Optional[bool] = None


# ---------------------------------------------------------------------------
# Schema SQL — exact DDL from design doc section 3
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Account Management Tables
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS installations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path TEXT NOT NULL UNIQUE,
    repo_name TEXT NOT NULL,
    jacked_version TEXT,
    hooks_installed TEXT,
    rules_installed BOOLEAN DEFAULT FALSE,
    agents_installed TEXT,
    commands_installed TEXT,
    guardrails_installed BOOLEAN DEFAULT FALSE,
    last_scanned_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Analytics Tables
CREATE TABLE IF NOT EXISTS gatekeeper_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    command TEXT,
    decision TEXT NOT NULL,
    method TEXT,
    reason TEXT,
    elapsed_ms REAL,
    session_id TEXT,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS command_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    success BOOLEAN,
    duration_ms REAL,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS agent_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    spawned_by TEXT,
    success BOOLEAN,
    duration_ms REAL,
    tasks_completed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS hook_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_type TEXT NOT NULL,
    hook_name TEXT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    success BOOLEAN,
    duration_ms REAL,
    error_msg TEXT,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    project_id TEXT,
    failure_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'learning',
    graduation_date TEXT,
    source_session_id TEXT,
    tags TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS version_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    current_version TEXT NOT NULL,
    latest_version TEXT NOT NULL,
    outdated BOOLEAN,
    cache_hit BOOLEAN
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active, is_deleted);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
CREATE INDEX IF NOT EXISTS idx_accounts_priority ON accounts(priority);
CREATE INDEX IF NOT EXISTS idx_installations_repo ON installations(repo_path);
CREATE INDEX IF NOT EXISTS idx_gatekeeper_timestamp ON gatekeeper_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_gatekeeper_decision ON gatekeeper_decisions(decision);
CREATE INDEX IF NOT EXISTS idx_command_usage_name ON command_usage(command_name);
CREATE INDEX IF NOT EXISTS idx_command_usage_ts ON command_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_invocations_name ON agent_invocations(agent_name);
CREATE INDEX IF NOT EXISTS idx_hook_executions_type ON hook_executions(hook_type);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE INDEX IF NOT EXISTS idx_version_checks_ts ON version_checks(timestamp);
CREATE INDEX IF NOT EXISTS idx_gatekeeper_repo ON gatekeeper_decisions(repo_path);
CREATE INDEX IF NOT EXISTS idx_command_usage_repo ON command_usage(repo_path);
CREATE INDEX IF NOT EXISTS idx_hook_executions_repo ON hook_executions(repo_path);
"""


def _default_db_path() -> str:
    """Return default database path: ~/.claude/jacked.db"""
    return str(Path.home() / ".claude" / "jacked.db")


class Database:
    """SQLite database manager with WAL mode and thread-safe writes.

    >>> db = Database(":memory:")
    >>> db.db_path
    ':memory:'
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = _default_db_path()

        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._local = threading.local()

        # Create parent dir + file if needed (skip for :memory:)
        if db_path != ":memory:" and not Path(db_path).exists():
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            Path(db_path).touch()

        self._init_schema()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.connection = conn
        return self._local.connection

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        yield self._get_connection()

    def _init_schema(self) -> None:
        with self._writer() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executescript(INDEXES_SQL)
            # Migration: add cached_usage_raw if missing (existing DBs)
            cursor = conn.execute("PRAGMA table_info(accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            if "cached_usage_raw" not in cols:
                try:
                    conn.execute("ALTER TABLE accounts ADD COLUMN cached_usage_raw TEXT")
                except sqlite3.OperationalError:
                    pass  # another worker beat us to it
            # Migration: add env_path to installations
            cursor = conn.execute("PRAGMA table_info(installations)")
            cols = {row[1] for row in cursor.fetchall()}
            if "env_path" not in cols:
                try:
                    conn.execute("ALTER TABLE installations ADD COLUMN env_path TEXT")
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None

    # ==================================================================
    # Account CRUD
    # ==================================================================

    def create_account(
        self,
        email: str,
        access_token: str,
        expires_at: int,
        refresh_token: Optional[str] = None,
        display_name: Optional[str] = None,
        scopes: Optional[str] = None,
        subscription_type: Optional[str] = None,
        rate_limit_tier: Optional[str] = None,
        has_extra_usage: bool = False,
    ) -> dict:
        """Create a new account or update if email already exists.

        Handles the design doc edge cases:
        - Existing deleted account with same email: undelete and update
        - Existing active account with same email: update tokens in place

        >>> db = Database(":memory:")
        >>> acct = db.create_account("test@example.com", "sk-ant-test", 9999999999)
        >>> acct["email"]
        'test@example.com'
        """
        now = datetime.utcnow().isoformat()

        with self._writer() as conn:
            # Determine priority for new accounts
            cursor = conn.execute("SELECT COUNT(*) FROM accounts WHERE is_deleted = 0")
            count = cursor.fetchone()[0]
            if count == 0:
                priority = 0
            else:
                cursor = conn.execute(
                    "SELECT MAX(COALESCE(priority, 0)) FROM accounts WHERE is_deleted = 0"
                )
                max_pri = cursor.fetchone()[0] or 0
                priority = max_pri + 1

            cursor = conn.execute(
                """INSERT INTO accounts (
                    email, access_token, refresh_token, expires_at, display_name,
                    scopes, subscription_type, rate_limit_tier, has_extra_usage,
                    priority, is_active, is_deleted, consecutive_failures,
                    validation_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 'unknown', ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scopes = excluded.scopes,
                    subscription_type = COALESCE(excluded.subscription_type, subscription_type),
                    rate_limit_tier = COALESCE(excluded.rate_limit_tier, rate_limit_tier),
                    has_extra_usage = excluded.has_extra_usage,
                    is_active = 1,
                    is_deleted = 0,
                    consecutive_failures = 0,
                    validation_status = 'unknown',
                    updated_at = excluded.updated_at
                """,
                (
                    email, access_token, refresh_token, expires_at, display_name,
                    scopes, subscription_type, rate_limit_tier, has_extra_usage,
                    priority, now, now,
                ),
            )

            cursor = conn.execute("SELECT * FROM accounts WHERE email = ?", (email,))
            row = cursor.fetchone()
            return dict(row) if row else {}

    def get_account(self, account_id: int) -> Optional[dict]:
        """Get an account by ID (excludes soft-deleted).

        >>> db = Database(":memory:")
        >>> db.get_account(999) is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM accounts WHERE id = ? AND is_deleted = 0",
                (account_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_account_by_email(self, email: str) -> Optional[dict]:
        """Get an account by email (excludes soft-deleted).

        >>> db = Database(":memory:")
        >>> db.get_account_by_email("nobody@nowhere.com") is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM accounts WHERE email = ? AND is_deleted = 0",
                (email,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_accounts(
        self,
        include_inactive: bool = False,
        include_deleted: bool = False,
    ) -> list[dict]:
        """List accounts ordered by priority.

        >>> db = Database(":memory:")
        >>> db.list_accounts()
        []
        """
        conditions = []
        if not include_deleted:
            conditions.append("is_deleted = 0")
        if not include_inactive:
            conditions.append("is_active = 1")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._reader() as conn:
            cursor = conn.execute(
                f"SELECT * FROM accounts {where} ORDER BY COALESCE(priority, 0) ASC, created_at ASC"
            )
            return [dict(row) for row in cursor.fetchall()]

    # Whitelist of columns allowed in update_account
    _ACCOUNT_UPDATE_COLS = frozenset({
        "display_name", "access_token", "refresh_token", "expires_at",
        "scopes", "subscription_type", "rate_limit_tier", "has_extra_usage",
        "is_active", "last_used_at", "priority",
        "cached_usage_5h", "cached_usage_7d",
        "cached_5h_resets_at", "cached_7d_resets_at", "usage_cached_at", "cached_usage_raw",
        "last_error", "last_error_at", "consecutive_failures",
        "last_validated_at", "validation_status",
    })

    def update_account(self, account_id: int, **kwargs: Any) -> bool:
        """Update an account by ID.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@test.com", "tok", 9999999999)
        >>> db.update_account(acct["id"], display_name="Test User")
        True
        """
        if not kwargs:
            return False

        invalid_cols = set(kwargs.keys()) - self._ACCOUNT_UPDATE_COLS - {"updated_at"}
        if invalid_cols:
            raise ValueError(f"Invalid columns for account update: {invalid_cols}")

        kwargs["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [account_id]

        with self._writer() as conn:
            cursor = conn.execute(
                f"UPDATE accounts SET {set_clause} WHERE id = ? AND is_deleted = 0",
                values,
            )
            return cursor.rowcount > 0

    def delete_account(self, account_id: int) -> bool:
        """Soft-delete an account.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("del@test.com", "tok", 9999999999)
        >>> db.delete_account(acct["id"])
        True
        >>> db.get_account(acct["id"]) is None
        True
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
                (now, account_id),
            )
            return cursor.rowcount > 0

    def reorder_accounts(self, account_ids: list[int]) -> None:
        """Reorder accounts — index position becomes priority value.

        >>> db = Database(":memory:")
        >>> a1 = db.create_account("a@t.com", "tok", 9999999999)
        >>> a2 = db.create_account("b@t.com", "tok", 9999999999)
        >>> db.reorder_accounts([a2["id"], a1["id"]])
        >>> accounts = db.list_accounts()
        >>> accounts[0]["email"]
        'b@t.com'
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            for i, aid in enumerate(account_ids):
                conn.execute(
                    "UPDATE accounts SET priority = ?, updated_at = ? WHERE id = ?",
                    (i, now, aid),
                )

    def get_default_account(self) -> Optional[dict]:
        """Get the primary account (lowest priority among active, non-deleted).

        >>> db = Database(":memory:")
        >>> db.get_default_account() is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT * FROM accounts
                   WHERE is_active = 1 AND is_deleted = 0
                   ORDER BY COALESCE(priority, 0) ASC, created_at ASC
                   LIMIT 1"""
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_fallback_account(self, exclude_ids: Optional[list[int]] = None) -> Optional[dict]:
        """Get a fallback account using the design doc ordering from section 13.

        >>> db = Database(":memory:")
        >>> db.get_fallback_account() is None
        True
        """
        exclude_ids = exclude_ids or []

        with self._reader() as conn:
            placeholders = ",".join("?" for _ in exclude_ids) if exclude_ids else ""
            exclude_clause = f"AND id NOT IN ({placeholders})" if exclude_ids else ""

            cursor = conn.execute(
                f"""SELECT * FROM accounts
                    WHERE is_active = 1
                      AND is_deleted = 0
                      AND consecutive_failures < 3
                      {exclude_clause}
                    ORDER BY
                        priority ASC,
                        COALESCE(cached_usage_5h, 0) ASC,
                        COALESCE(cached_usage_7d, 0) ASC,
                        consecutive_failures ASC,
                        created_at ASC
                    LIMIT 1""",
                exclude_ids,
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_account_usage_cache(
        self,
        account_id: int,
        five_hour: Optional[float] = None,
        seven_day: Optional[float] = None,
        five_hour_resets_at: Optional[str] = None,
        seven_day_resets_at: Optional[str] = None,
        raw: Optional[dict] = None,
    ) -> bool:
        """Update cached usage data for an account.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@t.com", "tok", 9999999999)
        >>> db.update_account_usage_cache(acct["id"], five_hour=42.5)
        True
        >>> db.update_account_usage_cache(acct["id"], raw={"test": "data"})
        True
        """
        updates: dict[str, Any] = {"usage_cached_at": int(time.time())}
        if five_hour is not None:
            updates["cached_usage_5h"] = five_hour
        if seven_day is not None:
            updates["cached_usage_7d"] = seven_day
        if five_hour_resets_at is not None:
            updates["cached_5h_resets_at"] = five_hour_resets_at
        if seven_day_resets_at is not None:
            updates["cached_7d_resets_at"] = seven_day_resets_at
        if raw is not None:
            raw_str = json.dumps(raw)
            if len(raw_str) <= 10240:  # 10KB guard
                updates["cached_usage_raw"] = raw_str
        return self.update_account(account_id, **updates)

    def record_account_error(
        self,
        account_id: int,
        error_message: str,
        increment_failures: bool = True,
    ) -> bool:
        """Record an error for an account.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@t.com", "tok", 9999999999)
        >>> db.record_account_error(acct["id"], "test error")
        True
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            if increment_failures:
                cursor = conn.execute(
                    """UPDATE accounts SET
                        last_error = ?, last_error_at = ?,
                        consecutive_failures = consecutive_failures + 1,
                        updated_at = ?
                       WHERE id = ?""",
                    (error_message, now, now, account_id),
                )
            else:
                cursor = conn.execute(
                    """UPDATE accounts SET
                        last_error = ?, last_error_at = ?,
                        updated_at = ?
                       WHERE id = ?""",
                    (error_message, now, now, account_id),
                )
            return cursor.rowcount > 0

    def clear_account_errors(self, account_id: int) -> bool:
        """Clear error state for an account.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@t.com", "tok", 9999999999)
        >>> db.clear_account_errors(acct["id"])
        True
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE accounts SET
                    last_error = NULL, last_error_at = NULL,
                    consecutive_failures = 0, last_used_at = ?,
                    updated_at = ?
                   WHERE id = ?""",
                (now, now, account_id),
            )
            return cursor.rowcount > 0

    # ==================================================================
    # Installation CRUD
    # ==================================================================

    def create_installation(
        self,
        repo_path: str,
        repo_name: str,
        jacked_version: Optional[str] = None,
        hooks_installed: Optional[str] = None,
        rules_installed: bool = False,
        agents_installed: Optional[str] = None,
        commands_installed: Optional[str] = None,
        guardrails_installed: bool = False,
    ) -> dict:
        """Create or update an installation record.

        >>> db = Database(":memory:")
        >>> inst = db.create_installation("/repo", "my-repo")
        >>> inst["repo_name"]
        'my-repo'
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            conn.execute(
                """INSERT INTO installations (
                    repo_path, repo_name, jacked_version, hooks_installed,
                    rules_installed, agents_installed, commands_installed,
                    guardrails_installed, last_scanned_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_path) DO UPDATE SET
                    repo_name = excluded.repo_name,
                    jacked_version = excluded.jacked_version,
                    hooks_installed = excluded.hooks_installed,
                    rules_installed = excluded.rules_installed,
                    agents_installed = excluded.agents_installed,
                    commands_installed = excluded.commands_installed,
                    guardrails_installed = excluded.guardrails_installed,
                    last_scanned_at = excluded.last_scanned_at
                """,
                (
                    repo_path, repo_name, jacked_version, hooks_installed,
                    rules_installed, agents_installed, commands_installed,
                    guardrails_installed, now, now,
                ),
            )
            cursor = conn.execute(
                "SELECT * FROM installations WHERE repo_path = ?", (repo_path,)
            )
            row = cursor.fetchone()
            return dict(row) if row else {}

    def list_installations(self) -> list[dict]:
        """List all installations.

        >>> db = Database(":memory:")
        >>> db.list_installations()
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM installations ORDER BY repo_name ASC"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_installation(self, installation_id: int) -> Optional[dict]:
        """Get an installation by ID.

        >>> db = Database(":memory:")
        >>> db.get_installation(999) is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM installations WHERE id = ?", (installation_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_installation(self, installation_id: int) -> bool:
        """Delete an installation.

        >>> db = Database(":memory:")
        >>> inst = db.create_installation("/repo", "my-repo")
        >>> db.delete_installation(inst["id"])
        True
        """
        with self._writer() as conn:
            cursor = conn.execute(
                "DELETE FROM installations WHERE id = ?", (installation_id,)
            )
            return cursor.rowcount > 0

    def update_installation_env(self, repo_path: str, env_path: str) -> bool:
        """Update env_path for an installation by repo_path.

        >>> db = Database(":memory:")
        >>> inst = db.create_installation("/repo", "my-repo")
        >>> db.update_installation_env("/repo", "/some/env")
        True
        """
        with self._writer() as conn:
            cursor = conn.execute(
                "UPDATE installations SET env_path = ? WHERE repo_path = ?",
                (env_path, repo_path),
            )
            return cursor.rowcount > 0

    def get_installation_by_repo(self, repo_path: str) -> Optional[dict]:
        """Get an installation by repo_path.

        >>> db = Database(":memory:")
        >>> db.get_installation_by_repo("/nonexistent") is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM installations WHERE repo_path = ?", (repo_path,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # ==================================================================
    # Settings CRUD
    # ==================================================================

    def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key.

        >>> db = Database(":memory:")
        >>> db.get_setting("nonexistent") is None
        True
        """
        with self._reader() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value (upsert).

        >>> db = Database(":memory:")
        >>> db.set_setting("theme", '"dark"')
        >>> db.get_setting("theme")
        '"dark"'
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, now),
            )

    def list_settings(self) -> list[dict]:
        """List all settings.

        >>> db = Database(":memory:")
        >>> db.list_settings()
        []
        """
        with self._reader() as conn:
            cursor = conn.execute("SELECT * FROM settings ORDER BY key ASC")
            return [dict(row) for row in cursor.fetchall()]

    # Alias for api-layer compatibility
    upsert_setting = set_setting

    def delete_setting(self, key: str) -> bool:
        """Delete a setting.

        >>> db = Database(":memory:")
        >>> db.set_setting("tmp", '"val"')
        >>> db.delete_setting("tmp")
        True
        """
        with self._writer() as conn:
            cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0

    # ==================================================================
    # Gatekeeper Decisions
    # ==================================================================

    def record_gatekeeper_decision(
        self,
        decision: str,
        timestamp: Optional[str] = None,
        command: Optional[str] = None,
        method: Optional[str] = None,
        reason: Optional[str] = None,
        elapsed_ms: Optional[float] = None,
        session_id: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> int:
        """Record a gatekeeper decision.

        >>> db = Database(":memory:")
        >>> rid = db.record_gatekeeper_decision("ALLOW", command="ls")
        >>> rid > 0
        True
        """
        ts = timestamp or datetime.utcnow().isoformat()
        # Truncate command to 1000 chars per design doc
        if command and len(command) > 1000:
            command = command[:1000]

        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO gatekeeper_decisions
                   (timestamp, command, decision, method, reason, elapsed_ms, session_id, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, command, decision, method, reason, elapsed_ms, session_id, repo_path),
            )
            return cursor.lastrowid or 0

    def list_gatekeeper_decisions(
        self, limit: int = 100, session_id: Optional[str] = None,
    ) -> list[dict]:
        """List recent gatekeeper decisions, optionally filtered by session.

        >>> db = Database(":memory:")
        >>> db.list_gatekeeper_decisions()
        []
        """
        with self._reader() as conn:
            if session_id:
                cursor = conn.execute(
                    "SELECT * FROM gatekeeper_decisions WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM gatekeeper_decisions ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def list_gatekeeper_sessions(self, limit: int = 50) -> list[dict]:
        """Aggregate gatekeeper decisions grouped by session.

        >>> db = Database(":memory:")
        >>> db.list_gatekeeper_sessions()
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT session_id,
                          COUNT(*) as total,
                          SUM(CASE WHEN decision='ALLOW' THEN 1 ELSE 0 END) as allowed,
                          SUM(CASE WHEN decision='ASK_USER' THEN 1 ELSE 0 END) as asked,
                          MIN(timestamp) as first_seen,
                          MAX(timestamp) as last_seen,
                          repo_path
                   FROM gatekeeper_decisions
                   WHERE session_id IS NOT NULL AND session_id != ''
                   GROUP BY session_id
                   ORDER BY last_seen DESC
                   LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def purge_gatekeeper_decisions(
        self, before_iso: Optional[str] = None, session_id: Optional[str] = None,
    ) -> int:
        """Delete gatekeeper decisions by age or session. Returns rows deleted.

        >>> db = Database(":memory:")
        >>> db.purge_gatekeeper_decisions()
        0
        >>> db.record_gatekeeper_decision("ALLOW", command="echo hi", method="LOCAL", reason="safe", elapsed_ms=1.0, session_id="sess1", repo_path="/repo")
        1
        >>> db.purge_gatekeeper_decisions(session_id="sess1")
        1
        >>> db.list_gatekeeper_decisions()
        []
        """
        with self._writer() as conn:
            if session_id:
                cursor = conn.execute(
                    "DELETE FROM gatekeeper_decisions WHERE session_id = ?",
                    (session_id,),
                )
            elif before_iso:
                cursor = conn.execute(
                    "DELETE FROM gatekeeper_decisions WHERE timestamp < ?",
                    (before_iso,),
                )
            else:
                cursor = conn.execute("DELETE FROM gatekeeper_decisions")
            return cursor.rowcount

    def export_gatekeeper_decisions(
        self,
        session_id: Optional[str] = None,
        decision: Optional[str] = None,
    ) -> list[dict]:
        """Export matching gatekeeper decisions as list of dicts.

        >>> db = Database(":memory:")
        >>> db.export_gatekeeper_decisions()
        []
        """
        with self._reader() as conn:
            sql = "SELECT * FROM gatekeeper_decisions WHERE 1=1"
            params: list = []
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            if decision:
                sql += " AND decision = ?"
                params.append(decision)
            sql += " ORDER BY timestamp DESC"
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    # ==================================================================
    # Command Usage
    # ==================================================================

    def record_command_usage(
        self,
        command_name: str,
        timestamp: Optional[str] = None,
        session_id: Optional[str] = None,
        success: Optional[bool] = None,
        duration_ms: Optional[float] = None,
        repo_path: Optional[str] = None,
    ) -> int:
        """Record a command usage event.

        >>> db = Database(":memory:")
        >>> rid = db.record_command_usage("dc", success=True)
        >>> rid > 0
        True
        """
        ts = timestamp or datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO command_usage
                   (command_name, timestamp, session_id, success, duration_ms, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (command_name, ts, session_id, success, duration_ms, repo_path),
            )
            return cursor.lastrowid or 0

    # ==================================================================
    # Agent Invocations
    # ==================================================================

    def record_agent_invocation(
        self,
        agent_name: str,
        timestamp: Optional[str] = None,
        session_id: Optional[str] = None,
        spawned_by: Optional[str] = None,
        success: Optional[bool] = None,
        duration_ms: Optional[float] = None,
        tasks_completed: int = 0,
        errors: int = 0,
        repo_path: Optional[str] = None,
    ) -> int:
        """Record an agent invocation.

        >>> db = Database(":memory:")
        >>> rid = db.record_agent_invocation("git-pr-workflow-manager")
        >>> rid > 0
        True
        """
        ts = timestamp or datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO agent_invocations
                   (agent_name, timestamp, session_id, spawned_by, success,
                    duration_ms, tasks_completed, errors, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_name, ts, session_id, spawned_by, success,
                 duration_ms, tasks_completed, errors, repo_path),
            )
            return cursor.lastrowid or 0

    def list_agent_invocations(self, limit: int = 100) -> list[dict]:
        """List recent agent invocations.

        >>> db = Database(":memory:")
        >>> db.list_agent_invocations()
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM agent_invocations ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================================================================
    # Hook Executions
    # ==================================================================

    def record_hook_execution(
        self,
        hook_type: str,
        timestamp: Optional[str] = None,
        hook_name: Optional[str] = None,
        session_id: Optional[str] = None,
        success: Optional[bool] = None,
        duration_ms: Optional[float] = None,
        error_msg: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> int:
        """Record a hook execution.

        >>> db = Database(":memory:")
        >>> rid = db.record_hook_execution("PreToolUse", hook_name="security_gatekeeper")
        >>> rid > 0
        True
        """
        ts = timestamp or datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO hook_executions
                   (hook_type, hook_name, timestamp, session_id, success,
                    duration_ms, error_msg, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (hook_type, hook_name, ts, session_id, success,
                 duration_ms, error_msg, repo_path),
            )
            return cursor.lastrowid or 0

    # ==================================================================
    # Lessons
    # ==================================================================

    def record_lesson(
        self,
        content: str,
        project_id: Optional[str] = None,
        failure_count: int = 1,
        status: str = "learning",
        source_session_id: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> int:
        """Record a lesson.

        >>> db = Database(":memory:")
        >>> rid = db.record_lesson("Always use full paths")
        >>> rid > 0
        True
        """
        now = datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO lessons
                   (content, project_id, failure_count, status,
                    source_session_id, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (content, project_id, failure_count, status,
                 source_session_id, tags, now, now),
            )
            return cursor.lastrowid or 0

    def list_lessons(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        """List lessons, optionally filtered by status.

        >>> db = Database(":memory:")
        >>> db.list_lessons()
        []
        """
        with self._reader() as conn:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM lessons WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM lessons ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def update_lesson(self, lesson_id: int, **kwargs: Any) -> bool:
        """Update a lesson.

        >>> db = Database(":memory:")
        >>> lid = db.record_lesson("test")
        >>> db.update_lesson(lid, failure_count=2)
        True
        """
        allowed = {"content", "failure_count", "status", "graduation_date", "tags"}
        invalid = set(kwargs.keys()) - allowed - {"updated_at"}
        if invalid:
            raise ValueError(f"Invalid columns for lesson update: {invalid}")

        kwargs["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [lesson_id]

        with self._writer() as conn:
            cursor = conn.execute(
                f"UPDATE lessons SET {set_clause} WHERE id = ?", values
            )
            return cursor.rowcount > 0

    # ==================================================================
    # Version Checks
    # ==================================================================

    def record_version_check(
        self,
        current_version: str,
        latest_version: str,
        outdated: Optional[bool] = None,
        cache_hit: Optional[bool] = None,
    ) -> int:
        """Record a version check.

        >>> db = Database(":memory:")
        >>> rid = db.record_version_check("0.3.11", "0.4.0", outdated=True)
        >>> rid > 0
        True
        """
        ts = datetime.utcnow().isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO version_checks
                   (timestamp, current_version, latest_version, outdated, cache_hit)
                   VALUES (?, ?, ?, ?, ?)""",
                (ts, current_version, latest_version, outdated, cache_hit),
            )
            return cursor.lastrowid or 0

    # ==================================================================
    # Analytics Query Methods (for API routes)
    # ==================================================================

    def query_gatekeeper_decisions(self, days: int = 30) -> dict:
        """Aggregate gatekeeper decision stats for the last N days.

        Returns dict with total, by_decision counts, by_method counts,
        avg_elapsed_ms, and recent decisions.

        >>> db = Database(":memory:")
        >>> stats = db.query_gatekeeper_decisions()
        >>> stats["total"]
        0
        """
        with self._reader() as conn:
            cutoff = f"datetime('now', '-{days} days')"

            cursor = conn.execute(
                f"SELECT COUNT(*) as total FROM gatekeeper_decisions WHERE timestamp >= {cutoff}"
            )
            total = cursor.fetchone()["total"]

            cursor = conn.execute(
                f"""SELECT decision, COUNT(*) as count
                    FROM gatekeeper_decisions WHERE timestamp >= {cutoff}
                    GROUP BY decision"""
            )
            by_decision = {row["decision"]: row["count"] for row in cursor.fetchall()}

            cursor = conn.execute(
                f"""SELECT method, COUNT(*) as count
                    FROM gatekeeper_decisions WHERE timestamp >= {cutoff}
                    GROUP BY method"""
            )
            by_method = {row["method"]: row["count"] for row in cursor.fetchall()}

            cursor = conn.execute(
                f"SELECT AVG(elapsed_ms) as avg_ms FROM gatekeeper_decisions WHERE timestamp >= {cutoff}"
            )
            avg_ms = cursor.fetchone()["avg_ms"]

            cursor = conn.execute(
                f"""SELECT * FROM gatekeeper_decisions
                    WHERE timestamp >= {cutoff}
                    ORDER BY timestamp DESC LIMIT 50"""
            )
            recent = [dict(row) for row in cursor.fetchall()]

            return {
                "total": total,
                "by_decision": by_decision,
                "by_method": by_method,
                "avg_elapsed_ms": round(avg_ms, 2) if avg_ms else None,
                "recent": recent,
            }

    def query_command_usage(self, days: int = 30) -> dict:
        """Aggregate command usage stats for the last N days.

        >>> db = Database(":memory:")
        >>> stats = db.query_command_usage()
        >>> stats["total"]
        0
        """
        with self._reader() as conn:
            cutoff = f"datetime('now', '-{days} days')"

            cursor = conn.execute(
                f"SELECT COUNT(*) as total FROM command_usage WHERE timestamp >= {cutoff}"
            )
            total = cursor.fetchone()["total"]

            cursor = conn.execute(
                f"""SELECT command_name, COUNT(*) as count,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                           AVG(duration_ms) as avg_ms
                    FROM command_usage WHERE timestamp >= {cutoff}
                    GROUP BY command_name ORDER BY count DESC"""
            )
            by_command = [
                {
                    "command": row["command_name"],
                    "count": row["count"],
                    "success_rate": round(row["successes"] / row["count"] * 100, 1) if row["count"] else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2) if row["avg_ms"] else None,
                }
                for row in cursor.fetchall()
            ]

            return {"total": total, "by_command": by_command}

    def query_agent_invocations(self, days: int = 30) -> dict:
        """Aggregate agent invocation stats for the last N days.

        >>> db = Database(":memory:")
        >>> stats = db.query_agent_invocations()
        >>> stats["total"]
        0
        """
        with self._reader() as conn:
            cutoff = f"datetime('now', '-{days} days')"

            cursor = conn.execute(
                f"SELECT COUNT(*) as total FROM agent_invocations WHERE timestamp >= {cutoff}"
            )
            total = cursor.fetchone()["total"]

            cursor = conn.execute(
                f"""SELECT agent_name, COUNT(*) as count,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                           AVG(duration_ms) as avg_ms,
                           SUM(tasks_completed) as total_tasks,
                           SUM(errors) as total_errors
                    FROM agent_invocations WHERE timestamp >= {cutoff}
                    GROUP BY agent_name ORDER BY count DESC"""
            )
            by_agent = [
                {
                    "agent": row["agent_name"],
                    "count": row["count"],
                    "success_rate": round(row["successes"] / row["count"] * 100, 1) if row["count"] else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2) if row["avg_ms"] else None,
                    "total_tasks": row["total_tasks"] or 0,
                    "total_errors": row["total_errors"] or 0,
                }
                for row in cursor.fetchall()
            ]

            return {"total": total, "by_agent": by_agent}

    def query_hook_executions(self, days: int = 30) -> dict:
        """Aggregate hook execution stats for the last N days.

        >>> db = Database(":memory:")
        >>> stats = db.query_hook_executions()
        >>> stats["total"]
        0
        """
        with self._reader() as conn:
            cutoff = f"datetime('now', '-{days} days')"

            cursor = conn.execute(
                f"SELECT COUNT(*) as total FROM hook_executions WHERE timestamp >= {cutoff}"
            )
            total = cursor.fetchone()["total"]

            cursor = conn.execute(
                f"""SELECT hook_name, hook_type, COUNT(*) as count,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                           AVG(duration_ms) as avg_ms
                    FROM hook_executions WHERE timestamp >= {cutoff}
                    GROUP BY hook_name, hook_type ORDER BY count DESC"""
            )
            by_hook = [
                {
                    "hook_name": row["hook_name"],
                    "hook_type": row["hook_type"],
                    "count": row["count"],
                    "success_rate": round(row["successes"] / row["count"] * 100, 1) if row["count"] else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2) if row["avg_ms"] else None,
                }
                for row in cursor.fetchall()
            ]

            return {"total": total, "by_hook": by_hook}

    def query_lessons(self) -> dict:
        """Aggregate lesson stats.

        >>> db = Database(":memory:")
        >>> stats = db.query_lessons()
        >>> stats["total"]
        0
        """
        with self._reader() as conn:
            cursor = conn.execute("SELECT COUNT(*) as total FROM lessons")
            total = cursor.fetchone()["total"]

            cursor = conn.execute(
                """SELECT status, COUNT(*) as count FROM lessons GROUP BY status"""
            )
            by_status = {row["status"]: row["count"] for row in cursor.fetchall()}

            cursor = conn.execute(
                "SELECT * FROM lessons ORDER BY updated_at DESC LIMIT 50"
            )
            recent = [dict(row) for row in cursor.fetchall()]

            return {"total": total, "by_status": by_status, "recent": recent}

    def get_project_activity_summary(self, limit: int = 20) -> list[dict]:
        """Aggregate all activity tables grouped by repo_path.

        Returns a list of dicts with per-project stats, ordered by most recent
        activity. Only includes repos with at least one recorded event.

        >>> db = Database(":memory:")
        >>> db.record_gatekeeper_decision("ALLOW", command="ls", repo_path="/repo/a", session_id="s1")
        1
        >>> db.record_gatekeeper_decision("ALLOW", command="cat", repo_path="/repo/a", session_id="s1")
        2
        >>> db.record_gatekeeper_decision("ASK_USER", command="rm -rf /", repo_path="/repo/a", session_id="s2")
        3
        >>> summary = db.get_project_activity_summary()
        >>> len(summary)
        1
        >>> summary[0]["repo_path"]
        '/repo/a'
        >>> summary[0]["gatekeeper_decisions"]
        3
        >>> summary[0]["gatekeeper_allowed"]
        2
        >>> summary[0]["unique_sessions"]
        2
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """
                SELECT
                    repo_path,
                    SUM(gk_total) as gatekeeper_decisions,
                    SUM(gk_allowed) as gatekeeper_allowed,
                    SUM(cmd_total) as commands_run,
                    SUM(hook_total) as hook_executions,
                    MAX(last_ts) as last_activity,
                    MIN(first_ts) as first_seen,
                    unique_sessions
                FROM (
                    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
                           COUNT(*) as gk_total,
                           SUM(CASE WHEN decision='ALLOW' THEN 1 ELSE 0 END) as gk_allowed,
                           0 as cmd_total, 0 as hook_total,
                           MAX(timestamp) as last_ts,
                           MIN(timestamp) as first_ts,
                           COUNT(DISTINCT session_id) as unique_sessions
                    FROM gatekeeper_decisions
                    WHERE repo_path IS NOT NULL AND repo_path != ''
                    GROUP BY REPLACE(repo_path, char(92), '/')

                    UNION ALL

                    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
                           0 as gk_total, 0 as gk_allowed,
                           COUNT(*) as cmd_total, 0 as hook_total,
                           MAX(timestamp) as last_ts,
                           MIN(timestamp) as first_ts,
                           COUNT(DISTINCT session_id) as unique_sessions
                    FROM command_usage
                    WHERE repo_path IS NOT NULL AND repo_path != ''
                    GROUP BY REPLACE(repo_path, char(92), '/')

                    UNION ALL

                    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
                           0 as gk_total, 0 as gk_allowed,
                           0 as cmd_total, COUNT(*) as hook_total,
                           MAX(timestamp) as last_ts,
                           MIN(timestamp) as first_ts,
                           COUNT(DISTINCT session_id) as unique_sessions
                    FROM hook_executions
                    WHERE repo_path IS NOT NULL AND repo_path != ''
                    GROUP BY REPLACE(repo_path, char(92), '/')
                )
                GROUP BY repo_path
                ORDER BY last_activity DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_command_usage(
        self, limit: int = 200, command_name: Optional[str] = None,
    ) -> list[dict]:
        """List recent command usage logs, optionally filtered by command name.

        >>> db = Database(":memory:")
        >>> db.list_command_usage()
        []
        >>> db.list_command_usage(command_name="search")
        []
        """
        with self._reader() as conn:
            if command_name:
                cursor = conn.execute(
                    "SELECT * FROM command_usage WHERE command_name = ? ORDER BY timestamp DESC LIMIT ?",
                    (command_name, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM command_usage ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def list_hook_executions(
        self, limit: int = 200, hook_name: Optional[str] = None,
    ) -> list[dict]:
        """List recent hook execution logs, optionally filtered by hook name.

        >>> db = Database(":memory:")
        >>> db.list_hook_executions()
        []
        >>> db.list_hook_executions(hook_name="session_indexing")
        []
        """
        with self._reader() as conn:
            if hook_name:
                cursor = conn.execute(
                    "SELECT * FROM hook_executions WHERE hook_name = ? ORDER BY timestamp DESC LIMIT ?",
                    (hook_name, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM hook_executions ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def list_version_checks(self, limit: int = 100) -> list[dict]:
        """List recent version check logs.

        >>> db = Database(":memory:")
        >>> db.list_version_checks()
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT * FROM version_checks ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
