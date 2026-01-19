"""
Configuration management for Jacked.

Handles environment variables and configuration file loading.
"""

import os
import hashlib
import platform
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


@dataclass
class SmartForkConfig:
    """
    Configuration for Jacked.

    Attributes:
        qdrant_endpoint: Qdrant Cloud endpoint URL
        qdrant_api_key: Qdrant Cloud API key
        collection_name: Name of the Qdrant collection to use
        claude_projects_dir: Path to Claude's projects directory
        machine_name: Name of the current machine (for tracking)
        chunk_size: Size of transcript chunks in characters
        chunk_overlap: Overlap between chunks in characters
        intent_max_tokens: Max tokens for intent text (before chunking)

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> config.collection_name
        'claude_sessions'
    """

    qdrant_endpoint: str
    qdrant_api_key: str
    collection_name: str = "claude_sessions"
    claude_projects_dir: Path = field(default_factory=lambda: SmartForkConfig._default_claude_dir())
    machine_name: str = field(default_factory=lambda: platform.node())
    user_name: str = field(default_factory=lambda: SmartForkConfig._default_user_name())
    chunk_size: int = 4000  # ~4KB chunks
    chunk_overlap: int = 200  # 200 char overlap for context continuity
    intent_max_tokens: int = 400  # Max tokens per intent chunk (Qdrant model limit ~512)
    # Ranking weights for multi-factor search
    teammate_weight: float = 0.8  # Multiplier for teammate sessions (vs 1.0 for own)
    other_repo_weight: float = 0.7  # Multiplier for other repos (vs 1.0 for current)
    time_decay_halflife_weeks: int = 35  # Weeks until session relevance halves

    @staticmethod
    def _default_claude_dir() -> Path:
        """Get the default Claude projects directory."""
        home = Path.home()
        return home / ".claude" / "projects"

    @staticmethod
    def _default_user_name() -> str:
        """Get default user name from git config or system."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "config", "user.name"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        # Fallback to system username
        import getpass
        return getpass.getuser()

    @classmethod
    def from_env(cls, dotenv_path: Optional[Path] = None) -> "SmartForkConfig":
        """
        Load configuration from environment variables.

        Searches for .env file in:
        1. Explicit dotenv_path if provided
        2. Current working directory
        3. Package root directory (next to jacked/)

        Args:
            dotenv_path: Optional path to .env file

        Returns:
            SmartForkConfig instance

        Raises:
            ValueError: If required environment variables are missing

        Examples:
            >>> import os
            >>> os.environ['QDRANT_CLAUDE_SESSIONS_ENDPOINT'] = 'https://example.com'
            >>> os.environ['QDRANT_CLAUDE_SESSIONS_API_KEY'] = 'test-key'
            >>> config = SmartForkConfig.from_env()
            >>> config.qdrant_endpoint
            'https://example.com'
        """
        if dotenv_path:
            load_dotenv(dotenv_path)
        else:
            # Try current dir first
            load_dotenv()
            # Also try package root (parent of jacked/)
            package_root = Path(__file__).parent.parent / ".env"
            if package_root.exists():
                load_dotenv(package_root)

        endpoint = os.getenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT")
        api_key = os.getenv("QDRANT_CLAUDE_SESSIONS_API_KEY")

        if not endpoint:
            raise ValueError(
                "QDRANT_CLAUDE_SESSIONS_ENDPOINT environment variable is required. "
                "Set it to your Qdrant Cloud endpoint URL."
            )
        if not api_key:
            raise ValueError(
                "QDRANT_CLAUDE_SESSIONS_API_KEY environment variable is required. "
                "Set it to your Qdrant Cloud API key."
            )

        collection_name = os.getenv("QDRANT_CLAUDE_SESSIONS_COLLECTION", "claude_sessions")

        claude_dir_str = os.getenv("CLAUDE_PROJECTS_DIR")
        claude_dir = Path(claude_dir_str) if claude_dir_str else cls._default_claude_dir()

        machine_name = os.getenv("SMART_FORK_MACHINE_NAME", platform.node())
        user_name = os.getenv("JACKED_USER_NAME", cls._default_user_name())

        # Ranking weights
        teammate_weight = float(os.getenv("JACKED_TEAMMATE_WEIGHT", "0.8"))
        other_repo_weight = float(os.getenv("JACKED_OTHER_REPO_WEIGHT", "0.7"))
        time_decay_halflife = int(os.getenv("JACKED_TIME_DECAY_HALFLIFE_WEEKS", "35"))

        return cls(
            qdrant_endpoint=endpoint,
            qdrant_api_key=api_key,
            collection_name=collection_name,
            claude_projects_dir=claude_dir,
            machine_name=machine_name,
            user_name=user_name,
            teammate_weight=teammate_weight,
            other_repo_weight=other_repo_weight,
            time_decay_halflife_weeks=time_decay_halflife,
        )

    def validate(self) -> bool:
        """
        Validate the configuration.

        Returns:
            True if valid

        Raises:
            ValueError: If configuration is invalid
        """
        if not self.qdrant_endpoint.startswith("http"):
            raise ValueError(f"Invalid Qdrant endpoint: {self.qdrant_endpoint}")
        if not self.qdrant_api_key:
            raise ValueError("Qdrant API key cannot be empty")
        if not self.collection_name:
            raise ValueError("Collection name cannot be empty")
        return True


def get_repo_id(repo_path: str) -> str:
    """
    Generate a unique repo ID from the full repo path.

    Uses SHA256 hash to avoid name collisions (two repos with same name
    in different locations get different IDs).

    Args:
        repo_path: Full path to the repository

    Returns:
        First 8 characters of SHA256 hash

    Examples:
        >>> get_repo_id('/c/Github/hank-autocoder-keyphrases-llm')
        'e4b7c2a1'  # Will be consistent for this path
    """
    normalized = repo_path.replace("\\", "/").lower().rstrip("/")
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def get_repo_name(repo_path: str) -> str:
    """
    Extract the repository name from a path.

    Args:
        repo_path: Full path to the repository

    Returns:
        Name of the repository (last component of path)

    Examples:
        >>> get_repo_name('/c/Github/hank-autocoder-keyphrases-llm')
        'hank-autocoder-keyphrases-llm'
        >>> get_repo_name('C:\\\\Github\\\\my-project')
        'my-project'
    """
    normalized = repo_path.replace("\\", "/").rstrip("/")
    return normalized.split("/")[-1]


def get_session_dir_for_repo(claude_projects_dir: Path, repo_path: str) -> Path:
    """
    Get the Claude session directory for a given repo.

    Claude stores sessions in ~/.claude/projects/{encoded_repo_path}/

    Args:
        claude_projects_dir: Path to Claude's projects directory
        repo_path: Full path to the repository

    Returns:
        Path to the session directory

    Examples:
        >>> from pathlib import Path
        >>> get_session_dir_for_repo(Path('/home/user/.claude/projects'), '/c/Github/my-repo')
        PosixPath('/home/user/.claude/projects/-c-Github-my-repo')
    """
    # Claude encodes the repo path by replacing path separators with dashes
    # and removing the leading slash/drive letter
    normalized = repo_path.replace("\\", "/")
    if normalized.startswith("/"):
        normalized = normalized[1:]
    # Replace / with - for the directory name
    encoded = normalized.replace("/", "-")
    return claude_projects_dir / encoded


def content_hash(content: str) -> str:
    """
    Generate a hash of content for change detection.

    Args:
        content: Content to hash

    Returns:
        SHA256 hash prefixed with 'sha256:'

    Examples:
        >>> content_hash("Hello, world!")
        'sha256:315f5bdb76d078c43b8ac0064e4a0164612b1fce77c869345bfc94c75894edd3'
    """
    return f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
