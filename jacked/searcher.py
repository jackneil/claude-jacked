"""
Session searching for Jacked.

Handles semantic search across indexed sessions using Qdrant Cloud Inference.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from collections import defaultdict

from jacked.config import SmartForkConfig, get_repo_id
from jacked.client import QdrantSessionClient


logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """
    A single search result representing a matched session.

    Attributes:
        session_id: The session UUID
        repo_name: Name of the repository
        repo_path: Full path to the repository
        machine: Machine name where the session was indexed
        timestamp: When the session was last indexed
        score: Relevance score (0-100)
        intent_preview: Preview of the matched intent text
        chunk_count: Number of transcript chunks stored
    """
    session_id: str
    repo_name: str
    repo_path: str
    machine: str
    timestamp: Optional[datetime]
    score: float
    intent_preview: str
    chunk_count: int

    def __str__(self) -> str:
        """Format result for display."""
        ts_str = self.timestamp.strftime("%Y-%m-%d") if self.timestamp else "unknown"
        return (
            f"[{self.score:.0f}%] {ts_str} - {self.repo_name} ({self.machine})\n"
            f"      {self.intent_preview[:80]}..."
        )


class SessionSearcher:
    """
    Searches for similar sessions in Qdrant using server-side embedding.

    Qdrant Cloud Inference handles embedding the query text.

    Attributes:
        config: SmartForkConfig instance
        client: QdrantSessionClient instance

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> searcher = SessionSearcher(config)  # doctest: +SKIP
        >>> results = searcher.search("implement overnight OB handling")  # doctest: +SKIP
    """

    def __init__(self, config: SmartForkConfig, client: Optional[QdrantSessionClient] = None):
        """
        Initialize the searcher.

        Args:
            config: SmartForkConfig instance
            client: Optional QdrantSessionClient (created if not provided)
        """
        self.config = config
        self.client = client or QdrantSessionClient(config)

    def search(
        self,
        query: str,
        repo_path: Optional[str] = None,
        limit: int = 10,
        min_score: float = 0.3,
    ) -> list[SearchResult]:
        """
        Search for sessions similar to the query.

        Qdrant Cloud Inference embeds the query server-side.

        Args:
            query: Text description of what you're looking for
            repo_path: Optional repo path to filter by
            limit: Maximum number of results
            min_score: Minimum cosine similarity score (0-1)

        Returns:
            List of SearchResult objects, sorted by relevance

        Examples:
            >>> searcher = SessionSearcher(config)  # doctest: +SKIP
            >>> results = searcher.search("fix anesthesia time handling")  # doctest: +SKIP
            >>> for r in results[:3]:  # doctest: +SKIP
            ...     print(r)
        """
        # Filter by repo if specified
        repo_id = get_repo_id(repo_path) if repo_path else None

        # Search for intent points using server-side embedding
        # Get more results than needed since we'll aggregate by session
        raw_results = self.client.search(
            query_text=query,
            repo_id=repo_id,
            point_type="intent",
            limit=limit * 5,  # Get extra for aggregation
        )

        # Aggregate by session (multiple intent chunks per session)
        session_scores: dict[str, list[float]] = defaultdict(list)
        session_data: dict[str, dict] = {}

        for result in raw_results:
            if result.score < min_score:
                continue

            payload = result.payload or {}
            session_id = payload.get("session_id")

            if not session_id:
                continue

            session_scores[session_id].append(result.score)

            # Keep the best payload data (highest score)
            if session_id not in session_data or result.score > max(session_scores[session_id][:-1], default=0):
                session_data[session_id] = payload

        # Build search results with aggregated scores
        results = []
        for session_id, scores in session_scores.items():
            # Use max score for ranking (best match in session)
            max_score = max(scores)
            payload = session_data[session_id]

            # Parse timestamp
            timestamp = None
            ts_str = payload.get("timestamp")
            if ts_str:
                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            results.append(
                SearchResult(
                    session_id=session_id,
                    repo_name=payload.get("repo_name", "unknown"),
                    repo_path=payload.get("repo_path", ""),
                    machine=payload.get("machine", "unknown"),
                    timestamp=timestamp,
                    score=max_score * 100,  # Convert to percentage
                    intent_preview=payload.get("intent_text", "")[:200],
                    chunk_count=payload.get("transcript_chunk_count", 0),
                )
            )

        # Sort by score (descending) and limit
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def search_by_repo(
        self,
        repo_path: str,
        limit: int = 20,
    ) -> list[SearchResult]:
        """
        List all sessions for a repository.

        Args:
            repo_path: Full path to the repository
            limit: Maximum number of results

        Returns:
            List of SearchResult objects, sorted by timestamp (newest first)
        """
        repo_id = get_repo_id(repo_path)
        sessions = self.client.list_sessions(repo_id=repo_id, limit=limit)

        results = []
        for session in sessions:
            timestamp = None
            ts_str = session.get("timestamp")
            if ts_str:
                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            results.append(
                SearchResult(
                    session_id=session.get("session_id", ""),
                    repo_name=session.get("repo_name", "unknown"),
                    repo_path=session.get("repo_path", ""),
                    machine=session.get("machine", "unknown"),
                    timestamp=timestamp,
                    score=100,  # No relevance score for list
                    intent_preview="",  # Not available in list
                    chunk_count=session.get("chunk_count", 0),
                )
            )

        # Sort by timestamp (newest first)
        results.sort(key=lambda r: r.timestamp or datetime.min, reverse=True)
        return results
