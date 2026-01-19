"""
Session indexing for Jacked.

Handles parsing Claude sessions and upserting to Qdrant with server-side embedding.
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
    parse_jsonl_file,
    chunk_text,
    chunk_intent_text,
    ParsedTranscript,
)


logger = logging.getLogger(__name__)


class SessionIndexer:
    """
    Indexes Claude sessions to Qdrant using server-side embedding.

    Creates two types of points for each session:
    - Intent points: User messages for semantic search
    - Chunk points: Full transcript chunks for retrieval

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
        Index a single session to Qdrant.

        Args:
            session_path: Path to the .jsonl session file
            repo_path: Full path to the repository
            force: If True, re-index even if unchanged

        Returns:
            Dict with indexing results:
            - session_id: The session ID
            - indexed: Whether the session was indexed
            - skipped: Whether it was skipped (unchanged)
            - intent_chunks: Number of intent chunks created
            - transcript_chunks: Number of transcript chunks created
            - error: Error message if failed

        Examples:
            >>> indexer = SessionIndexer(config)  # doctest: +SKIP
            >>> result = indexer.index_session(Path('session.jsonl'), '/c/Github/repo')  # doctest: +SKIP
        """
        result = {
            "session_id": session_path.stem,
            "indexed": False,
            "skipped": False,
            "intent_chunks": 0,
            "transcript_chunks": 0,
            "error": None,
        }

        try:
            # Ensure collection exists
            self.client.ensure_collection()

            # Parse the transcript
            transcript = parse_jsonl_file(session_path)
            result["session_id"] = transcript.session_id

            # Check if we should skip (unchanged)
            if not force:
                current_hash = content_hash(transcript.full_text)
                existing = self._get_existing_hash(transcript.session_id)
                if existing == current_hash:
                    logger.debug(f"Session {transcript.session_id} unchanged, skipping")
                    result["skipped"] = True
                    return result

            # Build points
            points = self._build_points(transcript, repo_path)

            if not points:
                logger.warning(f"No points to index for session {transcript.session_id}")
                result["error"] = "No content to index"
                return result

            # Delete existing points for this session (if any)
            self.client.delete_by_session(transcript.session_id)

            # Upsert new points
            self.client.upsert_points(points)

            # Count results
            result["indexed"] = True
            for p in points:
                payload = p.payload or {}
                if payload.get("type") == "intent":
                    result["intent_chunks"] += 1
                elif payload.get("type") == "chunk":
                    result["transcript_chunks"] += 1

            logger.info(
                f"Indexed session {transcript.session_id}: "
                f"{result['intent_chunks']} intent chunks, "
                f"{result['transcript_chunks']} transcript chunks"
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
        # Look for the first intent point using deterministic UUID
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{session_id}_intent_0"))
        point = self.client.get_point_by_id(point_id)
        if point and point.payload:
            return point.payload.get("content_hash")
        return None

    def _build_points(
        self,
        transcript: ParsedTranscript,
        repo_path: str,
    ) -> list[models.PointStruct]:
        """
        Build Qdrant points for a transcript.

        Uses models.Document for server-side embedding via Qdrant Cloud Inference.

        Args:
            transcript: Parsed transcript
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

        # Build intent points (user messages for semantic search)
        intent_chunks = chunk_intent_text(
            transcript.intent_text,
            max_tokens=self.config.intent_max_tokens,
        )

        # Get total transcript chunks for metadata
        transcript_chunks = chunk_text(
            transcript.full_text,
            chunk_size=self.config.chunk_size,
            overlap=self.config.chunk_overlap,
        )

        # Create intent points with Document for server-side embedding
        for i, chunk in enumerate(intent_chunks):
            if not chunk.strip():
                continue

            # Generate deterministic UUID from session_id + type + index
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{transcript.session_id}_intent_{i}"))
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=chunk,
                        model=INFERENCE_MODEL,
                    ),
                    payload={
                        "type": "intent",
                        "repo_id": repo_id,
                        "repo_name": repo_name,
                        "repo_path": repo_path,
                        "session_id": transcript.session_id,
                        "user_name": self.config.user_name,
                        "machine": self.config.machine_name,
                        "timestamp": timestamp_str,
                        "content_hash": full_hash,
                        "intent_text": chunk,
                        "chunk_index": i,
                        "total_chunks": len(intent_chunks),
                        "transcript_chunk_count": len(transcript_chunks),
                    },
                )
            )

        # Create transcript chunk points for retrieval
        for i, chunk in enumerate(transcript_chunks):
            if not chunk.strip():
                continue

            # Generate deterministic UUID from session_id + type + index
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{transcript.session_id}_chunk_{i}"))
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=models.Document(
                        text=chunk,
                        model=INFERENCE_MODEL,
                    ),
                    payload={
                        "type": "chunk",
                        "repo_id": repo_id,
                        "repo_name": repo_name,
                        "session_id": transcript.session_id,
                        "user_name": self.config.user_name,
                        "chunk_index": i,
                        "total_chunks": len(transcript_chunks),
                        "content": chunk,
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
