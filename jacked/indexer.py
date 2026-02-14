"""
Session indexing for Jacked.

Handles parsing Claude sessions and upserting to Qdrant with server-side embedding.

Content types indexed:
- plan: Full implementation strategy from ~/.claude/plans/{slug}.md
- subagent_summary: Rich summaries from subagent outputs
- summary_label: Tiny chapter titles from compaction events
- user_message: First few user messages for intent matching
- chunk: Full transcript chunks for full retrieval mode
"""

import logging
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from qdrant_client.http import models

from jacked.config import (
    SmartForkConfig,
    get_repo_id,
    get_repo_name,
    content_hash,
)
from jacked.client import QdrantSessionClient, INFERENCE_MODEL
from jacked.transcript import (
    parse_jsonl_file_enriched,
    chunk_text,
    EnrichedTranscript,
)
from jacked.index_write_tracker import IndexWriteTracker


logger = logging.getLogger(__name__)


class SessionIndexer:
    """
    Indexes Claude sessions to Qdrant using server-side embedding.

    Creates multiple content types for each session:
    - plan: Full implementation strategy (gold - highest priority)
    - subagent_summary: Rich summaries from agent outputs (gold)
    - summary_label: Tiny chapter titles from compaction
    - user_message: First few user messages for intent matching
    - chunk: Full transcript chunks for full retrieval mode

    Qdrant Cloud Inference handles all embedding server-side.

    Attributes:
        config: SmartForkConfig instance
        client: QdrantSessionClient instance

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> indexer = SessionIndexer(config)  # doctest: +SKIP
        >>> indexer.index_session(Path('session.jsonl'), '/c/Github/repo')  # doctest: +SKIP
    """

    def __init__(
        self, config: SmartForkConfig, client: Optional[QdrantSessionClient] = None
    ):
        """
        Initialize the indexer.

        Args:
            config: SmartForkConfig instance
            client: Optional QdrantSessionClient (created if not provided)
        """
        self.config = config
        self.client = client or QdrantSessionClient(config)
        # Config hash for detecting chunk_size/overlap changes
        self._config_hash = content_hash(f"{config.chunk_size}:{config.chunk_overlap}")
        # Write tracker for incremental indexing (NOT for retrieval!)
        self._tracker = IndexWriteTracker(self._config_hash)

    def index_session(
        self,
        session_path: Path,
        repo_path: str,
        force: bool = False,
    ) -> dict:
        """
        Index a single session to Qdrant with incremental updates.

        Uses local SQLite tracker to avoid re-pushing unchanged content.
        Only indexes NEW or CHANGED points - much more efficient than
        the old delete-all-and-replace approach.

        Args:
            session_path: Path to the .jsonl session file
            repo_path: Full path to the repository
            force: If True, clear tracker and re-seed from Qdrant

        Returns:
            Dict with indexing results:
            - session_id: The session ID
            - indexed: Whether new content was indexed
            - skipped: Whether it was skipped (no new content)
            - new_points: Number of new/changed points indexed
            - plans, subagent_summaries, etc.: Counts by content type
            - error: Error message if failed

        Examples:
            >>> indexer = SessionIndexer(config)  # doctest: +SKIP
            >>> result = indexer.index_session(Path('session.jsonl'), '/c/Github/repo')  # doctest: +SKIP
        """
        result = {
            "session_id": session_path.stem,
            "indexed": False,
            "skipped": False,
            "new_points": 0,
            "plans": 0,
            "subagent_summaries": 0,
            "summary_labels": 0,
            "user_messages": 0,
            "chunks": 0,
            "error": None,
        }

        try:
            # Ensure collection exists
            self.client.ensure_collection()

            # Parse the transcript with enriched data
            transcript = parse_jsonl_file_enriched(session_path)
            session_id = transcript.session_id
            result["session_id"] = session_id

            # Check session metadata from tracker
            meta = self._tracker.get_session_meta(session_id)

            # Config changed? Clear and re-seed from Qdrant
            if meta and meta["config_hash"] != self._config_hash:
                logger.info(
                    f"Config changed for session {session_id}, re-seeding from Qdrant"
                )
                self._tracker.clear_session(session_id)
                meta = None

            # Previous crash mid-indexing? Force re-index
            if meta and meta["status"] == "indexing":
                logger.info(
                    f"Session {session_id} was interrupted mid-index, forcing re-seed"
                )
                force = True

            # Cache miss or force? Seed from Qdrant (source of truth, THIS USER ONLY)
            if meta is None or force:
                self._tracker.clear_session(session_id)
                self._tracker.seed_from_qdrant(
                    session_id, self.client, self.config.user_name
                )

            # Get what's already indexed
            indexed = self._tracker.get_session_state(session_id)

            # Mark as indexing BEFORE doing work (crash safety)
            self._tracker.mark_indexing(session_id)

            # Build only NEW/CHANGED points
            points_to_index, points_metadata = self._build_incremental_points(
                transcript, repo_path, indexed
            )

            if not points_to_index:
                self._tracker.mark_complete(session_id)
                result["skipped"] = True
                logger.debug(f"Session {session_id}: no new content to index")
                return result

            # Upsert to Qdrant (no delete needed - deterministic IDs handle overwrites)
            self.client.upsert_points(points_to_index)

            # Record what we indexed in tracker
            for content_type, idx, hash_val, point_id in points_metadata:
                self._tracker.record_indexed(
                    session_id, content_type, idx, hash_val, str(point_id)
                )

            self._tracker.mark_complete(session_id)

            # Count results by content_type
            result["indexed"] = True
            result["new_points"] = len(points_to_index)
            for content_type, _, _, _ in points_metadata:
                if content_type == "plan":
                    result["plans"] += 1
                elif content_type == "subagent_summary":
                    result["subagent_summaries"] += 1
                elif content_type == "summary_label":
                    result["summary_labels"] += 1
                elif content_type == "user_message":
                    result["user_messages"] += 1
                elif content_type == "chunk":
                    result["chunks"] += 1

            logger.info(
                f"Indexed session {session_id}: "
                f"{result['new_points']} new points ("
                f"{result['plans']} plan, "
                f"{result['subagent_summaries']} summaries, "
                f"{result['summary_labels']} labels, "
                f"{result['user_messages']} msgs, "
                f"{result['chunks']} chunks)"
            )

            return result

        except Exception as e:
            logger.error(f"Failed to index session {session_path}: {e}")
            result["error"] = str(e)
            return result

    def _make_point_id(self, session_id: str, content_type: str, index: int) -> str:
        """Generate deterministic point ID.

        Args:
            session_id: The session UUID
            content_type: One of plan, subagent_summary, summary_label, user_message, chunk
            index: Index within that content type

        Returns:
            UUID5 string for the point
        """
        return str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}:{content_type}:{index}")
        )

    def _build_incremental_points(
        self,
        transcript: EnrichedTranscript,
        repo_path: str,
        indexed: dict,
    ) -> tuple[list[models.PointStruct], list[tuple]]:
        """
        Build only NEW or CHANGED points by comparing against what's already indexed.

        Args:
            transcript: EnrichedTranscript with all extracted data
            repo_path: Full path to the repository
            indexed: Dict mapping (content_type, index) -> content_hash from tracker

        Returns:
            Tuple of (points_to_index, points_metadata) where points_metadata is
            a list of (content_type, index, content_hash, point_id) tuples
        """
        points_to_index = []
        points_metadata = []  # (content_type, index, hash, point_id)

        repo_id = get_repo_id(repo_path)
        repo_name = get_repo_name(repo_path)
        full_hash = content_hash(transcript.full_text)
        timestamp_str = (
            transcript.timestamp.isoformat()
            if transcript.timestamp
            else datetime.now(timezone.utc).isoformat()
        )

        # Base payload for all points
        base_payload = {
            "repo_id": repo_id,
            "repo_name": repo_name,
            "repo_path": repo_path,
            "session_id": transcript.session_id,
            "user_name": self.config.user_name,
            "machine": self.config.machine_name,
            "timestamp": timestamp_str,
            "content_hash": full_hash,
            "slug": transcript.slug,
        }

        # 1. Plan - check hash
        if transcript.plan:
            plan_hash = content_hash(transcript.plan.content)
            if indexed.get(("plan", 0)) != plan_hash:
                point_id = self._make_point_id(transcript.session_id, "plan", 0)
                points_to_index.append(
                    models.PointStruct(
                        id=point_id,
                        vector=models.Document(
                            text=transcript.plan.content[:8000],
                            model=INFERENCE_MODEL,
                        ),
                        payload={
                            **base_payload,
                            "type": "plan",
                            "content_type": "plan",
                            "content": transcript.plan.content,
                            "plan_path": str(transcript.plan.path),
                            "chunk_index": 0,
                        },
                    )
                )
                points_metadata.append(("plan", 0, plan_hash, point_id))

        # 2. User messages - compare by content hash
        max_user_messages = 5
        for i, msg in enumerate(transcript.user_messages[:max_user_messages]):
            if not msg.content or len(msg.content) < 20:
                continue
            msg_hash = content_hash(msg.content)
            if indexed.get(("user_message", i)) != msg_hash:
                point_id = self._make_point_id(transcript.session_id, "user_message", i)
                points_to_index.append(
                    models.PointStruct(
                        id=point_id,
                        vector=models.Document(
                            text=msg.content[:2000],
                            model=INFERENCE_MODEL,
                        ),
                        payload={
                            **base_payload,
                            "type": "user_message",
                            "content_type": "user_message",
                            "content": msg.content,
                            "chunk_index": i,
                        },
                    )
                )
                points_metadata.append(("user_message", i, msg_hash, point_id))

        # 3. Agent summaries - compare by hash
        for i, agent_summary in enumerate(transcript.agent_summaries):
            summary_hash = content_hash(agent_summary.summary_text)
            if indexed.get(("subagent_summary", i)) != summary_hash:
                point_id = self._make_point_id(
                    transcript.session_id, "subagent_summary", i
                )
                points_to_index.append(
                    models.PointStruct(
                        id=point_id,
                        vector=models.Document(
                            text=agent_summary.summary_text[:8000],
                            model=INFERENCE_MODEL,
                        ),
                        payload={
                            **base_payload,
                            "type": "subagent_summary",
                            "content_type": "subagent_summary",
                            "content": agent_summary.summary_text,
                            "agent_id": agent_summary.agent_id,
                            "agent_type": agent_summary.agent_type,
                            "chunk_index": i,
                        },
                    )
                )
                points_metadata.append(("subagent_summary", i, summary_hash, point_id))

        # 4. Summary labels - compare by hash
        for i, label in enumerate(transcript.summary_labels):
            label_hash = content_hash(label.label)
            if indexed.get(("summary_label", i)) != label_hash:
                point_id = self._make_point_id(
                    transcript.session_id, "summary_label", i
                )
                points_to_index.append(
                    models.PointStruct(
                        id=point_id,
                        vector=models.Document(
                            text=label.label,
                            model=INFERENCE_MODEL,
                        ),
                        payload={
                            **base_payload,
                            "type": "summary_label",
                            "content_type": "summary_label",
                            "content": label.label,
                            "leaf_uuid": label.leaf_uuid,
                            "chunk_index": i,
                        },
                    )
                )
                points_metadata.append(("summary_label", i, label_hash, point_id))

        # 5. Chunks - compare by hash (handles boundary drift)
        transcript_chunks = chunk_text(
            transcript.full_text,
            chunk_size=self.config.chunk_size,
            overlap=self.config.chunk_overlap,
        )

        for i, chunk in enumerate(transcript_chunks):
            if not chunk.strip():
                continue
            chunk_hash = content_hash(chunk)
            if indexed.get(("chunk", i)) != chunk_hash:
                point_id = self._make_point_id(transcript.session_id, "chunk", i)
                points_to_index.append(
                    models.PointStruct(
                        id=point_id,
                        vector=models.Document(
                            text=chunk[:4000],
                            model=INFERENCE_MODEL,
                        ),
                        payload={
                            **base_payload,
                            "type": "chunk",
                            "content_type": "chunk",
                            "content": chunk,
                            "chunk_index": i,
                            "total_chunks": len(transcript_chunks),
                        },
                    )
                )
                points_metadata.append(("chunk", i, chunk_hash, point_id))

        return points_to_index, points_metadata

    def index_all_sessions(
        self,
        repo_pattern: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """
        Index all sessions in the Claude projects directory.

        Args:
            repo_pattern: Optional repo name pattern to filter by
            force: If True, re-index all sessions

        Returns:
            Dict with aggregate results:
            - total: Total sessions found
            - indexed: Number successfully indexed
            - skipped: Number skipped (unchanged)
            - errors: Number with errors
        """
        from jacked.transcript import find_session_files

        results = {
            "total": 0,
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "details": [],
        }

        for session_path, repo_path in find_session_files(
            self.config.claude_projects_dir, repo_pattern
        ):
            results["total"] += 1

            result = self.index_session(session_path, repo_path, force=force)
            results["details"].append(result)

            if result.get("indexed"):
                results["indexed"] += 1
            elif result.get("skipped"):
                results["skipped"] += 1
            elif result.get("error"):
                results["errors"] += 1

        return results


def index_current_session(config: SmartForkConfig) -> dict:
    """
    Index the current Claude session.

    Called by the Stop hook to index after each response.

    Args:
        config: SmartForkConfig instance

    Returns:
        Indexing result dict
    """
    import os

    session_id = os.getenv("CLAUDE_SESSION_ID")
    project_dir = os.getenv("CLAUDE_PROJECT_DIR")

    if not session_id or not project_dir:
        return {
            "error": "CLAUDE_SESSION_ID or CLAUDE_PROJECT_DIR not set",
            "indexed": False,
        }

    # Find the session file
    from jacked.config import get_session_dir_for_repo

    session_dir = get_session_dir_for_repo(config.claude_projects_dir, project_dir)
    session_path = session_dir / f"{session_id}.jsonl"

    if not session_path.exists():
        return {
            "error": f"Session file not found: {session_path}",
            "indexed": False,
        }

    indexer = SessionIndexer(config)
    return indexer.index_session(session_path, project_dir)
