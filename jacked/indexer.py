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
from datetime import datetime
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

    def __init__(self, config: SmartForkConfig, client: Optional[QdrantSessionClient] = None):
        """
        Initialize the indexer.

        Args:
            config: SmartForkConfig instance
            client: Optional QdrantSessionClient (created if not provided)
        """
        self.config = config
        self.client = client or QdrantSessionClient(config)

    def index_session(
        self,
        session_path: Path,
        repo_path: str,
        force: bool = False,
    ) -> dict:
        """
        Index a single session to Qdrant with all content types.

        Args:
            session_path: Path to the .jsonl session file
            repo_path: Full path to the repository
            force: If True, re-index even if unchanged

        Returns:
            Dict with indexing results:
            - session_id: The session ID
            - indexed: Whether the session was indexed
            - skipped: Whether it was skipped (unchanged)
            - plans: Number of plan points (0 or 1)
            - subagent_summaries: Number of subagent summary points
            - summary_labels: Number of summary label points
            - user_messages: Number of user message points
            - chunks: Number of transcript chunk points
            - error: Error message if failed

        Examples:
            >>> indexer = SessionIndexer(config)  # doctest: +SKIP
            >>> result = indexer.index_session(Path('session.jsonl'), '/c/Github/repo')  # doctest: +SKIP
        """
        result = {
            "session_id": session_path.stem,
            "indexed": False,
            "skipped": False,
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
            result["session_id"] = transcript.session_id

            # Check if we should skip (unchanged)
            if not force:
                current_hash = content_hash(transcript.full_text)
                existing = self._get_existing_hash(transcript.session_id)
                if existing == current_hash:
                    logger.debug(f"Session {transcript.session_id} unchanged, skipping")
                    result["skipped"] = True
                    return result

            # Build points for all content types
            points = self._build_points(transcript, repo_path)

            if not points:
                logger.warning(f"No points to index for session {transcript.session_id}")
                result["error"] = "No content to index"
                return result

            # Delete existing points for this session (if any)
            self.client.delete_by_session(transcript.session_id)

            # Upsert new points
            self.client.upsert_points(points)

            # Count results by content_type
            result["indexed"] = True
            for p in points:
                payload = p.payload or {}
                content_type = payload.get("content_type", payload.get("type"))
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
                f"Indexed session {transcript.session_id}: "
                f"{result['plans']} plan, "
                f"{result['subagent_summaries']} agent summaries, "
                f"{result['summary_labels']} labels, "
                f"{result['user_messages']} user msgs, "
                f"{result['chunks']} chunks"
            )

            return result

        except Exception as e:
            logger.error(f"Failed to index session {session_path}: {e}")
            result["error"] = str(e)
            return result

    def _get_existing_hash(self, session_id: str) -> Optional[str]:
        """
        Get the content hash of an existing indexed session.

        Args:
            session_id: Session ID to check

        Returns:
            Content hash string or None if not found
        """
        # Look for the first user_message point using deterministic UUID
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}:user_message:0"))
        point = self.client.get_point_by_id(point_id)
        if point and point.payload:
            return point.payload.get("content_hash")
        return None

    def _make_point_id(self, session_id: str, content_type: str, index: int) -> str:
        """Generate deterministic point ID.

        Args:
            session_id: The session UUID
            content_type: One of plan, subagent_summary, summary_label, user_message, chunk
            index: Index within that content type

        Returns:
            UUID5 string for the point
        """
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}:{content_type}:{index}"))

    def _build_points(
        self,
        transcript: EnrichedTranscript,
        repo_path: str,
    ) -> list[models.PointStruct]:
        """
        Build Qdrant points for all content types in a transcript.

        Creates points for:
        - plan: Full implementation strategy (if exists)
        - subagent_summary: Rich summaries from agent outputs
        - summary_label: Tiny chapter titles from compaction
        - user_message: First few user messages for intent matching
        - chunk: Full transcript chunks for full retrieval

        Args:
            transcript: EnrichedTranscript with all extracted data
            repo_path: Full path to the repository

        Returns:
            List of PointStruct objects
        """
        points = []
        repo_id = get_repo_id(repo_path)
        repo_name = get_repo_name(repo_path)
        full_hash = content_hash(transcript.full_text)
        timestamp_str = (
            transcript.timestamp.isoformat()
            if transcript.timestamp
            else datetime.now().isoformat()
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

        # 1. Plan file (gold - highest priority)
        if transcript.plan:
            point_id = self._make_point_id(transcript.session_id, "plan", 0)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=transcript.plan.content[:8000],  # Limit for embedding
                        model=INFERENCE_MODEL,
                    ),
                    payload={
                        **base_payload,
                        "type": "plan",  # Keep for backwards compat
                        "content_type": "plan",
                        "content": transcript.plan.content,
                        "plan_path": str(transcript.plan.path),
                    },
                )
            )

        # 2. Subagent summaries (gold)
        for i, agent_summary in enumerate(transcript.agent_summaries):
            point_id = self._make_point_id(transcript.session_id, "subagent_summary", i)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=agent_summary.summary_text[:8000],  # Limit for embedding
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

        # 3. Summary labels (chapter titles from compaction)
        for i, label in enumerate(transcript.summary_labels):
            point_id = self._make_point_id(transcript.session_id, "summary_label", i)
            points.append(
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

        # 4. User messages (first 5 for intent matching)
        max_user_messages = 5
        for i, msg in enumerate(transcript.user_messages[:max_user_messages]):
            if not msg.content or len(msg.content) < 20:
                continue
            point_id = self._make_point_id(transcript.session_id, "user_message", i)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=msg.content[:2000],  # Limit for embedding
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

        # 5. Transcript chunks (for full retrieval mode)
        transcript_chunks = chunk_text(
            transcript.full_text,
            chunk_size=self.config.chunk_size,
            overlap=self.config.chunk_overlap,
        )

        for i, chunk in enumerate(transcript_chunks):
            if not chunk.strip():
                continue

            point_id = self._make_point_id(transcript.session_id, "chunk", i)
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=chunk[:4000],  # Limit for embedding
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

        return points

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
