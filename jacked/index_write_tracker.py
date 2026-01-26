"""
SQLite-based tracker for what has been PUSHED to Qdrant.

WARNING: This is WRITE-SIDE ONLY. Used to track what we've already indexed
so we don't re-push unchanged content. This is NOT a read cache and MUST NOT
be used for search or retrieval - always query Qdrant directly for that.
"""
import sqlite3
import logging
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".claude" / "jacked_index_write_tracker.db"
MAX_SEED_POINTS = 5000  # Sanity limit to prevent OOM on pathological sessions


class IndexWriteTracker:
    """
    Tracks what content has been pushed to Qdrant to enable incremental indexing.

    WARNING: This is WRITE-SIDE ONLY tracking. NOT for retrieval/search.
    Always query Qdrant directly for search operations.

    The tracker uses SQLite for:
    - Indexed lookups (no loading entire file into memory)
    - Built-in locking for concurrent access (WAL mode)
    - ACID transactions for crash safety

    On cache miss or --force, seeds from Qdrant (source of truth).
    """

    def __init__(self, config_hash: str):
        """
        Initialize the write tracker.

        Args:
            config_hash: Hash of chunk_size:chunk_overlap to detect config changes
        """
        self.config_hash = config_hash
        self._init_db()

    def _init_db(self):
        """Initialize SQLite database with schema."""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS indexed_points (
                    session_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content_index INT NOT NULL,
                    content_hash TEXT NOT NULL,
                    qdrant_point_id TEXT NOT NULL,
                    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, content_type, content_index)
                );

                CREATE TABLE IF NOT EXISTS session_meta (
                    session_id TEXT PRIMARY KEY,
                    config_hash TEXT,
                    status TEXT DEFAULT 'complete',
                    last_indexed TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_session ON indexed_points(session_id);
            """)

    @contextmanager
    def _connect(self):
        """Context manager for DB connection with WAL mode for concurrency."""
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
        conn.execute("PRAGMA busy_timeout=30000")  # 30s retry on lock
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def is_indexed(self, session_id: str, content_type: str, index: int, content_hash: str) -> bool:
        """
        Check if specific content is already indexed with same hash.

        Args:
            session_id: Session UUID
            content_type: One of 'plan', 'chunk', 'user_message', 'agent_summary', 'summary_label'
            index: Index within content type (e.g., chunk 0, 1, 2...)
            content_hash: Hash of the content

        Returns:
            True if this exact content is already indexed
        """
        with self._connect() as conn:
            row = conn.execute("""
                SELECT 1 FROM indexed_points
                WHERE session_id = ? AND content_type = ? AND content_index = ? AND content_hash = ?
            """, (session_id, content_type, index, content_hash)).fetchone()
            return row is not None

    def get_session_state(self, session_id: str) -> dict:
        """
        Get all indexed content hashes for a session.

        Args:
            session_id: Session UUID

        Returns:
            Dict mapping (content_type, index) -> content_hash
        """
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT content_type, content_index, content_hash
                FROM indexed_points WHERE session_id = ?
            """, (session_id,)).fetchall()
            return {(r[0], r[1]): r[2] for r in rows}

    def get_session_meta(self, session_id: str) -> Optional[dict]:
        """
        Get session metadata.

        Args:
            session_id: Session UUID

        Returns:
            Dict with config_hash and status, or None if not found
        """
        with self._connect() as conn:
            row = conn.execute("""
                SELECT config_hash, status FROM session_meta WHERE session_id = ?
            """, (session_id,)).fetchone()
            return {"config_hash": row[0], "status": row[1]} if row else None

    def mark_indexing(self, session_id: str):
        """
        Mark session as indexing-in-progress (crash safety).

        If process crashes mid-index, next run will see 'indexing' status
        and force a re-seed from Qdrant.
        """
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO session_meta (session_id, config_hash, status, last_indexed)
                VALUES (?, ?, 'indexing', CURRENT_TIMESTAMP)
            """, (session_id, self.config_hash))

    def record_indexed(self, session_id: str, content_type: str, index: int,
                       content_hash: str, point_id: str):
        """
        Record that a point was successfully indexed to Qdrant.

        Args:
            session_id: Session UUID
            content_type: Type of content indexed
            index: Index within content type
            content_hash: Hash of content
            point_id: Qdrant point ID
        """
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO indexed_points
                (session_id, content_type, content_index, content_hash, qdrant_point_id)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, content_type, index, content_hash, point_id))

    def mark_complete(self, session_id: str):
        """Mark session indexing as complete."""
        with self._connect() as conn:
            conn.execute("""
                UPDATE session_meta SET status = 'complete', last_indexed = CURRENT_TIMESTAMP
                WHERE session_id = ?
            """, (session_id,))

    def clear_session(self, session_id: str):
        """
        Clear all tracked data for a session.

        Used before re-seeding from Qdrant on --force or config change.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM indexed_points WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_meta WHERE session_id = ?", (session_id,))

    def seed_from_qdrant(self, session_id: str, qdrant_client, user_name: str):
        """
        Seed tracker from what's actually in Qdrant FOR THIS USER ONLY.

        This is write-side only - we only care about what WE have indexed,
        not what other users have indexed. NOT for retrieval.

        Args:
            session_id: Session UUID
            qdrant_client: QdrantSessionClient instance
            user_name: Current user's name (for filtering)

        Raises:
            ValueError: If session has more than MAX_SEED_POINTS (pathological case)
        """
        points = qdrant_client.get_session_points(session_id, user_name)

        # Sanity limit to prevent OOM on pathological sessions
        if len(points) > MAX_SEED_POINTS:
            raise ValueError(
                f"Session {session_id} has {len(points)} points, exceeds limit {MAX_SEED_POINTS}"
            )

        logger.debug(f"Seeding tracker from Qdrant: {len(points)} points for session {session_id}")

        with self._connect() as conn:
            for point in points:
                payload = point.payload or {}
                conn.execute("""
                    INSERT OR REPLACE INTO indexed_points
                    (session_id, content_type, content_index, content_hash, qdrant_point_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    session_id,
                    payload.get("content_type") or payload.get("type"),
                    payload.get("chunk_index", 0),
                    payload.get("content_hash"),
                    str(point.id)
                ))
            conn.execute("""
                INSERT OR REPLACE INTO session_meta (session_id, config_hash, status)
                VALUES (?, ?, 'complete')
            """, (session_id, self.config_hash))
