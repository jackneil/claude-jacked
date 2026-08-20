"""Self-contained SQLite database for token consumption analytics.

Separate from jacked's main database to avoid lock contention. Uses WAL mode
and a threading.Lock for writes (same pattern as the main Database class).
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_hash TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_create_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    cost_usd REAL,
    cost_source TEXT DEFAULT 'estimate',
    is_subagent INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_hash);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date TEXT NOT NULL,
    project_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    total_messages INTEGER DEFAULT 0,
    total_sessions INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_create_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    cache_hit_ratio REAL,
    PRIMARY KEY (date, project_hash, model)
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    flag_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    session_id TEXT,
    project_hash TEXT,
    message TEXT NOT NULL,
    detail TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flags_active ON flags(resolved_at);

CREATE TABLE IF NOT EXISTS scan_state (
    file_path TEXT PRIMARY KEY,
    last_byte_offset INTEGER DEFAULT 0,
    last_mtime REAL,
    messages_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analytics_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ---------------------------------------------------------------------------
# Anthropic API pricing per million tokens (USD)
# Verify at: https://platform.claude.com/docs/en/about-claude/pricing
# Last verified: 2026-07-02
# ---------------------------------------------------------------------------

# Price tiers — single source of truth, mapped by full ID and short alias below.
_TIER_HAIKU  = {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25}
_TIER_SONNET = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}
_TIER_OPUS   = {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}
_TIER_FABLE  = {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50}

# Sonnet 5 launched (2026-06-30) with introductory pricing of $2/$10 through
# 2026-08-31; standard $3/$15 applies from 2026-09-01. Applies ONLY to the
# claude-sonnet-5 model ID; Sonnet 4.x keeps _TIER_SONNET, and the bare
# "sonnet" alias stays at standard pricing (legacy rows can't be dated to a
# specific Sonnet generation, so we price them conservatively).
_TIER_SONNET5_INTRO = {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}
_SONNET5_INTRO_END = "2026-09-01"  # first day STANDARD pricing applies (ISO date)

MODEL_PRICING: dict[str, dict[str, float]] = {
    # Full model IDs (stored in DB model column)
    "claude-haiku-4-5-20251001": _TIER_HAIKU,
    "claude-sonnet-4-5-20250929": _TIER_SONNET,
    "claude-sonnet-4-6": _TIER_SONNET,
    "claude-sonnet-5": _TIER_SONNET,
    "claude-opus-4-6": _TIER_OPUS,
    "claude-opus-4-7": _TIER_OPUS,
    "claude-opus-4-8": _TIER_OPUS,
    "claude-fable-5": _TIER_FABLE,
    "claude-mythos-5": _TIER_FABLE,
    # Short aliases (fallback for legacy rows without model column)
    "haiku": _TIER_HAIKU,
    "sonnet": _TIER_SONNET,
    "opus": _TIER_OPUS,
    "fable": _TIER_FABLE,
}


# ---------------------------------------------------------------------------
# Opus fallback pricing (used when model is unknown)
# ---------------------------------------------------------------------------

_OPUS_PRICING = MODEL_PRICING.get("claude-opus-4-6", {
    "input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25,
})


# ---------------------------------------------------------------------------
# Cost estimation helper
# ---------------------------------------------------------------------------

def _sonnet5_intro_active(at: str | None) -> bool:
    """True when the given ISO timestamp (or now, if absent) falls inside the
    Sonnet 5 introductory-pricing window. ISO date prefixes compare
    lexicographically, so a string comparison is exact."""
    ref = str(at)[:10] if at else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ref < _SONNET5_INTRO_END


def estimate_cost(model: str, input_t: int, output_t: int,
                  cache_read_t: int, cache_create_t: int,
                  at: str | None = None) -> float:
    """Estimate USD cost for a message based on model and token counts.

    Looks up the model in MODEL_PRICING. For versioned model IDs like
    ``claude-opus-4-6-20260401``, strips the date suffix and tries again.
    Unknown models fall back to Opus pricing. ``at`` is the message's ISO
    timestamp; it selects date-dependent pricing (Sonnet 5's $2/$10
    introductory window through 2026-08-31) and defaults to now when absent.

    >>> estimate_cost("claude-opus-4-6", 1_000_000, 0, 0, 0)
    5.0
    >>> estimate_cost("unknown-model", 0, 0, 0, 0)
    0.0
    >>> estimate_cost("claude-sonnet-5", 1_000_000, 0, 0, 0, at="2026-07-04T12:00:00Z")
    2.0
    >>> estimate_cost("claude-sonnet-5", 1_000_000, 0, 0, 0, at="2026-09-01T00:00:00Z")
    3.0
    >>> estimate_cost("claude-fable-6", 1_000_000, 0, 0, 0)  # future model: tier inferred from name
    10.0
    """
    resolved = model
    prices = MODEL_PRICING.get(model)

    # Try stripping date suffix: claude-opus-4-6-20260401 -> claude-opus-4-6
    if prices is None:
        parts = model.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
            resolved = parts[0]
            prices = MODEL_PRICING.get(resolved)

    # Date-dependent pricing: Sonnet 5 introductory window.
    if prices is not None and resolved == "claude-sonnet-5" and _sonnet5_intro_active(at):
        prices = _TIER_SONNET5_INTRO

    # Version-proofing: a model ID the map hasn't caught up with (a future
    # claude-fable-6, claude-opus-4-9, claude-mythos-preview) infers its tier
    # from the family name, so a new release never silently misprices a whole
    # tier (a fable-6 priced as Opus would undercount 2x). Fable/Mythos first:
    # tier names never co-occur in one ID except hypothetically, and the top
    # tier is the conservative match.
    if prices is None:
        lowered = model.lower()
        for family, tier in (("fable", _TIER_FABLE), ("mythos", _TIER_FABLE),
                             ("opus", _TIER_OPUS), ("sonnet", _TIER_SONNET),
                             ("haiku", _TIER_HAIKU)):
            if family in lowered:
                prices = tier
                break

    # Fallback to Opus
    if prices is None:
        prices = _OPUS_PRICING

    return (
        input_t / 1_000_000 * prices["input"]
        + output_t / 1_000_000 * prices["output"]
        + cache_read_t / 1_000_000 * prices["cache_read"]
        + cache_create_t / 1_000_000 * prices["cache_write"]
    )


# ---------------------------------------------------------------------------
# AnalyticsDB class
# ---------------------------------------------------------------------------

class AnalyticsDB:
    """Self-contained SQLite database for token consumption analytics.

    >>> db = AnalyticsDB(db_path=":memory:")
    >>> db.get_setting("foo") is None
    True
    >>> db.close()
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".claude" / "jacked-analytics.db")

        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._local = threading.local()

        # Create parent directory if needed (skip for :memory:)
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
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(messages)")
            }
            if "total_tokens" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN total_tokens INTEGER DEFAULT 0")
            if "cost_usd" not in columns:
                conn.execute("ALTER TABLE messages ADD COLUMN cost_usd REAL")
            if "cost_source" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN cost_source TEXT DEFAULT 'estimate'"
                )
            conn.execute(
                "UPDATE messages SET cost_source = 'estimate' "
                "WHERE cost_source IS NULL"
            )

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def insert_messages(self, messages: list[dict]) -> None:
        """INSERT OR IGNORE messages (dedup by id)."""
        if not messages:
            return
        with self._writer() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO messages "
                "(id, session_id, project_hash, timestamp, model, "
                " input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, "
                " total_tokens, estimated_cost_usd, cost_usd, cost_source, is_subagent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        m["id"], m["session_id"], m["project_hash"], m["timestamp"],
                        m.get("model"), m.get("input_tokens", 0), m.get("output_tokens", 0),
                        m.get("cache_read_tokens", 0), m.get("cache_create_tokens", 0),
                        m.get("total_tokens", m.get("input_tokens", 0) + m.get("output_tokens", 0) + m.get("cache_read_tokens", 0) + m.get("cache_create_tokens", 0)),
                        m.get("estimated_cost_usd", 0), m.get("cost_usd"),
                        m.get("cost_source", "estimate"), m.get("is_subagent", 0),
                    )
                    for m in messages
                ],
            )

    def get_messages_for_session(self, session_id: str) -> list[dict]:
        """SELECT * WHERE session_id, ordered by timestamp."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------

    def get_overview(self, days: int = 1) -> dict:
        """Aggregated stats: total tokens, cost, cache ratio, session count, project breakdown."""
        cutoff = _cutoff_iso(days)
        with self._reader() as conn:
            row = conn.execute(
                "SELECT "
                "  COALESCE(SUM(input_tokens), 0) AS total_input, "
                "  COALESCE(SUM(output_tokens), 0) AS total_output, "
                "  COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read, "
                "  COALESCE(SUM(cache_create_tokens), 0) AS total_cache_create, "
                "  COALESCE(SUM(COALESCE(cost_usd, estimated_cost_usd)), 0) AS total_cost, "
                "  COUNT(DISTINCT session_id) AS session_count, "
                "  COUNT(*) AS message_count "
                "FROM messages WHERE timestamp >= ?",
                (cutoff,),
            ).fetchone()

            total_input = row["total_input"]
            total_output = row["total_output"]
            total_cache_read = row["total_cache_read"]
            total_cache_create = row["total_cache_create"]
            total_tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS total FROM messages "
                "WHERE timestamp >= ?", (cutoff,)
            ).fetchone()["total"]

            # Cache hit ratio
            total_all_input = total_input + total_cache_read + total_cache_create
            cache_ratio = (total_cache_read / total_all_input) if total_all_input > 0 else 0.0

            # Project breakdown
            project_rows = conn.execute(
                "SELECT project_hash, "
                "  COUNT(*) AS messages, "
                "  COUNT(DISTINCT session_id) AS sessions, "
                "  COALESCE(SUM(COALESCE(cost_usd, estimated_cost_usd)), 0) AS cost "
                "FROM messages WHERE timestamp >= ? "
                "GROUP BY project_hash ORDER BY cost DESC",
                (cutoff,),
            ).fetchall()

        return {
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_create_tokens": total_cache_create,
            "total_cost_usd": row["total_cost"],
            "cache_hit_ratio": round(cache_ratio, 4),
            "session_count": row["session_count"],
            "message_count": row["message_count"],
            "project_breakdown": [
                {
                    "project_hash": r["project_hash"],
                    "messages": r["messages"],
                    "sessions": r["sessions"],
                    "cost_usd": r["cost"],
                }
                for r in project_rows
            ],
        }

    # ------------------------------------------------------------------
    # Session list
    # ------------------------------------------------------------------

    def get_session_list(self, days: int = 1, project_hash: str | None = None,
                         flagged_only: bool = False) -> list[dict]:
        """Sessions ranked by cost."""
        cutoff = _cutoff_iso(days)
        params: list = [cutoff]
        where_clauses = ["m.timestamp >= ?"]

        if project_hash is not None:
            where_clauses.append("m.project_hash = ?")
            params.append(project_hash)

        where_sql = " AND ".join(where_clauses)

        if flagged_only:
            query = (
                "SELECT m.session_id, "
                "  m.project_hash, "
                "  COUNT(*) AS message_count, "
                "  COALESCE(SUM(m.input_tokens), 0) AS input_tokens, "
                "  COALESCE(SUM(m.output_tokens), 0) AS output_tokens, "
                "  COALESCE(SUM(COALESCE(m.cost_usd, m.estimated_cost_usd)), 0) AS total_cost, "
                "  MIN(m.timestamp) AS first_message, "
                "  MAX(m.timestamp) AS last_message "
                f"FROM messages m INNER JOIN flags f ON f.session_id = m.session_id AND f.resolved_at IS NULL "
                f"WHERE {where_sql} "
                "GROUP BY m.session_id "
                "ORDER BY total_cost DESC"
            )
        else:
            query = (
                "SELECT m.session_id, "
                "  m.project_hash, "
                "  COUNT(*) AS message_count, "
                "  COALESCE(SUM(m.input_tokens), 0) AS input_tokens, "
                "  COALESCE(SUM(m.output_tokens), 0) AS output_tokens, "
                "  COALESCE(SUM(COALESCE(m.cost_usd, m.estimated_cost_usd)), 0) AS total_cost, "
                "  MIN(m.timestamp) AS first_message, "
                "  MAX(m.timestamp) AS last_message "
                f"FROM messages m "
                f"WHERE {where_sql} "
                "GROUP BY m.session_id "
                "ORDER BY total_cost DESC"
            )

        with self._reader() as conn:
            rows = conn.execute(query, params).fetchall()

        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Daily summaries
    # ------------------------------------------------------------------

    def get_daily_summaries(self, days: int = 7) -> list[dict]:
        """From daily_summaries table."""
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_summaries WHERE date >= ? ORDER BY date DESC",
                (cutoff_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def rollup_daily_summaries(self, date_str: str) -> None:
        """Aggregate messages for a date into daily_summaries. INSERT OR REPLACE."""
        next_date = (
            datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")

        with self._writer() as conn:
            rows = conn.execute(
                "SELECT project_hash, model, "
                "  COUNT(*) AS total_messages, "
                "  COUNT(DISTINCT session_id) AS total_sessions, "
                "  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
                "  COALESCE(SUM(cache_create_tokens), 0) AS cache_create_tokens, "
                "  COALESCE(SUM(COALESCE(cost_usd, estimated_cost_usd)), 0) AS estimated_cost_usd "
                "FROM messages "
                "WHERE timestamp >= ? AND timestamp < ? "
                "GROUP BY project_hash, model",
                (date_str, next_date),
            ).fetchall()

            for r in rows:
                total_input_all = r["input_tokens"] + r["cache_read_tokens"] + r["cache_create_tokens"]
                cache_ratio = (r["cache_read_tokens"] / total_input_all) if total_input_all > 0 else 0.0

                conn.execute(
                    "INSERT OR REPLACE INTO daily_summaries "
                    "(date, project_hash, model, total_messages, total_sessions, "
                    " input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, "
                    " estimated_cost_usd, cache_hit_ratio) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        date_str, r["project_hash"], r["model"],
                        r["total_messages"], r["total_sessions"],
                        r["input_tokens"], r["output_tokens"],
                        r["cache_read_tokens"], r["cache_create_tokens"],
                        r["estimated_cost_usd"], round(cache_ratio, 4),
                    ),
                )

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def purge_messages_older_than(self, days: int) -> None:
        """DELETE from messages WHERE timestamp < cutoff. Roll up before delete."""
        cutoff = _cutoff_iso(days)
        with self._writer() as conn:
            conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))

    # ------------------------------------------------------------------
    # Scan state
    # ------------------------------------------------------------------

    def update_scan_state(self, file_path: str, byte_offset: int,
                          mtime: float, count: int) -> None:
        """INSERT OR REPLACE scan state for a file."""
        with self._writer() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scan_state "
                "(file_path, last_byte_offset, last_mtime, messages_count) "
                "VALUES (?, ?, ?, ?)",
                (file_path, byte_offset, mtime, count),
            )

    def get_scan_state(self, file_path: str) -> dict | None:
        """Get scan state for a file, or None if not tracked."""
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM scan_state WHERE file_path = ?",
                (file_path,),
            ).fetchone()
        return dict(row) if row else None

    def prune_stale_scan_state(self, valid_paths: set[str]) -> None:
        """DELETE WHERE file_path NOT IN valid_paths."""
        with self._writer() as conn:
            if not valid_paths:
                conn.execute("DELETE FROM scan_state")
            else:
                placeholders = ",".join("?" for _ in valid_paths)
                conn.execute(
                    f"DELETE FROM scan_state WHERE file_path NOT IN ({placeholders})",
                    tuple(valid_paths),
                )

    # ------------------------------------------------------------------
    # Flags
    # ------------------------------------------------------------------

    def insert_flag(self, flag_type: str, severity: str,
                    session_id: str | None, project_hash: str | None,
                    message: str, detail: str | None = None) -> int:
        """INSERT a flag and return its rowid."""
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            cursor = conn.execute(
                "INSERT INTO flags (created_at, flag_type, severity, session_id, "
                " project_hash, message, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, flag_type, severity, session_id, project_hash, message, detail),
            )
            return cursor.lastrowid

    def get_active_flags(self) -> list[dict]:
        """WHERE resolved_at IS NULL."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM flags WHERE resolved_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_flag(self, flag_id: int) -> None:
        """UPDATE resolved_at for a single flag."""
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            conn.execute(
                "UPDATE flags SET resolved_at = ? WHERE id = ?",
                (now, flag_id),
            )

    def resolve_flags_for_session(self, session_id: str) -> None:
        """Bulk resolve all flags for a session."""
        now = datetime.now(timezone.utc).isoformat()
        with self._writer() as conn:
            conn.execute(
                "UPDATE flags SET resolved_at = ? WHERE session_id = ? AND resolved_at IS NULL",
                (now, session_id),
            )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Get a setting value, or default if not set."""
        with self._reader() as conn:
            row = conn.execute(
                "SELECT value FROM analytics_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """INSERT OR REPLACE a setting."""
        with self._writer() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analytics_settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the connection."""
        if hasattr(self._local, "connection") and self._local.connection is not None:
            self._local.connection.close()
            self._local.connection = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cutoff_iso(days: int) -> str:
    """Return UTC ISO timestamp for *days* ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()
