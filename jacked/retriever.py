"""
Session retrieval for Jacked.

Handles retrieving session context from Qdrant with smart mode support.

Retrieval modes:
- smart: Plan + subagent summaries + labels + first user messages (default)
- plan: Just the plan file (if exists)
- labels: Just summary labels (tiny)
- agents: All subagent summaries
- full: Everything including full transcript chunks

Token budgeting ensures context fits within limits without truncation.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal

from jacked.config import SmartForkConfig, get_session_dir_for_repo
from jacked.client import QdrantSessionClient


logger = logging.getLogger(__name__)

# Retrieval modes
RetrievalMode = Literal["smart", "plan", "labels", "agents", "full"]

# Default token budget for context injection
DEFAULT_MAX_TOKENS = 15000
CHARS_PER_TOKEN = 4  # Approximate


@dataclass
class SessionContent:
    """Content from a single session organized by type."""
    plan: Optional[str] = None
    subagent_summaries: list[str] = field(default_factory=list)
    summary_labels: list[str] = field(default_factory=list)
    user_messages: list[str] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)

    def estimate_tokens(self) -> dict[str, int]:
        """Estimate token count for each content type."""
        def _tokens(text: str) -> int:
            return len(text) // CHARS_PER_TOKEN

        return {
            "plan": _tokens(self.plan or ""),
            "subagent_summaries": sum(_tokens(s) for s in self.subagent_summaries),
            "summary_labels": sum(_tokens(l) for l in self.summary_labels),
            "user_messages": sum(_tokens(m) for m in self.user_messages),
            "chunks": sum(_tokens(c) for c in self.chunks),
            "total": (
                _tokens(self.plan or "") +
                sum(_tokens(s) for s in self.subagent_summaries) +
                sum(_tokens(l) for l in self.summary_labels) +
                sum(_tokens(m) for m in self.user_messages)
                # Don't include chunks in total by default (full mode only)
            ),
        }


@dataclass
class RetrievedSession:
    """
    A retrieved session with content organized by type.

    Attributes:
        session_id: The session UUID
        repo_name: Name of the repository
        repo_path: Full path to the repository
        machine: Machine name where the session was indexed
        user_name: User who created the session
        timestamp: When the session was last indexed
        content: SessionContent with all content types
        is_local: Whether the session exists locally (for native resume)
        local_path: Path to local session file (if exists)
        slug: The session slug (links to plan file)
    """
    session_id: str
    repo_name: str
    repo_path: str
    machine: str
    user_name: str
    timestamp: Optional[datetime]
    content: SessionContent
    is_local: bool
    local_path: Optional[Path]
    slug: Optional[str] = None

    @property
    def full_transcript(self) -> str:
        """Get full transcript from chunks (backwards compatibility)."""
        return "\n".join(self.content.chunks)

    @property
    def age_days(self) -> int:
        """Get age of session in days."""
        if not self.timestamp:
            return 0
        now = datetime.now(timezone.utc)
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).days

    def format_relative_time(self) -> str:
        """Format timestamp as relative time (e.g., '24 days ago')."""
        if not self.timestamp:
            return "unknown"
        days = self.age_days
        if days == 0:
            return "today"
        elif days == 1:
            return "yesterday"
        elif days < 7:
            return f"{days} days ago"
        elif days < 30:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        elif days < 365:
            months = days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        else:
            years = days // 365
            return f"{years} year{'s' if years != 1 else ''} ago"


def get_staleness_warning(age_days: int) -> str:
    """Generate appropriate staleness warning based on age.

    Args:
        age_days: Age of the context in days

    Returns:
        Warning string or empty if recent enough
    """
    if age_days < 7:
        return ""  # No warning needed
    elif age_days < 30:
        return (
            f"ℹ️ This context is {age_days} days old. Code may have "
            "changed - verify current state if anything seems off."
        )
    elif age_days < 90:
        return (
            f"⚠️ STALENESS NOTICE: This context is {age_days} days old. "
            "Code, APIs, or project structure may have changed. Use this "
            "as a starting point for WHERE to look, not necessarily WHAT "
            "is there now."
        )
    else:
        return (
            f"🚨 OLD CONTEXT WARNING: This context is {age_days} days old "
            f"(~{age_days // 30} months). Significant changes are likely. "
            "Treat this as historical reference only - re-explore the "
            "codebase to understand current state before making changes."
        )


class SessionRetriever:
    """
    Retrieves session context from Qdrant with smart mode support.

    Retrieval modes:
    - smart: Plan + subagent summaries + labels + first user messages
    - plan: Just the plan file (if exists)
    - labels: Just summary labels (tiny)
    - agents: All subagent summaries
    - full: Everything including full transcript chunks

    Attributes:
        config: SmartForkConfig instance
        client: QdrantSessionClient instance

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> retriever = SessionRetriever(config)  # doctest: +SKIP
        >>> session = retriever.retrieve("abc123-uuid", mode="smart")  # doctest: +SKIP
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

    def retrieve(
        self,
        session_id: str,
        mode: RetrievalMode = "smart",
    ) -> Optional[RetrievedSession]:
        """
        Retrieve a session's context with specified mode.

        Args:
            session_id: The session UUID to retrieve
            mode: Retrieval mode (smart, plan, labels, agents, full)

        Returns:
            RetrievedSession object or None if not found

        Examples:
            >>> retriever = SessionRetriever(config)  # doctest: +SKIP
            >>> session = retriever.retrieve("533e6824-...", mode="smart")  # doctest: +SKIP
        """
        # Get all points for this session
        points = self.client.get_points_by_session(session_id)

        if not points:
            logger.warning(f"Session {session_id} not found in index")
            return None

        # Organize points by content type
        content = SessionContent()
        metadata = {}

        for point in points:
            payload = point.payload or {}
            content_type = payload.get("content_type", payload.get("type", ""))
            chunk_content = payload.get("content", "")

            # Save metadata from first point
            if not metadata:
                metadata = {
                    "repo_name": payload.get("repo_name", "unknown"),
                    "repo_path": payload.get("repo_path", ""),
                    "machine": payload.get("machine", "unknown"),
                    "user_name": payload.get("user_name", "unknown"),
                    "slug": payload.get("slug"),
                    "timestamp": payload.get("timestamp"),
                }

            # Organize by content type
            if content_type == "plan":
                content.plan = chunk_content
            elif content_type == "subagent_summary":
                content.subagent_summaries.append(
                    (payload.get("chunk_index", 0), chunk_content)
                )
            elif content_type == "summary_label":
                content.summary_labels.append(
                    (payload.get("chunk_index", 0), chunk_content)
                )
            elif content_type == "user_message":
                content.user_messages.append(
                    (payload.get("chunk_index", 0), chunk_content)
                )
            elif content_type == "chunk":
                content.chunks.append(
                    (payload.get("chunk_index", 0), chunk_content)
                )
            elif content_type == "intent":
                # Legacy: treat as user_message
                content.user_messages.append(
                    (payload.get("chunk_index", 0), payload.get("intent_text", chunk_content))
                )

        # Sort content by chunk_index and extract just the text
        content.subagent_summaries = [
            text for _, text in sorted(content.subagent_summaries, key=lambda x: x[0])
        ]
        content.summary_labels = [
            text for _, text in sorted(content.summary_labels, key=lambda x: x[0])
        ]
        content.user_messages = [
            text for _, text in sorted(content.user_messages, key=lambda x: x[0])
        ]
        content.chunks = [
            text for _, text in sorted(content.chunks, key=lambda x: x[0])
        ]

        # Parse timestamp
        timestamp = None
        ts_str = metadata.get("timestamp")
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Check if session exists locally
        repo_path = metadata.get("repo_path", "")
        is_local, local_path = self._check_local_session(session_id, repo_path)

        return RetrievedSession(
            session_id=session_id,
            repo_name=metadata.get("repo_name", "unknown"),
            repo_path=repo_path,
            machine=metadata.get("machine", "unknown"),
            user_name=metadata.get("user_name", "unknown"),
            timestamp=timestamp,
            content=content,
            is_local=is_local,
            local_path=local_path,
            slug=metadata.get("slug"),
        )

    def _check_local_session(
        self,
        session_id: str,
        repo_path: str,
    ) -> tuple[bool, Optional[Path]]:
        """Check if the session exists locally."""
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
        """Get the Claude CLI command to resume a session natively."""
        if session.is_local:
            return f"claude --resume {session.session_id}"
        return None

    def format_for_injection(
        self,
        session: RetrievedSession,
        mode: RetrievalMode = "smart",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """
        Format the session context for injection into a conversation.

        Args:
            session: RetrievedSession object
            mode: Retrieval mode determining what content to include
            max_tokens: Maximum token budget (smart mode only)

        Returns:
            Formatted context string with staleness warning if needed
        """
        # Build header with relative time
        relative_time = session.format_relative_time()
        staleness_warning = get_staleness_warning(session.age_days)

        header_parts = [
            "=== CONTEXT FROM PREVIOUS SESSION ===",
            f"Session: {session.session_id}",
            f"Repository: {session.repo_name}",
            f"Machine: {session.machine}",
            f"Age: {relative_time}",
        ]

        if staleness_warning:
            header_parts.append("")
            header_parts.append(staleness_warning)

        header_parts.append("=" * 40)
        header = "\n".join(header_parts) + "\n\n"

        # Build content based on mode
        if mode == "plan":
            body = self._format_plan_only(session)
        elif mode == "labels":
            body = self._format_labels_only(session)
        elif mode == "agents":
            body = self._format_agents_only(session)
        elif mode == "full":
            body = self._format_full(session)
        else:  # smart
            body = self._format_smart(session, max_tokens)

        footer = f"\n{'='*40}\n=== END PREVIOUS SESSION CONTEXT ===\n"

        return header + body + footer

    def _format_plan_only(self, session: RetrievedSession) -> str:
        """Format plan-only mode."""
        if session.content.plan:
            return f"[PLAN]\n{session.content.plan}\n"
        return "[No plan file found for this session]\n"

    def _format_labels_only(self, session: RetrievedSession) -> str:
        """Format labels-only mode."""
        if session.content.summary_labels:
            labels_text = "\n".join(f"• {label}" for label in session.content.summary_labels)
            return f"[SUMMARY LABELS]\n{labels_text}\n"
        return "[No summary labels found for this session]\n"

    def _format_agents_only(self, session: RetrievedSession) -> str:
        """Format agents-only mode."""
        if session.content.subagent_summaries:
            parts = []
            for i, summary in enumerate(session.content.subagent_summaries, 1):
                parts.append(f"[AGENT SUMMARY {i}]\n{summary}\n")
            return "\n".join(parts)
        return "[No subagent summaries found for this session]\n"

    def _format_full(self, session: RetrievedSession) -> str:
        """Format full mode with everything."""
        parts = []

        if session.content.plan:
            parts.append(f"[PLAN]\n{session.content.plan}\n")

        if session.content.subagent_summaries:
            for i, summary in enumerate(session.content.subagent_summaries, 1):
                parts.append(f"[AGENT SUMMARY {i}]\n{summary}\n")

        if session.content.summary_labels:
            labels_text = "\n".join(f"• {label}" for label in session.content.summary_labels)
            parts.append(f"[SUMMARY LABELS]\n{labels_text}\n")

        if session.content.user_messages:
            parts.append("[USER MESSAGES]")
            for i, msg in enumerate(session.content.user_messages, 1):
                parts.append(f"USER {i}: {msg[:500]}{'...' if len(msg) > 500 else ''}\n")

        if session.content.chunks:
            parts.append(f"\n[FULL TRANSCRIPT - {len(session.content.chunks)} chunks]")
            for chunk in session.content.chunks:
                parts.append(chunk)

        return "\n".join(parts)

    def _format_smart(
        self,
        session: RetrievedSession,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """
        Format smart mode with token budgeting.

        Priority (never truncate, just exclude lower priority items):
        1. Plan file (full)
        2. Subagent summaries (all if space)
        3. First 3 user messages
        4. Summary labels
        """
        parts = []
        tokens_used = 0
        max_chars = max_tokens * CHARS_PER_TOKEN

        def _add_if_fits(text: str, label: str) -> bool:
            """Add text if it fits in budget."""
            nonlocal tokens_used, parts
            text_tokens = len(text) // CHARS_PER_TOKEN
            if tokens_used + text_tokens <= max_tokens:
                parts.append(f"{label}\n{text}\n")
                tokens_used += text_tokens
                return True
            return False

        # 1. Plan file (highest priority - always include if exists)
        if session.content.plan:
            plan_tokens = len(session.content.plan) // CHARS_PER_TOKEN
            parts.append(f"[PLAN - {plan_tokens} tokens]\n{session.content.plan}\n")
            tokens_used += plan_tokens

        # 2. Subagent summaries
        for i, summary in enumerate(session.content.subagent_summaries, 1):
            summary_tokens = len(summary) // CHARS_PER_TOKEN
            if tokens_used + summary_tokens <= max_tokens:
                parts.append(f"[AGENT SUMMARY {i} - {summary_tokens} tokens]\n{summary}\n")
                tokens_used += summary_tokens
            else:
                logger.debug(f"Skipping agent summary {i} - would exceed token budget")

        # 3. First 3 user messages
        for i, msg in enumerate(session.content.user_messages[:3], 1):
            msg_tokens = len(msg) // CHARS_PER_TOKEN
            if tokens_used + msg_tokens <= max_tokens:
                parts.append(f"[USER MESSAGE {i} - {msg_tokens} tokens]\n{msg}\n")
                tokens_used += msg_tokens
            else:
                logger.debug(f"Skipping user message {i} - would exceed token budget")

        # 4. Summary labels (usually small enough to fit)
        if session.content.summary_labels:
            labels_text = "\n".join(f"• {label}" for label in session.content.summary_labels)
            labels_tokens = len(labels_text) // CHARS_PER_TOKEN
            if tokens_used + labels_tokens <= max_tokens:
                parts.append(f"[SUMMARY LABELS - {labels_tokens} tokens]\n{labels_text}\n")
                tokens_used += labels_tokens

        # Add token accounting footer
        remaining = max_tokens - tokens_used
        parts.append(f"\n[Token budget: {max_tokens} | Used: {tokens_used} | Remaining: {remaining}]")

        return "\n".join(parts)

    def get_summary(self, session: RetrievedSession, max_lines: int = 20) -> str:
        """Get a brief summary of the session for display."""
        parts = [
            f"Session {session.session_id} ({session.repo_name})",
            f"Age: {session.format_relative_time()}",
            f"Machine: {session.machine}",
            f"Local: {'Yes' if session.is_local else 'No'}",
            "",
            "Content available:",
        ]

        if session.content.plan:
            parts.append(f"  • Plan file ({len(session.content.plan)} chars)")
        if session.content.subagent_summaries:
            parts.append(f"  • {len(session.content.subagent_summaries)} agent summaries")
        if session.content.summary_labels:
            parts.append(f"  • {len(session.content.summary_labels)} summary labels")
        if session.content.user_messages:
            parts.append(f"  • {len(session.content.user_messages)} user messages")
        if session.content.chunks:
            parts.append(f"  • {len(session.content.chunks)} transcript chunks")

        tokens = session.content.estimate_tokens()
        parts.append(f"\nEstimated tokens (smart mode): {tokens['total']}")

        return "\n".join(parts)
