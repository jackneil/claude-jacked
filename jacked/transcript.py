"""
Transcript parsing for Claude Code session files.

Handles parsing Claude's JSONL session format and extracting
user messages and conversation chunks.
"""

import json
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Iterator

logger = logging.getLogger(__name__)


@dataclass
class TranscriptMessage:
    """
    A single message from a Claude session transcript.

    Attributes:
        role: 'user' or 'assistant'
        content: The text content of the message
        timestamp: When the message was sent
        uuid: Unique identifier for the message
        is_meta: Whether this is a meta/system message
    """
    role: str
    content: str
    timestamp: Optional[datetime] = None
    uuid: Optional[str] = None
    is_meta: bool = False


@dataclass
class ParsedTranscript:
    """
    A fully parsed transcript with extracted data.

    Attributes:
        session_id: The session UUID
        messages: List of all messages
        user_messages: Just the user messages (for intent matching)
        full_text: Concatenated transcript text (for chunking)
        intent_text: Concatenated user messages (for intent vector)
        timestamp: Most recent message timestamp
    """
    session_id: str
    messages: list[TranscriptMessage] = field(default_factory=list)
    user_messages: list[TranscriptMessage] = field(default_factory=list)
    full_text: str = ""
    intent_text: str = ""
    timestamp: Optional[datetime] = None


def parse_jsonl_file(filepath: Path) -> ParsedTranscript:
    """
    Parse a Claude session JSONL file.

    Args:
        filepath: Path to the .jsonl session file

    Returns:
        ParsedTranscript with extracted messages and text

    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If file has invalid JSON

    Examples:
        >>> # transcript = parse_jsonl_file(Path('session.jsonl'))  # doctest: +SKIP
    """
    if not filepath.exists():
        raise FileNotFoundError(f"Session file not found: {filepath}")

    # Extract session_id from filename
    session_id = filepath.stem

    messages = []
    user_messages = []
    latest_timestamp = None

    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping invalid JSON at line {line_num}: {e}")
                continue

            msg_type = data.get("type")

            # Parse timestamp
            ts_str = data.get("timestamp")
            timestamp = None
            if ts_str:
                try:
                    # Handle ISO format with Z suffix
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if latest_timestamp is None or timestamp > latest_timestamp:
                        latest_timestamp = timestamp
                except ValueError:
                    pass

            if msg_type == "user":
                content = _extract_user_content(data)
                is_meta = data.get("isMeta", False)

                msg = TranscriptMessage(
                    role="user",
                    content=content,
                    timestamp=timestamp,
                    uuid=data.get("uuid"),
                    is_meta=is_meta,
                )
                messages.append(msg)

                # Only include non-meta user messages in intent
                if not is_meta and content and not _is_command_only(content):
                    user_messages.append(msg)

            elif msg_type == "assistant":
                content = _extract_assistant_content(data)
                if content:
                    msg = TranscriptMessage(
                        role="assistant",
                        content=content,
                        timestamp=timestamp,
                        uuid=data.get("uuid"),
                        is_meta=False,
                    )
                    messages.append(msg)

    # Build full text for chunking
    full_text = _build_full_text(messages)

    # Build intent text (user messages only)
    intent_text = _build_intent_text(user_messages)

    return ParsedTranscript(
        session_id=session_id,
        messages=messages,
        user_messages=user_messages,
        full_text=full_text,
        intent_text=intent_text,
        timestamp=latest_timestamp,
    )


def _extract_user_content(data: dict) -> str:
    """
    Extract text content from a user message.

    Args:
        data: The parsed JSON message object

    Returns:
        Extracted text content
    """
    message = data.get("message", {})
    content = message.get("content", "")

    if isinstance(content, str):
        # Strip XML tags from content for cleaner text
        clean = _strip_xml_tags(content)
        return clean.strip()
    elif isinstance(content, list):
        # Handle list content (rare for user messages)
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return _strip_xml_tags(" ".join(parts)).strip()

    return ""


def _extract_assistant_content(data: dict) -> str:
    """
    Extract text content from an assistant message.

    Args:
        data: The parsed JSON message object

    Returns:
        Extracted text content (excludes thinking, tool_use, etc.)
    """
    message = data.get("message", {})
    content = message.get("content", [])

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts).strip()

    return ""


def _strip_xml_tags(text: str) -> str:
    """
    Remove XML-style tags from text (command tags, system reminders, etc.).

    Args:
        text: Text potentially containing XML tags

    Returns:
        Text with XML tags removed

    Examples:
        >>> _strip_xml_tags('<command-name>test</command-name> hello')
        ' hello'
        >>> _strip_xml_tags('plain text')
        'plain text'
    """
    # Remove XML tags but keep the content between them for certain tags
    # that have user-relevant info
    keep_content_tags = ["local-command-stdout", "local-command-stderr"]

    for tag in keep_content_tags:
        pattern = f"<{tag}>(.*?)</{tag}>"
        text = re.sub(pattern, r"\1", text, flags=re.DOTALL)

    # Remove other XML tags entirely (including content)
    remove_tags = [
        "command-name", "command-message", "command-args",
        "local-command-caveat", "system-reminder",
    ]
    for tag in remove_tags:
        pattern = f"<{tag}>.*?</{tag}>"
        text = re.sub(pattern, "", text, flags=re.DOTALL)

    # Remove any remaining XML-style tags
    text = re.sub(r"<[^>]+>", "", text)

    return text


def _is_command_only(content: str) -> bool:
    """
    Check if content is only a command (no actual user text).

    Args:
        content: The cleaned message content

    Returns:
        True if content is only a slash command
    """
    content = content.strip()
    if not content:
        return True
    # If content is just a slash command like /login or /help
    if content.startswith("/") and " " not in content:
        return True
    # If content is very short (likely just command output acknowledgment)
    if len(content) < 10:
        return True
    return False


def _build_full_text(messages: list[TranscriptMessage]) -> str:
    """
    Build concatenated transcript text for chunking.

    Args:
        messages: List of transcript messages

    Returns:
        Full transcript as text with role markers
    """
    parts = []
    for msg in messages:
        if msg.content:
            role_marker = "USER:" if msg.role == "user" else "ASSISTANT:"
            parts.append(f"{role_marker}\n{msg.content}\n")
    return "\n".join(parts)


def _build_intent_text(user_messages: list[TranscriptMessage]) -> str:
    """
    Build concatenated user messages for intent matching.

    Args:
        user_messages: List of user messages (non-meta)

    Returns:
        Concatenated user messages
    """
    return "\n".join(msg.content for msg in user_messages if msg.content)


def chunk_text(text: str, chunk_size: int = 4000, overlap: int = 200) -> list[str]:
    """
    Split text into overlapping chunks for embedding.

    Args:
        text: Text to chunk
        chunk_size: Maximum size of each chunk in characters
        overlap: Number of characters to overlap between chunks

    Returns:
        List of text chunks

    Examples:
        >>> chunks = chunk_text("Hello world " * 100, chunk_size=100, overlap=20)
        >>> len(chunks) > 1
        True
        >>> all(len(c) <= 100 for c in chunks)
        True
    """
    if not text or len(text) <= chunk_size:
        return [text] if text else []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # Try to break at a natural boundary (newline, sentence, word)
        if end < text_len:
            # Look for newline first
            newline_pos = text.rfind("\n", start, end)
            if newline_pos > start + chunk_size // 2:
                end = newline_pos + 1
            else:
                # Look for sentence boundary
                for punct in [".", "!", "?"]:
                    punct_pos = text.rfind(punct, start + chunk_size // 2, end)
                    if punct_pos > start:
                        end = punct_pos + 1
                        break
                else:
                    # Fall back to word boundary
                    space_pos = text.rfind(" ", start + chunk_size // 2, end)
                    if space_pos > start:
                        end = space_pos + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Move start, accounting for overlap
        start = end - overlap if end < text_len else end

    return chunks


def chunk_intent_text(
    intent_text: str,
    max_tokens: int = 400,
    chars_per_token: float = 4.0,
) -> list[str]:
    """
    Chunk intent text into embedding-friendly sizes.

    Qdrant's embedding models have token limits (~512), so we need to
    chunk long intent texts.

    Args:
        intent_text: Concatenated user messages
        max_tokens: Maximum tokens per chunk
        chars_per_token: Approximate characters per token

    Returns:
        List of intent chunks
    """
    max_chars = int(max_tokens * chars_per_token)
    return chunk_text(intent_text, chunk_size=max_chars, overlap=50)


def find_session_files(projects_dir: Path, repo_pattern: Optional[str] = None) -> Iterator[tuple[Path, str]]:
    """
    Find all session JSONL files in Claude's projects directory.

    Args:
        projects_dir: Path to ~/.claude/projects/
        repo_pattern: Optional repo name pattern to filter by

    Yields:
        Tuples of (session_path, repo_path)

    Examples:
        >>> # for path, repo in find_session_files(Path('~/.claude/projects')):  # doctest: +SKIP
        >>> #     print(f'{path.name} in {repo}')
    """
    if not projects_dir.exists():
        logger.warning(f"Projects directory not found: {projects_dir}")
        return

    for repo_dir in projects_dir.iterdir():
        if not repo_dir.is_dir():
            continue

        # Decode repo path from directory name
        # Format: C--Github-repo-name -> C:/Github/repo-name
        repo_path = _decode_repo_path(repo_dir.name)

        if repo_pattern and repo_pattern.lower() not in repo_path.lower():
            continue

        # Find all .jsonl files that look like session files
        for session_file in repo_dir.glob("*.jsonl"):
            # Include UUID sessions and agent sessions
            if _is_uuid_format(session_file.stem) or session_file.stem.startswith("agent-"):
                yield session_file, repo_path


def _decode_repo_path(encoded: str) -> str:
    """
    Decode a Claude-encoded repo path from directory name.

    Args:
        encoded: The encoded directory name (e.g., C--Github-repo-name)

    Returns:
        Decoded path (e.g., C:/Github/repo-name)

    Examples:
        >>> _decode_repo_path('C--Github-repo-name')
        'C:/Github/repo-name'
        >>> _decode_repo_path('F--act-envision')
        'F:/act/envision'
    """
    # Replace -- with :/ (drive letter)
    # Then replace - with /
    # But this is tricky because repo names can have hyphens

    # The pattern seems to be: first part before -- is drive letter
    # Everything after is path with - as separator
    if "--" in encoded:
        parts = encoded.split("--", 1)
        drive = parts[0]
        rest = parts[1]
        # The rest uses - as path separator
        # Problem: can't distinguish between - in path and - in name
        # Best effort: assume single - is path separator
        return f"{drive}:/{rest.replace('-', '/')}"
    else:
        # No drive letter, just replace - with /
        return "/" + encoded.replace("-", "/")


def _is_uuid_format(name: str) -> bool:
    """
    Check if a string looks like a UUID.

    Args:
        name: String to check

    Returns:
        True if looks like UUID format

    Examples:
        >>> _is_uuid_format('533e6824-6fb0-4f12-a406-517d2677734e')
        True
        >>> _is_uuid_format('agent-a08e819')
        False
    """
    uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    return bool(re.match(uuid_pattern, name.lower()))


# =============================================================================
# NEW: Extraction functions for subagent summaries, labels, and plans
# =============================================================================


@dataclass
class SummaryLabel:
    """A compaction summary label (tiny chapter title).

    Examples:
        >>> label = SummaryLabel("Implementing auth flow", "abc123", None)
        >>> label.label
        'Implementing auth flow'
    """
    label: str
    leaf_uuid: Optional[str] = None
    timestamp: Optional[datetime] = None


@dataclass
class AgentSummary:
    """Summary extracted from a subagent's final output.

    Examples:
        >>> summary = AgentSummary("a4e75d5", "Explore", "## Summary\\n...", None)
        >>> summary.agent_id
        'a4e75d5'
    """
    agent_id: str
    agent_type: Optional[str]
    summary_text: str
    timestamp: Optional[datetime] = None


@dataclass
class PlanFile:
    """A plan file linked to a session via slug.

    Examples:
        >>> plan = PlanFile("hidden-finding-goose", Path("..."), "# Plan content")
        >>> plan.slug
        'hidden-finding-goose'
    """
    slug: str
    path: Path
    content: str


def find_subagent_files(session_path: Path) -> list[Path]:
    """Find all subagent JSONL files for a session.

    Subagents are stored in {session-id}/subagents/agent-*.jsonl

    Args:
        session_path: Path to the main session JSONL file

    Returns:
        List of paths to subagent JSONL files

    Examples:
        >>> # files = find_subagent_files(Path('session.jsonl'))  # doctest: +SKIP
    """
    # Session dir is {session-id}/ next to {session-id}.jsonl
    session_dir = session_path.parent / session_path.stem
    subagents_dir = session_dir / "subagents"

    if not subagents_dir.exists():
        return []

    # Use iterator to avoid memory issues with large directories
    return sorted(subagents_dir.glob("agent-*.jsonl"))


def extract_agent_summary(agent_file: Path) -> Optional[AgentSummary]:
    """Extract the final summary text from an agent's session.

    The summary is the text content from the LAST assistant message.

    Args:
        agent_file: Path to an agent-*.jsonl file

    Returns:
        AgentSummary or None if no usable summary found

    Examples:
        >>> # summary = extract_agent_summary(Path('agent-abc.jsonl'))  # doctest: +SKIP
    """
    # Extract agent_id from filename: agent-a4e75d5.jsonl -> a4e75d5
    agent_id = agent_file.stem.replace("agent-", "")

    last_assistant_msg = None
    timestamp = None

    try:
        with open(agent_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "assistant":
                    last_assistant_msg = data
                    ts_str = data.get("timestamp")
                    if ts_str:
                        try:
                            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            pass
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read agent file {agent_file}: {e}")
        return None

    if not last_assistant_msg:
        return None

    # Extract text content from the message
    message = last_assistant_msg.get("message", {})
    content = message.get("content", [])

    text_parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
    elif isinstance(content, str):
        text_parts.append(content)

    summary_text = "\n".join(text_parts).strip()

    # Skip if too short (likely not a real summary)
    if len(summary_text) < 200:
        logger.debug(f"Agent {agent_id} summary too short ({len(summary_text)} chars), skipping")
        return None

    # Agent type is hard to detect from the file itself
    # Could infer from content patterns but skip for MVP
    agent_type = None

    return AgentSummary(
        agent_id=agent_id,
        agent_type=agent_type,
        summary_text=summary_text,
        timestamp=timestamp,
    )


def extract_summary_labels(session_path: Path) -> list[SummaryLabel]:
    """Extract summary labels from a session JSONL file.

    These are the tiny "chapter titles" from compaction events.

    Args:
        session_path: Path to the main session JSONL file

    Returns:
        List of SummaryLabel objects

    Examples:
        >>> # labels = extract_summary_labels(Path('session.jsonl'))  # doctest: +SKIP
    """
    labels = []

    try:
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "summary":
                    label_text = data.get("summary", "")
                    if label_text:
                        timestamp = None
                        ts_str = data.get("timestamp")
                        if ts_str:
                            try:
                                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            except ValueError:
                                pass

                        labels.append(SummaryLabel(
                            label=label_text,
                            leaf_uuid=data.get("leafUuid"),
                            timestamp=timestamp,
                        ))
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read session file {session_path}: {e}")

    return labels


def extract_session_slug(session_path: Path) -> Optional[str]:
    """Extract the slug from a session JSONL file.

    The slug links the session to its plan file in ~/.claude/plans/

    Args:
        session_path: Path to the main session JSONL file

    Returns:
        The slug string or None if not found

    Examples:
        >>> # slug = extract_session_slug(Path('session.jsonl'))  # doctest: +SKIP
    """
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "slug" in data:
                    return data["slug"]
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read session file {session_path}: {e}")

    return None


def find_plan_file(slug: str, plans_dir: Optional[Path] = None) -> Optional[PlanFile]:
    """Find and read the plan file for a session slug.

    Args:
        slug: The session slug (e.g., "hidden-finding-goose")
        plans_dir: Path to plans directory (default: ~/.claude/plans/)

    Returns:
        PlanFile or None if not found or invalid

    Examples:
        >>> # plan = find_plan_file("hidden-finding-goose")  # doctest: +SKIP
    """
    if plans_dir is None:
        plans_dir = Path.home() / ".claude" / "plans"

    plan_path = plans_dir / f"{slug}.md"

    if not plan_path.exists():
        return None

    try:
        # Size validation - skip huge files
        file_size = plan_path.stat().st_size
        if file_size > 100_000:  # 100KB sanity check
            logger.warning(f"Plan file too large ({file_size} bytes), skipping: {plan_path}")
            return None

        if file_size < 50:  # Too small to be useful
            logger.debug(f"Plan file too small ({file_size} bytes), skipping: {plan_path}")
            return None

        content = plan_path.read_text(encoding="utf-8")

        return PlanFile(
            slug=slug,
            path=plan_path,
            content=content,
        )
    except (IOError, OSError) as e:
        logger.warning(f"Failed to read plan file {plan_path}: {e}")
        return None


@dataclass
class EnrichedTranscript(ParsedTranscript):
    """ParsedTranscript with additional extracted data.

    Adds subagent summaries, summary labels, and plan file content.
    """
    summary_labels: list[SummaryLabel] = field(default_factory=list)
    agent_summaries: list[AgentSummary] = field(default_factory=list)
    plan: Optional[PlanFile] = None
    slug: Optional[str] = None


def parse_jsonl_file_enriched(filepath: Path) -> EnrichedTranscript:
    """Parse a Claude session with all enriched data.

    Extracts:
    - Messages (user, assistant)
    - Summary labels (compaction chapter titles)
    - Subagent summaries (gold context from agent outputs)
    - Plan file (if linked via slug)

    Args:
        filepath: Path to the .jsonl session file

    Returns:
        EnrichedTranscript with all extracted data

    Examples:
        >>> # transcript = parse_jsonl_file_enriched(Path('session.jsonl'))  # doctest: +SKIP
    """
    # Parse base transcript
    base = parse_jsonl_file(filepath)

    # Extract summary labels
    labels = extract_summary_labels(filepath)

    # Extract subagent summaries
    agent_summaries = []
    for agent_file in find_subagent_files(filepath):
        summary = extract_agent_summary(agent_file)
        if summary:
            agent_summaries.append(summary)

    # Extract plan file via slug
    slug = extract_session_slug(filepath)
    plan = find_plan_file(slug) if slug else None

    return EnrichedTranscript(
        session_id=base.session_id,
        messages=base.messages,
        user_messages=base.user_messages,
        full_text=base.full_text,
        intent_text=base.intent_text,
        timestamp=base.timestamp,
        summary_labels=labels,
        agent_summaries=agent_summaries,
        plan=plan,
        slug=slug,
    )
