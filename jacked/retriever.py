"""
Session retrieval for Jacked.

Handles retrieving full transcripts from Qdrant for context injection.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jacked.config import SmartForkConfig, get_session_dir_for_repo
from jacked.client import QdrantSessionClient


logger = logging.getLogger(__name__)


@dataclass
class RetrievedSession:
    """
    A retrieved session with full transcript.

    Attributes:
        session_id: The session UUID
        repo_name: Name of the repository
        repo_path: Full path to the repository
        machine: Machine name where the session was indexed
        full_transcript: The complete transcript text
        is_local: Whether the session exists locally (for native resume)
        local_path: Path to local session file (if exists)
    """
    session_id: str
    repo_name: str
    repo_path: str
    machine: str
    full_transcript: str
    is_local: bool
    local_path: Optional[Path]


class SessionRetriever:
    """
    Retrieves full session transcripts from Qdrant.

    Also checks if the session exists locally for native Claude resume.

    Attributes:
        config: SmartForkConfig instance
        client: QdrantSessionClient instance

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> retriever = SessionRetriever(config)  # doctest: +SKIP
        >>> session = retriever.retrieve("abc123-uuid")  # doctest: +SKIP
    """

    def __init__(self, config: SmartForkConfig, client: Optional[QdrantSessionClient] = None):
        """
        Initialize the retriever.

        Args:
            config: SmartForkConfig instance
            client: Optional QdrantSessionClient (created if not provided)
        """
        self.config = config
        self.client = client or QdrantSessionClient(config)

    def retrieve(self, session_id: str) -> Optional[RetrievedSession]:
        """
        Retrieve a session's full transcript.

        Args:
            session_id: The session UUID to retrieve

        Returns:
            RetrievedSession object or None if not found

        Examples:
            >>> retriever = SessionRetriever(config)  # doctest: +SKIP
            >>> session = retriever.retrieve("533e6824-6fb0-4f12-a406-517d2677734e")  # doctest: +SKIP
            >>> if session:  # doctest: +SKIP
            ...     print(f"Found session with {len(session.full_transcript)} chars")
        """
        # Get all points for this session
        points = self.client.get_points_by_session(session_id)

        if not points:
            logger.warning(f"Session {session_id} not found in index")
            return None

        # Separate intent and chunk points
        intent_points = []
        chunk_points = []

        for point in points:
            payload = point.payload or {}
            point_type = payload.get("type")

            if point_type == "intent":
                intent_points.append((payload.get("chunk_index", 0), payload))
            elif point_type == "chunk":
                chunk_points.append((payload.get("chunk_index", 0), payload))

        # Get metadata from first intent point
        repo_name = "unknown"
        repo_path = ""
        machine = "unknown"

        if intent_points:
            _, first_intent = sorted(intent_points, key=lambda x: x[0])[0]
            repo_name = first_intent.get("repo_name", "unknown")
            repo_path = first_intent.get("repo_path", "")
            machine = first_intent.get("machine", "unknown")

        # Reconstruct transcript from chunks
        if not chunk_points:
            logger.warning(f"Session {session_id} has no transcript chunks")
            return None

        # Sort chunks by index and reconstruct
        sorted_chunks = sorted(chunk_points, key=lambda x: x[0])
        full_transcript = self._reconstruct_transcript(sorted_chunks)

        # Check if session exists locally
        is_local, local_path = self._check_local_session(session_id, repo_path)

        return RetrievedSession(
            session_id=session_id,
            repo_name=repo_name,
            repo_path=repo_path,
            machine=machine,
            full_transcript=full_transcript,
            is_local=is_local,
            local_path=local_path,
        )

    def _reconstruct_transcript(
        self,
        sorted_chunks: list[tuple[int, dict]],
    ) -> str:
        """
        Reconstruct the full transcript from chunks.

        Handles overlap by removing duplicate content between chunks.

        Args:
            sorted_chunks: List of (index, payload) tuples, sorted by index

        Returns:
            Reconstructed transcript text
        """
        if not sorted_chunks:
            return ""

        # Simple approach: just concatenate chunks
        # The overlap helps ensure we don't lose content at boundaries
        # For retrieval, having some duplication is better than missing content
        parts = []
        for _, payload in sorted_chunks:
            content = payload.get("content", "")
            if content:
                parts.append(content)

        return "\n".join(parts)

    def _check_local_session(
        self,
        session_id: str,
        repo_path: str,
    ) -> tuple[bool, Optional[Path]]:
        """
        Check if the session exists locally.

        Args:
            session_id: The session UUID
            repo_path: Full path to the repository

        Returns:
            Tuple of (is_local, local_path)
        """
        if not repo_path:
            return False, None

        session_dir = get_session_dir_for_repo(
            self.config.claude_projects_dir, repo_path
        )
        session_file = session_dir / f"{session_id}.jsonl"

        if session_file.exists():
            return True, session_file

        return False, None

    def get_resume_command(self, session: RetrievedSession) -> Optional[str]:
        """
        Get the Claude CLI command to resume a session natively.

        Only works for local sessions.

        Args:
            session: RetrievedSession object

        Returns:
            CLI command string or None if not local
        """
        if session.is_local:
            return f"claude --resume {session.session_id}"
        return None

    def format_for_injection(
        self,
        session: RetrievedSession,
        max_length: int = 50000,
    ) -> str:
        """
        Format the transcript for injection into a conversation.

        Args:
            session: RetrievedSession object
            max_length: Maximum length of formatted output

        Returns:
            Formatted context string
        """
        header = (
            f"=== CONTEXT FROM PREVIOUS SESSION ===\n"
            f"Session: {session.session_id}\n"
            f"Repository: {session.repo_name}\n"
            f"Machine: {session.machine}\n"
            f"{'='*40}\n\n"
        )

        # Truncate transcript if needed
        transcript = session.full_transcript
        available = max_length - len(header) - 100  # Leave room for footer

        if len(transcript) > available:
            transcript = transcript[:available] + "\n... [transcript truncated]"

        footer = f"\n{'='*40}\n=== END PREVIOUS SESSION CONTEXT ===\n"

        return header + transcript + footer

    def get_summary(self, session: RetrievedSession, max_lines: int = 20) -> str:
        """
        Get a brief summary of the session for display.

        Args:
            session: RetrievedSession object
            max_lines: Maximum number of lines to show

        Returns:
            Summary string
        """
        lines = session.full_transcript.split("\n")

        # Get first and last N lines
        if len(lines) <= max_lines:
            preview = session.full_transcript
        else:
            half = max_lines // 2
            first_part = "\n".join(lines[:half])
            last_part = "\n".join(lines[-half:])
            preview = f"{first_part}\n\n... [{len(lines) - max_lines} lines omitted] ...\n\n{last_part}"

        return (
            f"Session {session.session_id} ({session.repo_name})\n"
            f"From machine: {session.machine}\n"
            f"Local: {'Yes' if session.is_local else 'No'}\n"
            f"\nPreview:\n{preview}"
        )
