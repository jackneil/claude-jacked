"""
Session searching for Jacked.

Handles semantic search across indexed sessions using Qdrant Cloud Inference.
Implements multi-factor ranking: ownership, repo, recency, and semantic similarity.

Supports filtering by content_type:
- plan: Full implementation strategy (gold)
- subagent_summary: Rich summaries from agent outputs (gold)
- summary_label: Tiny chapter titles from compaction
- user_message: User messages for intent matching
- chunk: Full transcript chunks
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from jacked.config import SmartForkConfig, get_repo_id
from jacked.client import QdrantSessionClient


# Default content types for search (high-value content)
DEFAULT_SEARCH_CONTENT_TYPES = [
    "plan",
    "subagent_summary",
    "summary_label",
    "user_message",
]


logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """
    A single search result representing a matched session.

    Attributes:
        session_id: The session UUID
        repo_name: Name of the repository
        repo_path: Full path to the repository
        user_name: Name of the user who created the session
        machine: Machine name where the session was indexed
        timestamp: When the session was last indexed
        score: Final relevance score (0-100) after multi-factor ranking
        semantic_score: Raw semantic similarity score (0-100)
        is_own: Whether this is the current user's session
        is_current_repo: Whether this is from the current repo
        intent_preview: Preview of the matched intent text
        chunk_count: Number of transcript chunks stored
        has_plan: Whether this session has a plan file indexed
        has_agent_summaries: Whether this session has agent summaries
        content_types_found: Set of content types found in this session
    """
    session_id: str
    repo_name: str
    repo_path: str
    user_name: str
    machine: str
    timestamp: Optional[datetime]
    score: float
    semantic_score: float
    is_own: bool
    is_current_repo: bool
    intent_preview: str
    chunk_count: int
    has_plan: bool = False
    has_agent_summaries: bool = False
    content_types_found: set = field(default_factory=set)

    def __str__(self) -> str:
        """Format result for display."""
        ts_str = self.timestamp.strftime("%Y-%m-%d") if self.timestamp else "unknown"
        owner = "YOU" if self.is_own else f"@{self.user_name}"
        # Add indicators for rich content
        indicators = []
        if self.has_plan:
            indicators.append("📋")
        if self.has_agent_summaries:
            indicators.append("🤖")
        indicator_str = " ".join(indicators)
        return (
            f"[{self.score:.0f}%] {owner} - {self.repo_name} - {ts_str} {indicator_str}\n"
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
        mine_only: bool = False,
        user_filter: Optional[str] = None,
        content_types: Optional[list[str]] = None,
    ) -> list[SearchResult]:
        """
        Search for sessions similar to the query with multi-factor ranking.

        Ranking factors:
        - Semantic similarity (from Qdrant)
        - Ownership (own sessions weighted higher)
        - Repository (current repo weighted higher)
        - Recency (recent sessions weighted higher)

        Args:
            query: Text description of what you're looking for
            repo_path: Optional repo path to boost (and filter if combined with mine_only)
            limit: Maximum number of results
            min_score: Minimum cosine similarity score (0-1)
            mine_only: If True, only return current user's sessions
            user_filter: If set, only return sessions from this user
            content_types: Content types to search (default: plan, subagent_summary,
                          summary_label, user_message). Use ["chunk"] for full-text search.

        Returns:
            List of SearchResult objects, sorted by multi-factor relevance

        Examples:
            >>> searcher = SessionSearcher(config)  # doctest: +SKIP
            >>> results = searcher.search("fix auth handling")  # doctest: +SKIP
            >>> for r in results[:3]:  # doctest: +SKIP
            ...     print(r)
        """
        current_repo_id = get_repo_id(repo_path) if repo_path else None
        current_user = self.config.user_name

        # Default to high-value content types
        if content_types is None:
            content_types = DEFAULT_SEARCH_CONTENT_TYPES

        # Build user filter for Qdrant (more efficient than post-filter)
        qdrant_user_filter = None
        if mine_only:
            qdrant_user_filter = current_user
        elif user_filter:
            qdrant_user_filter = user_filter

        # Search using content_types filter
        # Get more results than needed since we'll aggregate and re-rank
        raw_results = self.client.search(
            query_text=query,
            repo_id=None,  # Don't filter in Qdrant, we'll boost instead
            content_types=content_types,
            user_name=qdrant_user_filter,
            limit=limit * 10,  # Get extra for aggregation and filtering
        )

        # Aggregate by session (multiple points per session)
        session_scores: dict[str, list[float]] = defaultdict(list)
        session_data: dict[str, dict] = {}
        session_content_types: dict[str, set] = defaultdict(set)

        for result in raw_results:
            if result.score < min_score:
                continue

            payload = result.payload or {}
            session_id = payload.get("session_id")
            session_user = payload.get("user_name", "unknown")
            content_type = payload.get("content_type", payload.get("type", ""))

            if not session_id:
                continue

            session_scores[session_id].append(result.score)
            session_content_types[session_id].add(content_type)

            # Keep the best payload data (highest score)
            if session_id not in session_data or result.score > max(session_scores[session_id][:-1], default=0):
                session_data[session_id] = payload

        # Build search results with multi-factor ranking
        results = []
        for session_id, scores in session_scores.items():
            # Use max score for semantic ranking (best match in session)
            semantic_score = max(scores)
            payload = session_data[session_id]
            found_types = session_content_types[session_id]

            # Parse timestamp
            timestamp = None
            ts_str = payload.get("timestamp")
            if ts_str:
                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Determine ownership and repo match
            session_user = payload.get("user_name", "unknown")
            session_repo_id = payload.get("repo_id", "")
            is_own = session_user == current_user
            is_current_repo = current_repo_id and session_repo_id == current_repo_id

            # Calculate multi-factor score
            final_score = self._calculate_ranked_score(
                semantic_score=semantic_score,
                is_own=is_own,
                is_current_repo=is_current_repo,
                timestamp=timestamp,
            )

            # Get preview text - prefer content field over intent_text
            preview = payload.get("content", payload.get("intent_text", ""))[:200]

            results.append(
                SearchResult(
                    session_id=session_id,
                    repo_name=payload.get("repo_name", "unknown"),
                    repo_path=payload.get("repo_path", ""),
                    user_name=session_user,
                    machine=payload.get("machine", "unknown"),
                    timestamp=timestamp,
                    score=final_score * 100,  # Convert to percentage
                    semantic_score=semantic_score * 100,
                    is_own=is_own,
                    is_current_repo=is_current_repo,
                    intent_preview=preview,
                    chunk_count=payload.get("total_chunks", 0),
                    has_plan="plan" in found_types,
                    has_agent_summaries="subagent_summary" in found_types,
                    content_types_found=found_types,
                )
            )

        # Sort by final score (descending) and limit
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _calculate_ranked_score(
        self,
        semantic_score: float,
        is_own: bool,
        is_current_repo: bool,
        timestamp: Optional[datetime],
    ) -> float:
        """
        Calculate multi-factor ranked score.

        Formula: semantic * ownership_weight * repo_weight * time_decay

        Args:
            semantic_score: Raw semantic similarity (0-1)
            is_own: Whether this is current user's session
            is_current_repo: Whether this is from current repo
            timestamp: Session timestamp for decay calculation

        Returns:
            Final ranked score (0-1)
        """
        # Ownership weight
        ownership_weight = 1.0 if is_own else self.config.teammate_weight

        # Repo weight
        repo_weight = 1.0 if is_current_repo else self.config.other_repo_weight

        # Time decay (exponential decay with configurable half-life)
        time_decay = 1.0
        if timestamp:
            now = datetime.now(timezone.utc)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_weeks = (now - timestamp).days / 7
            halflife = self.config.time_decay_halflife_weeks
            time_decay = math.pow(0.5, age_weeks / halflife)

        return semantic_score * ownership_weight * repo_weight * time_decay

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
