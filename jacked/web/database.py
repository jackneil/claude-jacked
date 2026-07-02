"""SQLite database layer for jacked web dashboard.

10 tables across three concerns:
- Account management: accounts, installations, settings
- Analytics: command_usage, agent_invocations,
             hook_executions, lessons, version_checks
- Session tracking: session_accounts

WAL mode for concurrent reads, single writer lock for atomic writes.
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, computed_field

logger = logging.getLogger(__name__)

# Sessions without a heartbeat within this window are considered stale
SESSION_STALENESS_MINUTES = 60

# Sessions with no Claude process alive for this long are auto-closed
DEAD_SESSION_HOURS = 4


# ---------------------------------------------------------------------------
# Pydantic v2 Models
# ---------------------------------------------------------------------------


class Account(BaseModel):
    """Pydantic v2 model for an account row."""

    id: int
    provider: str = "claude"
    email: str
    organization_uuid: str = ""
    organization_name: Optional[str] = None
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
    cc_access_token: Optional[str] = None
    cc_refresh_token: Optional[str] = None
    cc_expires_at: Optional[int] = None
    refresh_last_failed_at: Optional[int] = None
    refresh_failure_type: Optional[str] = None
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
    provider TEXT NOT NULL DEFAULT 'claude',  -- 'claude' | 'codex' — the account's CLI provider
    email TEXT NOT NULL,
    organization_uuid TEXT DEFAULT '',  -- Sentinel: "" = personal/legacy, non-empty = real org UUID
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
    UNIQUE(provider, email, organization_uuid)
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
-- (gatekeeper_decisions was removed in 0.70.0; old DBs may still carry it)
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

CREATE TABLE IF NOT EXISTS session_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    account_id INTEGER,
    email TEXT,
    detected_at TEXT NOT NULL,
    ended_at TEXT,
    last_activity_at TEXT,
    detection_method TEXT,
    repo_path TEXT,
    is_subagent BOOLEAN DEFAULT 0,
    parent_session_id TEXT,
    agent_type TEXT,
    pid INTEGER,
    UNIQUE(session_id, detected_at)
);

CREATE TABLE IF NOT EXISTS swap_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    from_account_id INTEGER,
    to_account_id INTEGER,
    reason TEXT,
    trigger TEXT,
    from_5h_usage REAL,
    from_7d_usage REAL,
    to_5h_usage REAL,
    to_7d_usage REAL,
    status TEXT NOT NULL DEFAULT 'committed',
    residency_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    account_id INTEGER,
    action TEXT NOT NULL,
    trigger TEXT,
    target_id INTEGER,
    reason TEXT,
    detail TEXT
);

"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active, is_deleted);
CREATE INDEX IF NOT EXISTS idx_accounts_priority ON accounts(priority);
CREATE INDEX IF NOT EXISTS idx_installations_repo ON installations(repo_path);
CREATE INDEX IF NOT EXISTS idx_command_usage_name ON command_usage(command_name);
CREATE INDEX IF NOT EXISTS idx_command_usage_ts ON command_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_invocations_name ON agent_invocations(agent_name);
CREATE INDEX IF NOT EXISTS idx_hook_executions_type ON hook_executions(hook_type);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE INDEX IF NOT EXISTS idx_version_checks_ts ON version_checks(timestamp);
CREATE INDEX IF NOT EXISTS idx_command_usage_repo ON command_usage(repo_path);
CREATE INDEX IF NOT EXISTS idx_hook_executions_repo ON hook_executions(repo_path);
CREATE INDEX IF NOT EXISTS idx_sa_session ON session_accounts(session_id);
CREATE INDEX IF NOT EXISTS idx_sa_account ON session_accounts(account_id);
CREATE INDEX IF NOT EXISTS idx_sa_active ON session_accounts(ended_at, last_activity_at, detected_at);
CREATE INDEX IF NOT EXISTS idx_swap_log_ts ON swap_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_log_timestamp ON decision_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_action ON decision_log(action);
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
            # Pre-schema crash recovery: if accounts_new exists but accounts doesn't,
            # a prior migration crashed between DROP TABLE and ALTER TABLE RENAME.
            # Rename before SCHEMA_SQL runs (which would CREATE TABLE accounts empty).
            _has_new = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts_new'"
            ).fetchone() is not None
            _has_old = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone() is not None
            if _has_new and not _has_old:
                conn.execute("ALTER TABLE accounts_new RENAME TO accounts")
                conn.execute("DROP INDEX IF EXISTS idx_accounts_email")
            conn.executescript(SCHEMA_SQL)
            # Migrations run BEFORE indexes (indexes may reference new columns)
            # Migration: add cached_usage_raw if missing (existing DBs)
            cursor = conn.execute("PRAGMA table_info(accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            if "cached_usage_raw" not in cols:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN cached_usage_raw TEXT"
                    )
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
            # Migration: add last_activity_at to session_accounts
            cursor = conn.execute("PRAGMA table_info(session_accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            if "last_activity_at" not in cols:
                try:
                    conn.execute(
                        "ALTER TABLE session_accounts ADD COLUMN last_activity_at TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
            # Migration: add subagent tracking columns to session_accounts
            cursor = conn.execute("PRAGMA table_info(session_accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            for col_name, col_def in [
                ("is_subagent", "BOOLEAN DEFAULT 0"),
                ("parent_session_id", "TEXT"),
                ("agent_type", "TEXT"),
                ("pid", "INTEGER"),
            ]:
                if col_name not in cols:
                    try:
                        conn.execute(
                            f"ALTER TABLE session_accounts ADD COLUMN {col_name} {col_def}"
                        )
                    except sqlite3.OperationalError:
                        pass
            # Migration: add CC (Claude Code) token columns for dual-token architecture.
            # Primary tokens are jacked-only; CC tokens are written to credential files
            # for Claude Code. This prevents token rotation conflicts.
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_cols = {row[1] for row in cursor.fetchall()}
            cc_cols_added = False
            for col_name, col_def in [
                ("cc_access_token", "TEXT"),
                ("cc_refresh_token", "TEXT"),
                ("cc_expires_at", "INTEGER"),
            ]:
                if col_name not in acct_cols:
                    cc_cols_added = True
                    try:
                        conn.execute(
                            f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"
                        )
                    except sqlite3.OperationalError:
                        pass
            # Seed cc_access_token from primary for existing accounts (one-time only).
            # Only runs when columns were JUST added — not on every startup.
            # Re-running would defeat invalid_grant handling (which clears cc_refresh_token).
            # cc_refresh_token is intentionally NOT seeded — seeding it would recreate
            # the shared-refresh-token bug. Users must do CC OAuth to get independent
            # CC refresh tokens.
            if cc_cols_added:
                conn.execute(
                    """UPDATE accounts SET cc_access_token = access_token,
                    cc_expires_at = expires_at
                    WHERE cc_access_token IS NULL AND access_token IS NOT NULL
                    AND is_deleted = 0
                    AND (expires_at IS NULL OR expires_at > strftime('%s','now'))"""
                )
            # Migration: add organization columns + composite uniqueness.
            # Requires table recreation because SQLite can't ALTER UNIQUE constraints.
            # Uses individual conn.execute() (NOT executescript) for transactional safety.
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_cols_org = {row[1] for row in cursor.fetchall()}
            if "organization_uuid" not in acct_cols_org:
                # Note: scenario where accounts was dropped but accounts_new wasn't
                # yet renamed is handled by pre-schema recovery above (line ~430).
                # Here we only need to handle partial accounts_new from early-stage crash.
                conn.execute("DROP TABLE IF EXISTS accounts_new")
                conn.execute("""CREATE TABLE accounts_new (
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
                )""")
                conn.execute("""INSERT INTO accounts_new (
                    id, email, organization_uuid, organization_name,
                    display_name, access_token, refresh_token, expires_at,
                    scopes, subscription_type, rate_limit_tier, has_extra_usage,
                    priority, is_active, is_deleted, last_used_at,
                    cached_usage_5h, cached_usage_7d, cached_5h_resets_at, cached_7d_resets_at,
                    usage_cached_at, cached_usage_raw, last_error, last_error_at,
                    consecutive_failures, last_validated_at, validation_status,
                    cc_access_token, cc_refresh_token, cc_expires_at,
                    created_at, updated_at
                ) SELECT
                    id, email, '', NULL,
                    display_name, access_token, refresh_token, expires_at,
                    scopes, subscription_type, rate_limit_tier, has_extra_usage,
                    priority, is_active, is_deleted, last_used_at,
                    cached_usage_5h, cached_usage_7d, cached_5h_resets_at, cached_7d_resets_at,
                    usage_cached_at, cached_usage_raw, last_error, last_error_at,
                    consecutive_failures, last_validated_at, validation_status,
                    cc_access_token, cc_refresh_token, cc_expires_at,
                    created_at, updated_at
                FROM accounts""")
                conn.execute("DROP TABLE accounts")
                conn.execute("ALTER TABLE accounts_new RENAME TO accounts")
                conn.execute("DROP INDEX IF EXISTS idx_accounts_email")
            # Migration: add auto_swap_enabled to accounts
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_cols_swap = {row[1] for row in cursor.fetchall()}
            if "auto_swap_enabled" not in acct_cols_swap:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN auto_swap_enabled INTEGER NOT NULL DEFAULT 1"
                    )
                except sqlite3.OperationalError:
                    pass
            # Migration: add circuit breaker columns to accounts
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_cols_cb = {row[1] for row in cursor.fetchall()}
            if "refresh_last_failed_at" not in acct_cols_cb:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN refresh_last_failed_at INTEGER"
                    )
                except sqlite3.OperationalError:
                    pass
            if "refresh_failure_type" not in acct_cols_cb:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN refresh_failure_type TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
            # Migration: add `provider` column + provider-scoped uniqueness.
            # Codex support: the same (email, organization_uuid) can exist under
            # both 'claude' and 'codex'. SQLite can't ALTER a UNIQUE constraint,
            # so rebuild the table. This runs AFTER every other accounts
            # column-add above, so the rebuilt table preserves all columns. The
            # crash window (DROP accounts before RENAME accounts_new) is covered
            # by the pre-schema recovery near the top of _init_schema. Placed
            # BEFORE INDEXES_SQL so idx_accounts_active/idx_accounts_priority are
            # recreated on the rebuilt table.
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_info_prov = cursor.fetchall()
            acct_cols_prov = {row[1] for row in acct_info_prov}
            if "provider" not in acct_cols_prov:
                conn.execute("DROP TABLE IF EXISTS accounts_new")
                # Reconstruct the live accounts schema (so this never drifts as
                # columns are added by earlier migrations), injecting `provider`
                # and the new composite UNIQUE.
                col_defs = []
                for cid, name, ctype, notnull, dflt, pk in acct_info_prov:
                    if pk:
                        col_defs.append(
                            f'"{name}" {ctype or "INTEGER"} PRIMARY KEY AUTOINCREMENT'
                        )
                        continue
                    piece = f'"{name}" {ctype or "TEXT"}'
                    if notnull:
                        piece += " NOT NULL"
                    if dflt is not None:
                        piece += f" DEFAULT {dflt}"
                    col_defs.append(piece)
                col_defs.append("provider TEXT NOT NULL DEFAULT 'claude'")
                conn.execute(
                    "CREATE TABLE accounts_new (\n"
                    + ",\n".join(col_defs)
                    + ",\nUNIQUE(provider, email, organization_uuid)\n)"
                )
                old_cols = ", ".join(f'"{r[1]}"' for r in acct_info_prov)
                conn.execute(
                    f"INSERT INTO accounts_new ({old_cols}) "
                    f"SELECT {old_cols} FROM accounts"
                )
                conn.execute("DROP TABLE accounts")
                conn.execute("ALTER TABLE accounts_new RENAME TO accounts")
            # Migration: add swap outcome tracking to swap_log.
            # status distinguishes committed swaps from pending/failed attempts;
            # residency_seconds is how long the outgoing account was active.
            cursor = conn.execute("PRAGMA table_info(swap_log)")
            swap_cols = {row[1] for row in cursor.fetchall()}
            for col_name, col_def in [
                ("status", "TEXT NOT NULL DEFAULT 'committed'"),
                ("residency_seconds", "INTEGER"),
            ]:
                if col_name not in swap_cols:
                    try:
                        conn.execute(
                            f"ALTER TABLE swap_log ADD COLUMN {col_name} {col_def}"
                        )
                    except sqlite3.OperationalError:
                        pass
            # Indexes (after migrations so new columns exist)
            conn.executescript(INDEXES_SQL)
            # Migration: rebuild idx_sa_active to cover last_activity_at
            try:
                conn.execute("DROP INDEX IF EXISTS idx_sa_active")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sa_active "
                    "ON session_accounts(ended_at, last_activity_at, detected_at)"
                )
            except sqlite3.OperationalError:
                pass
            # Migration: drop known_refresh_tokens (no longer needed)
            try:
                conn.execute("DROP TABLE IF EXISTS known_refresh_tokens")
            except sqlite3.OperationalError:
                pass
            # Cleanup: end duplicate open session-account records.
            # Keeps only the newest open row per (session_id, account_id).
            try:
                conn.execute(
                    """UPDATE session_accounts SET ended_at = datetime('now')
                       WHERE ended_at IS NULL
                         AND id NOT IN (
                             SELECT MAX(id) FROM session_accounts
                             WHERE ended_at IS NULL
                             GROUP BY session_id, COALESCE(account_id, '')
                         )"""
                )
            except sqlite3.OperationalError:
                pass
            # Audit trail: catch ALL display_name changes regardless of code path.
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS display_name_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    changed_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TRIGGER IF NOT EXISTS trg_display_name_audit
                AFTER UPDATE OF display_name ON accounts
                WHEN OLD.display_name IS NOT NEW.display_name
                BEGIN
                    INSERT INTO display_name_audit (account_id, old_value, new_value)
                    VALUES (OLD.id, OLD.display_name, NEW.display_name);
                END;
                CREATE TRIGGER IF NOT EXISTS trg_display_name_audit_insert
                AFTER INSERT ON accounts
                WHEN NEW.display_name IS NOT NULL
                BEGIN
                    INSERT INTO display_name_audit (account_id, old_value, new_value)
                    VALUES (NEW.id, NULL, NEW.display_name);
                END;
            """)
            logger.info(
                "display_name protected: only set_account_label() can modify labels"
            )

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
        organization_uuid: str = "",  # Sentinel: "" = personal/legacy, non-empty = real org UUID
        organization_name: Optional[str] = None,
        provider: str = "claude",  # 'claude' | 'codex' — the account's CLI provider
    ) -> dict:
        """Create a new account or update if (email, organization_uuid) already exists.

        organization_uuid uses "" (empty string) as sentinel for personal/legacy accounts
        instead of NULL, because SQLite treats NULL != NULL in UNIQUE constraints.
        This makes UNIQUE(email, organization_uuid) and ON CONFLICT work atomically.

        Handles edge cases:
        - Existing deleted account with same email+org: undelete and update
        - Existing active account with same email+org: update tokens in place
        - Same email, different org: creates a new separate account

        >>> db = Database(":memory:")
        >>> acct = db.create_account("test@example.com", "sk-ant-test", 9999999999)
        >>> acct["email"]
        'test@example.com'
        >>> acct["organization_uuid"]
        ''
        """
        # Normalize None → sentinel to prevent SQLite NULL uniqueness issues
        if organization_uuid is None:
            organization_uuid = ""
        now = datetime.now(timezone.utc).isoformat()

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
                    provider, email, organization_uuid, organization_name,
                    access_token, refresh_token, expires_at, display_name,
                    scopes, subscription_type, rate_limit_tier, has_extra_usage,
                    priority, is_active, is_deleted, consecutive_failures,
                    validation_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 'unknown', ?, ?)
                ON CONFLICT(provider, email, organization_uuid) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scopes = excluded.scopes,
                    organization_name = COALESCE(excluded.organization_name, organization_name),
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
                    provider,
                    email,
                    organization_uuid,
                    organization_name,
                    access_token,
                    refresh_token,
                    expires_at,
                    display_name,
                    scopes,
                    subscription_type,
                    rate_limit_tier,
                    has_extra_usage,
                    priority,
                    now,
                    now,
                ),
            )

            # Use compound key for retrieval — safe since
            # UNIQUE(provider, email, organization_uuid)
            cursor = conn.execute(
                "SELECT * FROM accounts WHERE provider = ? AND email = ? "
                "AND organization_uuid = ?",
                (provider, email, organization_uuid),
            )
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

    def get_account_by_email(
        self,
        email: str,
        organization_uuid: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Optional[dict]:
        """Get an account by email (+ optional org/provider), case-insensitive.

        When organization_uuid is provided, returns exact match.
        When omitted, returns the single match or raises ValueError
        if multiple accounts share the same email. Pass ``provider`` to scope
        the lookup to one CLI provider — required to disambiguate a same-email
        account that exists under both 'claude' and 'codex'.

        >>> db = Database(":memory:")
        >>> db.get_account_by_email("nobody@nowhere.com") is None
        True
        """
        clauses = ["LOWER(email) = LOWER(?)", "is_deleted = 0"]
        params: list[Any] = [email]
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        with self._reader() as conn:
            if organization_uuid is not None:
                cursor = conn.execute(
                    f"SELECT * FROM accounts WHERE {' AND '.join(clauses)} "
                    "AND organization_uuid = ?",
                    (*params, organization_uuid),
                )
                row = cursor.fetchone()
                return dict(row) if row else None

            cursor = conn.execute(
                f"SELECT * FROM accounts WHERE {' AND '.join(clauses)} "
                "ORDER BY priority ASC, id ASC",
                tuple(params),
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                raise ValueError(
                    f"Ambiguous: {len(rows)} accounts for {email} — "
                    "specify by ID, org, or provider"
                )
            return dict(rows[0]) if rows else None

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
    _ACCOUNT_UPDATE_COLS = frozenset(
        {
            "organization_name",
            "access_token",
            "refresh_token",
            "expires_at",
            "scopes",
            "subscription_type",
            "rate_limit_tier",
            "has_extra_usage",
            "is_active",
            "last_used_at",
            "priority",
            "cached_usage_5h",
            "cached_usage_7d",
            "cached_5h_resets_at",
            "cached_7d_resets_at",
            "usage_cached_at",
            "cached_usage_raw",
            "last_error",
            "last_error_at",
            "consecutive_failures",
            "last_validated_at",
            "validation_status",
            "cc_access_token",
            "cc_refresh_token",
            "cc_expires_at",
            "organization_uuid",
            "refresh_last_failed_at",
            "refresh_failure_type",
        }
    )

    def update_account(self, account_id: int, **kwargs: Any) -> bool:
        """Update an account by ID.

        display_name is NOT in the whitelist — use set_account_label() instead.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@test.com", "tok", 9999999999)
        >>> db.update_account(acct["id"], is_active=False)
        True
        """
        if not kwargs:
            return False

        invalid_cols = set(kwargs.keys()) - self._ACCOUNT_UPDATE_COLS - {"updated_at"}
        if invalid_cols:
            raise ValueError(f"Invalid columns for account update: {invalid_cols}")

        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [account_id]

        with self._writer() as conn:
            cursor = conn.execute(
                f"UPDATE accounts SET {set_clause} WHERE id = ? AND is_deleted = 0",
                values,
            )
            return cursor.rowcount > 0

    def set_account_label(self, account_id: int, label: Optional[str]) -> bool:
        """Set display_name for an account — the ONLY way to change labels.

        Logs old→new for audit trail. The SQLite trigger on display_name
        also writes to display_name_audit for belt-and-suspenders tracking.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@test.com", "tok", 9999999999)
        >>> db.set_account_label(acct["id"], "Work")
        True
        >>> db.get_account(acct["id"])["display_name"]
        'Work'
        """
        with self._writer() as conn:
            old_row = conn.execute(
                "SELECT display_name FROM accounts WHERE id = ? AND is_deleted = 0",
                (account_id,),
            ).fetchone()
            if old_row is None:
                return False
            old = old_row[0]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE accounts SET display_name = ?, updated_at = ? "
                "WHERE id = ? AND is_deleted = 0",
                (label, now, account_id),
            )
            logger.info(
                "Label changed for account %d: %r -> %r", account_id, old, label,
            )
            return True

    def get_label_audit_log(self, limit: int = 50) -> list[dict]:
        """Return recent display_name audit entries.

        >>> db = Database(":memory:")
        >>> isinstance(db.get_label_audit_log(), list)
        True
        """
        with self._reader() as conn:
            cursor = conn.execute(
                "SELECT a.id, a.account_id, a.old_value, a.new_value, "
                "a.changed_at, acct.email "
                "FROM display_name_audit a "
                "LEFT JOIN accounts acct ON acct.id = a.account_id "
                "ORDER BY a.id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_account(self, account_id: int) -> bool:
        """Soft-delete an account.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("del@test.com", "tok", 9999999999)
        >>> db.delete_account(acct["id"])
        True
        >>> db.get_account(acct["id"]) is None
        True
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET is_deleted = 1, updated_at = ? WHERE id = ? AND is_deleted = 0",
                (now, account_id),
            )
            return cursor.rowcount > 0

    def hard_delete_duplicate(self, email: str, organization_uuid: str) -> bool:
        """Hard-delete a soft-deleted account that would collide on UNIQUE(email, org_uuid).

        Used during re-auth when org_uuid changes: a prior buggy re-auth may have
        created a duplicate row that was later soft-deleted. The UNIQUE constraint
        includes soft-deleted rows, so we must remove the ghost before updating.
        Only deletes rows where is_deleted=1 — never touches active accounts.

        >>> db = Database(":memory:")
        >>> db.hard_delete_duplicate("x@test.com", "")
        False
        """
        with self._writer() as conn:
            cursor = conn.execute(
                "DELETE FROM accounts WHERE email = ? AND organization_uuid = ? AND is_deleted = 1",
                (email, organization_uuid),
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
        now = datetime.now(timezone.utc).isoformat()
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

    def get_fallback_account(
        self, exclude_ids: Optional[list[int]] = None
    ) -> Optional[dict]:
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
        ok = self.update_account(account_id, **updates)
        if ok:
            # Wake same-process watchers (the menu-bar pill) immediately
            # instead of leaving them to their slow poll heartbeat.
            from jacked import usage_events

            usage_events.bump()
        return ok

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
        now = datetime.now(timezone.utc).isoformat()
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
        """Clear error state for an account and mark as valid.

        Called after a successful API response, so the token is known-good.

        >>> db = Database(":memory:")
        >>> acct = db.create_account("u@t.com", "tok", 9999999999)
        >>> db.clear_account_errors(acct["id"])
        True
        >>> db.get_account(acct["id"])["validation_status"]
        'valid'
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE accounts SET
                    validation_status = 'valid',
                    last_error = NULL, last_error_at = NULL,
                    consecutive_failures = 0, last_used_at = ?,
                    last_validated_at = ?,
                    updated_at = ?
                   WHERE id = ?""",
                (now, int(time.time()), now, account_id),
            )
            return cursor.rowcount > 0

    def list_stuck_checking_accounts(self, threshold_seconds: int) -> list[dict]:
        """Return non-deleted accounts where validation_status='checking'
        AND (updated_at is NULL OR updated_at older than threshold_seconds).

        NULL updated_at is treated as "definitely stuck" — otherwise
        strftime('%s', NULL) returns NULL and the row is hidden forever.

        Includes inactive accounts (they can still be stuck and need cleanup).

        >>> db = Database(":memory:")
        >>> db.list_stuck_checking_accounts(120)
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT * FROM accounts
                   WHERE validation_status = 'checking'
                     AND is_deleted = 0
                     AND (
                       updated_at IS NULL
                       OR (strftime('%s','now') - strftime('%s', updated_at)) > ?
                     )
                   ORDER BY updated_at ASC""",
                (threshold_seconds,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def reset_stuck_checking(
        self,
        account_id: int,
        threshold_seconds: int,
        reason: str,
    ) -> int:
        """Atomically reset validation_status='checking' to 'unknown' IFF
        the row still reads 'checking' AND (updated_at is NULL OR stale
        past threshold_seconds).

        WHERE guard prevents clobbering a row that a concurrent validator
        already moved to 'valid' (PM1 TOCTOU fix).

        Returns rowcount (0 if already moved).

        >>> db = Database(":memory:")
        >>> db.reset_stuck_checking(1, 120, "x")
        0
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE accounts
                   SET validation_status = 'unknown',
                       last_error = ?,
                       last_error_at = ?,
                       updated_at = ?
                   WHERE id = ?
                     AND validation_status = 'checking'
                     AND is_deleted = 0
                     AND (
                       updated_at IS NULL
                       OR (strftime('%s','now') - strftime('%s', updated_at)) > ?
                     )""",
                (reason, now_iso, now_iso, account_id, threshold_seconds),
            )
            return cursor.rowcount

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
        now = datetime.now(timezone.utc).isoformat()
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
                    repo_path,
                    repo_name,
                    jacked_version,
                    hooks_installed,
                    rules_installed,
                    agents_installed,
                    commands_installed,
                    guardrails_installed,
                    now,
                    now,
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
            cursor = conn.execute("SELECT * FROM installations ORDER BY repo_name ASC")
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
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, now),
            )

    @staticmethod
    def active_account_setting_key(provider: str = "claude") -> str:
        """Settings key holding the active account id for ``provider``.

        Claude keeps the legacy bare ``active_account_id`` key for back-compat
        (no setting migration needed); other providers are namespaced. A user
        can be active on Claude AND Codex at the same time, so active state is
        tracked independently per provider.

        >>> Database.active_account_setting_key("claude")
        'active_account_id'
        >>> Database.active_account_setting_key("codex")
        'active_account_id_codex'
        """
        return (
            "active_account_id"
            if provider == "claude"
            else f"active_account_id_{provider}"
        )

    def get_active_account_id(self, provider: str = "claude") -> Optional[int]:
        """Return the active account id for ``provider`` (None if unset/invalid)."""
        raw = self.get_setting(self.active_account_setting_key(provider))
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def set_active_account_id(self, account_id: int, provider: str = "claude") -> None:
        """Set the active account id for ``provider``."""
        self.set_setting(
            self.active_account_setting_key(provider), str(int(account_id))
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
        ts = timestamp or datetime.now(timezone.utc).isoformat()
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
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO agent_invocations
                   (agent_name, timestamp, session_id, spawned_by, success,
                    duration_ms, tasks_completed, errors, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_name,
                    ts,
                    session_id,
                    spawned_by,
                    success,
                    duration_ms,
                    tasks_completed,
                    errors,
                    repo_path,
                ),
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
        >>> rid = db.record_hook_execution("PreToolUse", hook_name="qa_suggest")
        >>> rid > 0
        True
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO hook_executions
                   (hook_type, hook_name, timestamp, session_id, success,
                    duration_ms, error_msg, repo_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    hook_type,
                    hook_name,
                    ts,
                    session_id,
                    success,
                    duration_ms,
                    error_msg,
                    repo_path,
                ),
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
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO lessons
                   (content, project_id, failure_count, status,
                    source_session_id, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content,
                    project_id,
                    failure_count,
                    status,
                    source_session_id,
                    tags,
                    now,
                    now,
                ),
            )
            return cursor.lastrowid or 0

    def list_lessons(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
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

        kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        ts = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO version_checks
                   (timestamp, current_version, latest_version, outdated, cache_hit)
                   VALUES (?, ?, ?, ?, ?)""",
                (ts, current_version, latest_version, outdated, cache_hit),
            )
            return cursor.lastrowid or 0

    # ==================================================================
    # Session-Account Tracking
    # ==================================================================

    def record_session_account(
        self,
        session_id: str,
        account_id: Optional[int] = None,
        email: Optional[str] = None,
        detection_method: Optional[str] = None,
        repo_path: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> int:
        """Record which account a session is using.

        Closes stale records for different accounts on the same session and
        prevents duplicate rows for the same session+account combo.

        >>> db = Database(":memory:")
        >>> rid = db.record_session_account("sess-1", account_id=1, email="a@b.com", detection_method="session_start")
        >>> rid > 0
        True
        >>> rows = db.get_session_accounts("sess-1")
        >>> len(rows)
        1
        >>> rows[0]["email"]
        'a@b.com'
        >>> rid2 = db.record_session_account("sess-1", account_id=1, email="a@b.com", detection_method="session_start")
        >>> len(db.get_session_accounts("sess-1"))
        1
        >>> rid3 = db.record_session_account("sess-1", account_id=2, email="b@b.com", detection_method="session_start")
        >>> rows = db.get_session_accounts("sess-1")
        >>> sum(1 for r in rows if r["ended_at"] is None)
        1
        >>> rows[0]["account_id"]
        2
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            # End any open records for this session under a DIFFERENT account
            # (account_id != ? doesn't match NULLs, so OR account_id IS NULL)
            if account_id is not None:
                conn.execute(
                    """UPDATE session_accounts SET ended_at = ?
                       WHERE session_id = ? AND ended_at IS NULL
                         AND (account_id != ? OR account_id IS NULL)""",
                    (ts, session_id, account_id),
                )

            # Check if open record already exists for same session+account
            # (IS used instead of = for NULL-safe comparison)
            existing = conn.execute(
                """SELECT id FROM session_accounts
                   WHERE session_id = ? AND account_id IS ? AND ended_at IS NULL
                   LIMIT 1""",
                (session_id, account_id),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE session_accounts SET last_activity_at = ? WHERE id = ?",
                    (ts, existing[0]),
                )
                return existing[0]

            cursor = conn.execute(
                """INSERT OR IGNORE INTO session_accounts
                   (session_id, account_id, email, detected_at, last_activity_at,
                    detection_method, repo_path, pid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, account_id, email, ts, ts, detection_method, repo_path, pid),
            )
            return cursor.lastrowid or 0

    def end_session_account(self, session_id: str) -> bool:
        """Mark the latest session-account record as ended.

        >>> db = Database(":memory:")
        >>> _ = db.record_session_account("sess-1", account_id=1, email="a@b.com")
        >>> db.end_session_account("sess-1")
        True
        >>> rows = db.get_session_accounts("sess-1")
        >>> rows[0]["ended_at"] is not None
        True
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE session_accounts SET ended_at = ?
                   WHERE session_id = ? AND ended_at IS NULL""",
                (ts, session_id),
            )
            return cursor.rowcount > 0

    def heartbeat_session(self, session_id: str) -> bool:
        """Update last_activity_at for the most recent open session record.

        Only updates the newest open record, not all of them.

        >>> db = Database(":memory:")
        >>> _ = db.record_session_account("sess-1", account_id=1, email="a@b.com")
        >>> db.heartbeat_session("sess-1")
        True
        >>> db.heartbeat_session("nonexistent")
        False
        >>> db.end_session_account("sess-1")
        True
        >>> db.heartbeat_session("sess-1")
        False
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE session_accounts SET last_activity_at = ?
                   WHERE id = (
                       SELECT id FROM session_accounts
                       WHERE session_id = ? AND ended_at IS NULL
                       ORDER BY detected_at DESC LIMIT 1
                   )""",
                (ts, session_id),
            )
            return cursor.rowcount > 0

    def get_session_accounts(self, session_id: str) -> list:
        """Get account spans for a session.

        >>> db = Database(":memory:")
        >>> db.get_session_accounts("nonexistent")
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT id, session_id, account_id, email, detected_at,
                          ended_at, detection_method, repo_path, pid
                   FROM session_accounts
                   WHERE session_id = ?
                   ORDER BY detected_at DESC""",
                (session_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_account_sessions(self, account_id: int, limit: int = 50) -> list:
        """Get recent sessions that used a given account.

        >>> db = Database(":memory:")
        >>> db.get_account_sessions(999)
        []
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT id, session_id, account_id, email, detected_at,
                          ended_at, detection_method, repo_path
                   FROM session_accounts
                   WHERE account_id = ?
                   ORDER BY detected_at DESC
                   LIMIT ?""",
                (account_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_sessions(
        self, staleness_minutes: int = SESSION_STALENESS_MINUTES
    ) -> list:
        """Get session-account records with recent heartbeat activity.

        Uses COALESCE(last_activity_at, detected_at) with a configurable
        staleness window (default 60 min, clamped to 5-120).
        Sessions fade from view when idle but are NOT permanently closed —
        a heartbeat update makes them reappear (resurrection).

        >>> db = Database(":memory:")
        >>> db.get_active_sessions()
        []
        >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")
        >>> active = db.get_active_sessions()
        >>> len(active)
        1
        >>> active[0]["repo_path"]
        '/repo/a'
        >>> active[0]["last_activity_at"] is not None
        True
        """
        clamped = max(5, min(120, staleness_minutes))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=clamped)).isoformat()
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT session_id, account_id, email,
                          MIN(detected_at) AS detected_at,
                          detection_method, repo_path,
                          MAX(COALESCE(last_activity_at, detected_at)) AS last_activity_at,
                          is_subagent, parent_session_id, agent_type
                   FROM session_accounts
                   WHERE ended_at IS NULL
                     AND COALESCE(last_activity_at, detected_at) > ?
                   GROUP BY session_id, account_id
                   ORDER BY last_activity_at DESC""",
                (cutoff,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_stale_open_sessions(
        self, staleness_minutes: int = SESSION_STALENESS_MINUTES
    ) -> list:
        """Get sessions that are stale (past staleness window) but not ended.

        Inverse of get_active_sessions — returns sessions that have gone
        silent but were never closed. Used by the process-alive sweeper.

        >>> db = Database(":memory:")
        >>> db.get_stale_open_sessions()
        []
        """
        clamped = max(5, min(120, staleness_minutes))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=clamped)).isoformat()
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT s.session_id,
                          s.last_activity_at,
                          s.detected_at,
                          s.pid
                   FROM session_accounts s
                   INNER JOIN (
                       SELECT session_id,
                              MAX(COALESCE(last_activity_at, detected_at)) AS last_activity_at,
                              MAX(detected_at) AS max_detected_at
                       FROM session_accounts
                       WHERE ended_at IS NULL
                         AND COALESCE(last_activity_at, detected_at) <= ?
                       GROUP BY session_id
                   ) g ON s.session_id = g.session_id
                      AND s.detected_at = g.max_detected_at
                   WHERE s.ended_at IS NULL
                   ORDER BY s.last_activity_at ASC""",
                (cutoff,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def bump_all_stale_sessions(
        self, staleness_minutes: int = SESSION_STALENESS_MINUTES
    ) -> int:
        """Bump last_activity_at on all stale-but-open sessions.

        Used by the process-alive sweeper when Claude processes are still
        running. Returns the number of rows updated.

        >>> db = Database(":memory:")
        >>> db.bump_all_stale_sessions()
        0
        """
        clamped = max(5, min(120, staleness_minutes))
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=clamped)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE session_accounts SET last_activity_at = ?
                   WHERE ended_at IS NULL
                     AND COALESCE(last_activity_at, detected_at) <= ?""",
                (now, cutoff),
            )
            return cursor.rowcount

    def close_dead_sessions(self, hours: int = DEAD_SESSION_HOURS) -> int:
        """Close sessions that have been stale for more than `hours`.

        Used by the process-alive sweeper when no Claude processes are
        running. Returns the number of rows updated.

        >>> db = Database(":memory:")
        >>> db.close_dead_sessions()
        0
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE session_accounts SET ended_at = ?
                   WHERE ended_at IS NULL
                     AND COALESCE(last_activity_at, detected_at) <= ?""",
                (now, cutoff),
            )
            return cursor.rowcount

    def reassign_sessions(
        self, from_account_id: int, to_account_id: int, since_iso: str
    ) -> int:
        """Reassign sessions from one account to another.

        Batch-fixes wrongly-tagged sessions. Updates both account_id and email.
        Both account IDs must exist and the target must not be deleted.

        Args:
            from_account_id: Source account (wrongly tagged)
            to_account_id: Target account (correct)
            since_iso: ISO timestamp cutoff — only sessions after this are reassigned

        Returns:
            Count of sessions reassigned

        >>> db = Database(":memory:")
        >>> a = db.create_account("a@x.com", "tok", 9999999999)
        >>> b = db.create_account("b@x.com", "tok", 9999999999)
        >>> db.reassign_sessions(a["id"], b["id"], "2025-01-01T00:00:00Z")
        0
        >>> db.reassign_sessions(999, b["id"], "2025-01-01T00:00:00Z")
        Traceback (most recent call last):
        ...
        ValueError: Source account 999 not found
        """
        from_acct = self.get_account(from_account_id)
        to_acct = self.get_account(to_account_id)
        if not from_acct:
            raise ValueError(f"Source account {from_account_id} not found")
        if not to_acct:
            raise ValueError(f"Target account {to_account_id} not found")
        if to_acct.get("is_deleted"):
            raise ValueError(f"Target account {to_account_id} is deleted")

        with self._writer() as conn:
            cursor = conn.execute(
                """UPDATE session_accounts
                   SET account_id = ?, email = ?
                   WHERE account_id = ? AND detected_at > ?""",
                (to_account_id, to_acct["email"], from_account_id, since_iso),
            )
            return cursor.rowcount

    def lookup_session_by_suffix(self, suffix: str, limit: int = 10) -> list:
        """Find session-account records by session_id suffix.

        Requires at least 8 characters. LIKE wildcards in the suffix
        are escaped to prevent broad matches.

        >>> db = Database(":memory:")
        >>> db.lookup_session_by_suffix("short")
        []
        >>> db.lookup_session_by_suffix("abcd1234")
        []
        >>> db.lookup_session_by_suffix("%")
        []
        """
        if len(suffix) < 8:
            return []
        # Escape LIKE wildcards — backslash first to avoid double-escaping
        safe = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._reader() as conn:
            cursor = conn.execute(
                """SELECT session_id, account_id, email, repo_path,
                          detected_at, ended_at,
                          COALESCE(last_activity_at, detected_at) AS last_activity_at
                   FROM session_accounts
                   WHERE session_id LIKE '%' || ? ESCAPE '\\'
                   ORDER BY detected_at DESC
                   LIMIT ?""",
                (safe, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ==================================================================
    # Analytics Query Methods (for API routes)
    # ==================================================================

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
                    "success_rate": round(row["successes"] / row["count"] * 100, 1)
                    if row["count"]
                    else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2)
                    if row["avg_ms"]
                    else None,
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
                    "success_rate": round(row["successes"] / row["count"] * 100, 1)
                    if row["count"]
                    else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2)
                    if row["avg_ms"]
                    else None,
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
                    "success_rate": round(row["successes"] / row["count"] * 100, 1)
                    if row["count"]
                    else 0,
                    "avg_duration_ms": round(row["avg_ms"], 2)
                    if row["avg_ms"]
                    else None,
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
        >>> db.record_hook_execution("Stop", hook_name="qa_suggest", repo_path="/repo/a", session_id="s1")
        1
        >>> db.record_hook_execution("Stop", hook_name="qa_suggest", repo_path="/repo/a", session_id="s2")
        2
        >>> summary = db.get_project_activity_summary()
        >>> len(summary)
        1
        >>> summary[0]["repo_path"]
        '/repo/a'
        >>> summary[0]["hook_executions"]
        2
        >>> summary[0]["unique_sessions"]
        2
        """
        with self._reader() as conn:
            cursor = conn.execute(
                """
                SELECT
                    repo_path,
                    SUM(cmd_total) as commands_run,
                    SUM(hook_total) as hook_executions,
                    MAX(last_ts) as last_activity,
                    MIN(first_ts) as first_seen,
                    unique_sessions
                FROM (
                    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
                           COUNT(*) as cmd_total, 0 as hook_total,
                           MAX(timestamp) as last_ts,
                           MIN(timestamp) as first_ts,
                           COUNT(DISTINCT session_id) as unique_sessions
                    FROM command_usage
                    WHERE repo_path IS NOT NULL AND repo_path != ''
                    GROUP BY REPLACE(repo_path, char(92), '/')

                    UNION ALL

                    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
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
        self,
        limit: int = 200,
        command_name: Optional[str] = None,
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
        self,
        limit: int = 200,
        offset: int = 0,
        hook_name: Optional[str] = None,
    ) -> dict:
        """List recent hook execution logs with pagination.

        Returns ``{"rows": [...], "total": N}``.

        >>> db = Database(":memory:")
        >>> db.list_hook_executions()
        {'rows': [], 'total': 0}
        >>> db.list_hook_executions(hook_name="session_indexing")
        {'rows': [], 'total': 0}
        """
        where = ""
        params: list = []
        if hook_name:
            where = " WHERE hook_name = ?"
            params.append(hook_name)

        with self._reader() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM hook_executions{where}",
                params,
            ).fetchone()[0]

            cursor = conn.execute(
                f"SELECT * FROM hook_executions{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            return {"rows": [dict(row) for row in cursor.fetchall()], "total": total}

    def list_version_checks(self, limit: int = 100, offset: int = 0) -> dict:
        """List recent version check logs with pagination.

        Returns ``{"rows": [...], "total": N}``.

        >>> db = Database(":memory:")
        >>> db.list_version_checks()
        {'rows': [], 'total': 0}
        """
        with self._reader() as conn:
            total = conn.execute("SELECT COUNT(*) FROM version_checks").fetchone()[0]
            cursor = conn.execute(
                "SELECT * FROM version_checks ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return {"rows": [dict(row) for row in cursor.fetchall()], "total": total}

    # ==================================================================
    # Swap Log
    # ==================================================================

    def record_swap(self, from_account_id, to_account_id, reason, trigger,
                    from_5h=None, from_7d=None, to_5h=None, to_7d=None,
                    status="committed", residency_seconds=None):
        """Record an account swap event. Returns the inserted row ID."""
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO swap_log
                   (from_account_id, to_account_id, reason, trigger,
                    from_5h_usage, from_7d_usage, to_5h_usage, to_7d_usage,
                    status, residency_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (from_account_id, to_account_id, reason, trigger,
                 from_5h, from_7d, to_5h, to_7d,
                 status, residency_seconds),
            )
            return cursor.lastrowid

    def update_swap_status(self, swap_id: int, status: str) -> None:
        """Update the status of a swap_log row (e.g. 'pending' -> 'committed'/'failed')."""
        with self._writer() as conn:
            conn.execute(
                "UPDATE swap_log SET status = ? WHERE id = ?",
                (status, swap_id),
            )

    def swaps_last_24h(self, committed_only: bool = True) -> int:
        """Count swap events in the trailing 24 hours.

        >>> db = Database(":memory:")
        >>> db.swaps_last_24h()
        0
        """
        # Cutoff uses the same strftime format as the column DEFAULT so the
        # lexicographic comparison is exact (datetime('now') lacks 'T'/'Z').
        query = (
            "SELECT COUNT(*) FROM swap_log "
            "WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-24 hours')"
        )
        if committed_only:
            query += " AND status = 'committed'"
        with self._reader() as conn:
            return conn.execute(query).fetchone()[0]

    def list_swaps(self, limit=50):
        """List recent swap events with account emails and org info."""
        with self._reader() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          fa.email AS from_email,
                          fa.organization_name AS from_org_name,
                          fa.display_name AS from_display_name,
                          ta.email AS to_email,
                          ta.organization_name AS to_org_name,
                          ta.display_name AS to_display_name
                   FROM swap_log s
                   LEFT JOIN accounts fa ON fa.id = s.from_account_id
                   LEFT JOIN accounts ta ON ta.id = s.to_account_id
                   ORDER BY s.timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def record_decision(
        self,
        account_id: int | None,
        action: str,
        trigger: str | None = None,
        target_id: int | None = None,
        reason: str | None = None,
        detail: dict | None = None,
    ) -> int:
        """Record a swap decision (stay, swap, or manual_switch).

        Returns the inserted row ID.
        """
        import json as _json
        detail_str = _json.dumps(detail) if detail else None
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO decision_log
                   (account_id, action, trigger, target_id, reason, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, action, trigger, target_id, reason, detail_str),
            )
            return cursor.lastrowid

    def list_decisions(self, limit: int = 100, actions: list[str] | None = None) -> list[dict]:
        """List recent decision log entries, newest first."""
        import json as _json
        with self._reader() as conn:
            if actions:
                placeholders = ",".join("?" for _ in actions)
                rows = conn.execute(
                    f"""SELECT * FROM decision_log
                        WHERE action IN ({placeholders})
                        ORDER BY timestamp DESC LIMIT ?""",
                    (*actions, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM decision_log
                       ORDER BY timestamp DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("detail"):
                    try:
                        d["detail"] = _json.loads(d["detail"])
                    except (ValueError, TypeError):
                        pass
                result.append(d)
            return result

    def prune_decision_log(self, days: int = 7):
        """Delete decision log entries older than the given number of days."""
        with self._writer() as conn:
            conn.execute(
                "DELETE FROM decision_log WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
