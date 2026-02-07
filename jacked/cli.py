"""
CLI for Jacked.

Provides command-line interface for indexing, searching, and
retrieving Claude Code sessions.
"""

import sys
import logging
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from jacked.config import SmartForkConfig, get_repo_id


console = Console()
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def get_config(quiet: bool = False) -> Optional[SmartForkConfig]:
    """Load configuration from environment.

    Args:
        quiet: If True, return None instead of printing error and exiting.
               Used by hooks that should fail gracefully.
    """
    try:
        return SmartForkConfig.from_env()
    except ValueError as e:
        if quiet:
            return None
        console.print(f"[red]Configuration error:[/red] {e}")
        console.print("\nSet these environment variables:")
        console.print("  QDRANT_CLAUDE_SESSIONS_ENDPOINT=<your-qdrant-url>")
        console.print("  QDRANT_CLAUDE_SESSIONS_API_KEY=<your-api-key>")
        sys.exit(1)


def _require_search(command_name: str) -> bool:
    """Check if qdrant-client is installed. If not, print helpful error and return False."""
    try:
        import qdrant_client  # noqa: F401
        return True
    except ImportError:
        console.print(f"[red]Error:[/red] '{command_name}' requires the search extra.")
        console.print('\nInstall it with:')
        console.print('  [bold]pip install "claude-jacked\[search]"[/bold]')
        console.print('  [bold]pipx install "claude-jacked\[search]"[/bold]')
        return False


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
def main(verbose: bool):
    """Jacked - Cross-machine context for Claude Code sessions."""
    setup_logging(verbose)


@main.command()
@click.argument("session", required=False)
@click.option("--repo", "-r", help="Repository path (defaults to CLAUDE_PROJECT_DIR)")
def index(session: Optional[str], repo: Optional[str]):
    """
    Index a Claude session to Qdrant.

    If SESSION is not provided, indexes the current session (from CLAUDE_SESSION_ID).
    Requires: pip install "claude-jacked[search]"
    """
    import os

    # Check if qdrant is available
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        # If called from Stop hook (CLAUDE_SESSION_ID set), exit silently
        # If called manually, show helpful message
        if os.getenv("CLAUDE_SESSION_ID") and not session:
            sys.exit(0)
        else:
            console.print("[red]Error:[/red] 'index' requires the search extra.")
            console.print('\nInstall it with:')
            console.print('  [bold]pip install "claude-jacked\[search]"[/bold]')
            sys.exit(1)

    from jacked.indexer import SessionIndexer

    # Try to get config quietly - if not configured, nudge and exit cleanly
    config = get_config(quiet=True)
    if config is None:
        print("[jacked] Indexing skipped - run 'jacked configure' to set up Qdrant")
        sys.exit(0)

    indexer = SessionIndexer(config)

    if session:
        # Index specific session by path or ID
        session_path = Path(session)
        if session_path.exists():
            # It's a file path
            repo_path = repo or os.getenv("CLAUDE_PROJECT_DIR", "")
            if not repo_path:
                console.print("[red]Error:[/red] --repo is required when indexing a file path")
                sys.exit(1)
        else:
            # Assume it's a session ID, find the file
            if not repo:
                repo = os.getenv("CLAUDE_PROJECT_DIR")
            if not repo:
                console.print("[red]Error:[/red] --repo or CLAUDE_PROJECT_DIR is required")
                sys.exit(1)

            from jacked.config import get_session_dir_for_repo
            session_dir = get_session_dir_for_repo(config.claude_projects_dir, repo)
            session_path = session_dir / f"{session}.jsonl"
            repo_path = repo

            if not session_path.exists():
                console.print(f"[red]Error:[/red] Session file not found: {session_path}")
                sys.exit(1)
    else:
        # Index current session
        session_id = os.getenv("CLAUDE_SESSION_ID")
        repo_path = os.getenv("CLAUDE_PROJECT_DIR")

        if not session_id or not repo_path:
            console.print("[red]Error:[/red] CLAUDE_SESSION_ID and CLAUDE_PROJECT_DIR not set")
            console.print("Provide a session path or run from within a Claude session")
            sys.exit(1)

        from jacked.config import get_session_dir_for_repo
        session_dir = get_session_dir_for_repo(config.claude_projects_dir, repo_path)
        session_path = session_dir / f"{session_id}.jsonl"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Indexing {session_path.stem}...", total=None)

        result = indexer.index_session(session_path, repo_path)

        progress.remove_task(task)

    if result.get("indexed"):
        console.print(
            f"[green][OK][/green] Indexed session {result['session_id']}: "
            f"{result['plans']}p {result['subagent_summaries']}a "
            f"{result['summary_labels']}l {result['user_messages']}u {result['chunks']}c"
        )
    elif result.get("skipped"):
        console.print(f"[yellow][-][/yellow] Session {result['session_id']} unchanged, skipped")
    else:
        console.print(f"[red][FAIL][/red] Failed: {result.get('error')}")
        sys.exit(1)


@main.command()
@click.option("--repo", "-r", help="Filter by repository name pattern")
@click.option("--force", "-f", is_flag=True, help="Re-index all sessions")
def backfill(repo: Optional[str], force: bool):
    """Index all existing Claude sessions. Requires: pip install "claude-jacked[search]" """
    if not _require_search("backfill"):
        sys.exit(1)

    from jacked.indexer import SessionIndexer

    config = get_config()
    indexer = SessionIndexer(config)

    console.print(f"Scanning {config.claude_projects_dir}...")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Indexing sessions...", total=None)

        results = indexer.index_all_sessions(repo_pattern=repo, force=force)

        progress.remove_task(task)

    console.print(
        f"\n[bold]Results:[/bold]\n"
        f"  Total:   {results['total']}\n"
        f"  Indexed: [green]{results['indexed']}[/green]\n"
        f"  Skipped: [yellow]{results['skipped']}[/yellow]\n"
        f"  Errors:  [red]{results['errors']}[/red]"
    )


@main.command()
@click.argument("query")
@click.option("--repo", "-r", help="Boost results from this repository path")
@click.option("--limit", "-n", default=5, help="Maximum results")
@click.option("--mine", "-m", is_flag=True, help="Only show my sessions")
@click.option("--user", "-u", help="Only show sessions from this user")
@click.option(
    "--type", "-t", "content_types",
    multiple=True,
    help="Filter by content type (plan, subagent_summary, summary_label, user_message, chunk)"
)
def search(query: str, repo: Optional[str], limit: int, mine: bool, user: Optional[str], content_types: tuple):
    """Search for sessions by semantic similarity with multi-factor ranking.

    Requires: pip install "claude-jacked[search]"
    """
    if not _require_search("search"):
        sys.exit(1)

    import os
    from jacked.searcher import SessionSearcher

    config = get_config()
    searcher = SessionSearcher(config)

    # Use current repo if not specified
    current_repo = repo or os.getenv("CLAUDE_PROJECT_DIR")

    # Convert tuple to list or None
    type_filter = list(content_types) if content_types else None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Searching...", total=None)

        results = searcher.search(
            query,
            repo_path=current_repo,
            limit=limit,
            mine_only=mine,
            user_filter=user,
            content_types=type_filter,
        )

        progress.remove_task(task)

    if not results:
        console.print("[yellow]No matching sessions found[/yellow]")
        return

    table = Table(title="Search Results", show_header=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Score", style="cyan", width=6)
    table.add_column("User", style="yellow", width=10)
    table.add_column("Age", style="green", width=12)
    table.add_column("Repo", style="magenta", width=15)
    table.add_column("Content", style="blue", width=8)
    table.add_column("Preview")

    for i, result in enumerate(results, 1):
        # Format relative time
        if result.timestamp:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            ts = result.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            days = (now - ts).days
            if days == 0:
                age_str = "today"
            elif days == 1:
                age_str = "yesterday"
            elif days < 7:
                age_str = f"{days}d ago"
            elif days < 30:
                age_str = f"{days // 7}w ago"
            elif days < 365:
                age_str = f"{days // 30}mo ago"
            else:
                age_str = f"{days // 365}y ago"
        else:
            age_str = "?"

        preview = result.intent_preview[:40] + "..." if len(result.intent_preview) > 40 else result.intent_preview
        user_display = "YOU" if result.is_own else f"@{result.user_name}"

        # Content indicators
        indicators = []
        if result.has_plan:
            indicators.append("📋")
        if result.has_agent_summaries:
            indicators.append("🤖")
        content_str = " ".join(indicators) if indicators else "-"

        table.add_row(
            str(i),
            f"{result.score:.0f}%",
            user_display,
            age_str,
            result.repo_name[:15],
            content_str,
            preview,
        )

    console.print(table)
    console.print("\n[dim]📋 = has plan file | 🤖 = has agent summaries[/dim]")
    console.print(f"[dim]Use 'jacked retrieve <id> --mode smart' for optimized context (default)[/dim]")
    console.print(f"[dim]Use 'jacked retrieve <id> --mode full' for complete transcript[/dim]")

    # Print session IDs for easy copy
    console.print("\nSession IDs:")
    for i, result in enumerate(results, 1):
        console.print(f"  {i}. {result.session_id}")


@main.command()
@click.argument("session_id")
@click.option("--output", "-o", type=click.Path(), help="Save output to file")
@click.option("--summary", "-s", is_flag=True, help="Show summary instead of content")
@click.option(
    "--mode", "-m",
    type=click.Choice(["smart", "plan", "labels", "agents", "full"]),
    default="smart",
    help="Retrieval mode (default: smart)"
)
@click.option("--max-tokens", "-t", default=15000, help="Max token budget for smart mode")
@click.option("--inject", "-i", is_flag=True, help="Format for context injection")
def retrieve(session_id: str, output: Optional[str], summary: bool, mode: str, max_tokens: int, inject: bool):
    """Retrieve a session's context with smart mode support.

    Requires: pip install "claude-jacked[search]"
    """
    if not _require_search("retrieve"):
        sys.exit(1)

    from jacked.retriever import SessionRetriever

    config = get_config()
    retriever = SessionRetriever(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Retrieving {session_id}...", total=None)

        session = retriever.retrieve(session_id, mode=mode)

        progress.remove_task(task)

    if not session:
        console.print(f"[red]Session {session_id} not found[/red]")
        sys.exit(1)

    # Show metadata with content summary
    tokens = session.content.estimate_tokens()
    content_parts = []
    if session.content.plan:
        content_parts.append(f"Plan: {tokens['plan']} tokens")
    if session.content.subagent_summaries:
        content_parts.append(f"Agent summaries: {len(session.content.subagent_summaries)} ({tokens['subagent_summaries']} tokens)")
    if session.content.summary_labels:
        content_parts.append(f"Labels: {len(session.content.summary_labels)} ({tokens['summary_labels']} tokens)")
    if session.content.user_messages:
        content_parts.append(f"User messages: {len(session.content.user_messages)} ({tokens['user_messages']} tokens)")
    if session.content.chunks:
        content_parts.append(f"Transcript chunks: {len(session.content.chunks)} ({tokens['chunks']} tokens)")

    console.print(Panel(
        f"Session: {session.session_id}\n"
        f"Repository: {session.repo_name}\n"
        f"Machine: {session.machine}\n"
        f"Age: {session.format_relative_time()}\n"
        f"Local: {'Yes' if session.is_local else 'No'}\n"
        f"\nContent available:\n  " + "\n  ".join(content_parts) +
        f"\n\nEstimated tokens (smart): {tokens['total']}",
        title="Session Info",
    ))

    if session.is_local:
        resume_cmd = retriever.get_resume_command(session)
        console.print(f"\n[green][OK] Session exists locally![/green]")
        console.print(f"To resume natively: [bold]{resume_cmd}[/bold]")

    if summary:
        text = retriever.get_summary(session)
    elif inject:
        text = retriever.format_for_injection(session, mode=mode, max_tokens=max_tokens)
    else:
        # Default: format based on mode
        text = retriever.format_for_injection(session, mode=mode, max_tokens=max_tokens)

    if output:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"\n[green]Saved to {output}[/green]")
    else:
        console.print(f"\n[bold]Content (mode={mode}):[/bold]")
        console.print(text)


@main.command(name="sessions")
@click.option("--repo", "-r", help="Filter by repository path")
@click.option("--limit", "-n", default=20, help="Maximum results")
def list_sessions(repo: Optional[str], limit: int):
    """List indexed sessions. Requires: pip install "claude-jacked[search]" """
    if not _require_search("sessions"):
        sys.exit(1)

    from jacked.client import QdrantSessionClient

    config = get_config()
    client = QdrantSessionClient(config)

    repo_id = get_repo_id(repo) if repo else None
    sessions = client.list_sessions(repo_id=repo_id, limit=limit)

    if not sessions:
        console.print("[yellow]No sessions found[/yellow]")
        return

    table = Table(title="Indexed Sessions", show_header=True)
    table.add_column("Session ID", style="cyan")
    table.add_column("Repository", style="magenta")
    table.add_column("Machine", style="green")
    table.add_column("Date", style="dim")
    table.add_column("Chunks", justify="right")

    for session in sessions:
        ts = session.get("timestamp", "")
        date_str = ts[:10] if ts else "?"
        table.add_row(
            session.get("session_id", "?")[:36],
            session.get("repo_name", "?"),
            session.get("machine", "?"),
            date_str,
            str(session.get("chunk_count", 0)),
        )

    console.print(table)


@main.command()
@click.argument("session_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def delete(session_id: str, yes: bool):
    """Delete a session from the index. Requires: pip install "claude-jacked[search]" """
    if not _require_search("delete"):
        sys.exit(1)

    from jacked.client import QdrantSessionClient

    config = get_config()
    client = QdrantSessionClient(config)

    if not yes:
        if not click.confirm(f"Delete session {session_id} from index?"):
            console.print("Cancelled")
            return

    client.delete_by_session(session_id)
    console.print(f"[green][OK][/green] Deleted session {session_id}")


@main.command()
def cleardb():
    """
    Delete ALL your indexed data from Qdrant.

    Requires: pip install "claude-jacked[search]"
    """
    if not _require_search("cleardb"):
        sys.exit(1)

    from jacked.client import QdrantSessionClient

    config = get_config()
    client = QdrantSessionClient(config)

    # Show what we're about to delete
    user_name = config.user_name
    count = client.count_by_user(user_name)

    if count == 0:
        console.print(f"[yellow]No data found for user '{user_name}'[/yellow]")
        return

    console.print(Panel(
        f"[bold red]WARNING: This will permanently delete ALL your indexed data![/bold red]\n\n"
        f"User: [cyan]{user_name}[/cyan]\n"
        f"Points to delete: [red]{count}[/red]\n\n"
        f"This only affects YOUR data. Teammates' data will be untouched.\n"
        f"After clearing, run 'jacked backfill' to re-index.",
        title="Clear Database",
    ))

    # Require typing confirmation phrase
    console.print("\n[bold]To confirm, type: DELETE MY DATA[/bold]")
    confirmation = click.prompt("Confirmation", default="", show_default=False)

    if confirmation != "DELETE MY DATA":
        console.print("[yellow]Cancelled - confirmation did not match[/yellow]")
        return

    # Do the delete
    deleted = client.delete_by_user(user_name)
    console.print(f"\n[green][OK][/green] Deleted {deleted} points for user '{user_name}'")
    console.print("\n[dim]Run 'jacked backfill' to re-index your sessions[/dim]")


@main.command()
def status():
    """Show indexing health and Qdrant connectivity. Requires: pip install "claude-jacked[search]" """
    if not _require_search("status"):
        sys.exit(1)

    from jacked.client import QdrantSessionClient

    config = get_config()

    console.print(Panel(
        f"Endpoint: {config.qdrant_endpoint}\n"
        f"Collection: {config.collection_name}\n"
        f"Projects Dir: {config.claude_projects_dir}\n"
        f"Machine: {config.machine_name}",
        title="Configuration",
    ))

    # Check Qdrant connectivity
    client = QdrantSessionClient(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Checking Qdrant...", total=None)

        info = client.get_collection_info()

        progress.remove_task(task)

    if info:
        console.print(Panel(
            f"Status: [green]{info['status']}[/green]\n"
            f"Points: {info['points_count']}\n"
            f"Segments: {info['segments_count']}\n"
            f"Indexed Vectors: {info['indexed_vectors_count']}",
            title="Qdrant Collection",
        ))
    else:
        console.print(Panel(
            "[red]Collection not found or Qdrant unreachable[/red]\n"
            "Run 'jacked backfill' to create collection and index sessions",
            title="Qdrant Status",
        ))


@main.command()
@click.option("--show", "-s", is_flag=True, help="Show current configuration")
def configure(show: bool):
    """Show configuration help or current settings."""
    import os

    if show:
        # Show current config
        console.print("[bold]Current Configuration[/bold]\n")
        try:
            config = get_config()
            console.print(Panel(
                f"User: [cyan]{config.user_name}[/cyan]\n"
                f"Machine: {config.machine_name}\n"
                f"Qdrant Endpoint: {config.qdrant_endpoint[:50]}...\n"
                f"Collection: {config.collection_name}\n"
                f"Projects Dir: {config.claude_projects_dir}\n"
                f"\n[bold]Ranking Weights:[/bold]\n"
                f"  Teammate weight: {config.teammate_weight}\n"
                f"  Other repo weight: {config.other_repo_weight}\n"
                f"  Time decay half-life: {config.time_decay_halflife_weeks} weeks",
                title="Active Config",
            ))
        except Exception as e:
            console.print(f"[red]Error loading config:[/red] {e}")
        return

    console.print("[bold]Jacked Configuration[/bold]\n")

    console.print("[bold cyan]Required:[/bold cyan]\n")
    console.print("  QDRANT_CLAUDE_SESSIONS_ENDPOINT")
    console.print("    Your Qdrant Cloud endpoint URL\n")
    console.print("  QDRANT_CLAUDE_SESSIONS_API_KEY")
    console.print("    Your Qdrant Cloud API key\n")

    console.print("[bold cyan]Team/Identity (Optional):[/bold cyan]\n")
    console.print("  JACKED_USER_NAME")
    console.print(f"    Your name for session attribution (default: git user.name or system user)")
    console.print(f"    Current: {os.getenv('JACKED_USER_NAME', SmartForkConfig._default_user_name())}\n")

    console.print("[bold cyan]Ranking Weights (Optional):[/bold cyan]\n")
    console.print("  JACKED_TEAMMATE_WEIGHT")
    console.print("    Multiplier for teammate sessions vs yours (default: 0.8)\n")
    console.print("  JACKED_OTHER_REPO_WEIGHT")
    console.print("    Multiplier for other repos vs current (default: 0.7)\n")
    console.print("  JACKED_TIME_DECAY_HALFLIFE_WEEKS")
    console.print("    Weeks until session relevance halves (default: 35)\n")

    console.print("[bold]Example shell profile setup:[/bold]\n")
    console.print('  # Required')
    console.print('  export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"')
    console.print('  export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"')
    console.print('')
    console.print('  # Team setup (optional)')
    console.print('  export JACKED_USER_NAME="yourname"')
    console.print('')
    console.print("[dim]Run 'jacked configure --show' to see current values[/dim]")




def _get_data_root() -> Path:
    """Find the data root directory for skills/agents/commands.

    Data is now inside the package at jacked/data/.
    """
    return Path(__file__).parent / "data"


def _sound_hook_marker() -> str:
    """Marker to identify jacked sound hooks."""
    return "# jacked-sound: "


def _get_sound_command(hook_type: str) -> str:
    """Generate cross-platform sound command (backgrounded, with fallbacks).

    Args:
        hook_type: 'notification' or 'complete'
    """
    if hook_type == "notification":
        win_sound = "Exclamation"
        mac_sound = "Basso.aiff"
        linux_sound = "dialog-warning.oga"
    else:  # complete
        win_sound = "Asterisk"
        mac_sound = "Glass.aiff"
        linux_sound = "complete.oga"

    # Use uname for detection, background with &, fallback to bell
    return (
        '('
        'OS=$(uname -s); '
        'case "$OS" in '
        f'Darwin) afplay /System/Library/Sounds/{mac_sound} 2>/dev/null || printf "\\a";; '
        'Linux) '
        '  if grep -qi microsoft /proc/version 2>/dev/null; then '
        f'    powershell.exe -Command "[System.Media.SystemSounds]::{win_sound}.Play()" 2>/dev/null || printf "\\a"; '
        '  else '
        f'    paplay /usr/share/sounds/freedesktop/stereo/{linux_sound} 2>/dev/null || printf "\\a"; '
        '  fi;; '
        f'MINGW*|MSYS*|CYGWIN*) powershell -Command "[System.Media.SystemSounds]::{win_sound}.Play()" 2>/dev/null || printf "\\a";; '
        '*) printf "\\a";; '
        'esac'
        ') &'
    )


def _install_sound_hooks(existing: dict, settings_path: Path):
    """Install sound notification hooks."""
    import json

    marker = _sound_hook_marker()

    # Notification hook
    if "Notification" not in existing["hooks"]:
        existing["hooks"]["Notification"] = []

    notif_exists = any(marker in str(h) for h in existing["hooks"]["Notification"])
    if not notif_exists:
        existing["hooks"]["Notification"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": marker + _get_sound_command("notification")}]
        })
        console.print("[green][OK][/green] Added Notification sound hook")
    else:
        console.print("[yellow][-][/yellow] Notification sound hook exists")

    # Stop sound hook (separate from index)
    stop_exists = any(marker in str(h) for h in existing["hooks"]["Stop"])
    if not stop_exists:
        existing["hooks"]["Stop"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": marker + _get_sound_command("complete")}]
        })
        console.print("[green][OK][/green] Added Stop sound hook")
    else:
        console.print("[yellow][-][/yellow] Stop sound hook exists")

    settings_path.write_text(json.dumps(existing, indent=2))


def _remove_sound_hooks(settings_path: Path) -> bool:
    """Remove jacked sound hooks. Returns True if any removed."""
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text())
    marker = _sound_hook_marker()
    modified = False

    for hook_type in ["Notification", "Stop"]:
        if hook_type in settings.get("hooks", {}):
            before = len(settings["hooks"][hook_type])
            settings["hooks"][hook_type] = [
                h for h in settings["hooks"][hook_type]
                if marker not in str(h)
            ]
            if len(settings["hooks"][hook_type]) < before:
                console.print(f"[green][OK][/green] Removed {hook_type} sound hook")
                modified = True

    if modified:
        settings_path.write_text(json.dumps(settings, indent=2))
    return modified


def _get_behavioral_rules() -> str:
    """Load behavioral rules from data file."""
    rules_path = _get_data_root() / "rules" / "jacked_behaviors.md"
    if not rules_path.exists():
        raise FileNotFoundError(f"Behavioral rules not found: {rules_path}")
    return rules_path.read_text(encoding="utf-8").strip()


def _behavioral_rules_marker() -> str:
    """Start marker for jacked behavioral rules block."""
    return "# jacked-behaviors-v2"


def _behavioral_rules_end_marker() -> str:
    """End marker for jacked behavioral rules block."""
    return "# end-jacked-behaviors"


def _install_behavioral_rules(claude_md_path: Path, force: bool = False):
    """Install behavioral rules into CLAUDE.md with marker boundaries.

    - Show rules before writing, require confirmation
    - Backup file before first modification
    - Atomic write (build in memory, write once)
    - Skip if already installed with same version
    """
    import shutil

    try:
        rules_text = _get_behavioral_rules()
    except FileNotFoundError as e:
        console.print(f"[red][FAIL][/red] {e}")
        console.print("[yellow]Skipping behavioral rules installation[/yellow]")
        return

    start_marker = _behavioral_rules_marker()
    end_marker = _behavioral_rules_end_marker()

    # Read existing content
    existing_content = ""
    if claude_md_path.exists():
        existing_content = claude_md_path.read_text(encoding="utf-8")

    # Check if already installed (any version)
    marker_prefix = "# jacked-behaviors-v"
    has_start = marker_prefix in existing_content
    has_end = end_marker in existing_content

    # Orphaned marker detection: start without end (or end without start)
    if has_start != has_end:
        which = "start" if has_start else "end"
        missing = "end" if has_start else "start"
        console.print(f"[red][FAIL][/red] Found {which} marker but no {missing} marker in CLAUDE.md")
        console.print("Your CLAUDE.md has a corrupted jacked rules block. Please fix it manually:")
        console.print(f"  Start marker: {start_marker}")
        console.print(f"  End marker: {end_marker}")
        return

    has_existing = has_start and has_end
    if has_existing:
        # Extract existing block (find the versioned start marker)
        start_idx = existing_content.index(marker_prefix)
        end_idx = existing_content.index(end_marker) + len(end_marker)
        existing_block = existing_content[start_idx:end_idx].strip()

        if existing_block == rules_text:
            console.print("[yellow][-][/yellow] Behavioral rules already configured correctly")
            return
        else:
            # Version upgrade needed
            console.print("\n[bold]Behavioral rules update available:[/bold]")
            console.print(f"[dim]{rules_text}[/dim]")
            if not force and sys.stdin.isatty() and not click.confirm("Update behavioral rules in CLAUDE.md?"):
                console.print("[yellow][-][/yellow] Skipped behavioral rules update")
                return

            # Backup before modifying
            backup_path = claude_md_path.with_suffix(".md.pre-jacked")
            if not backup_path.exists():
                shutil.copy2(claude_md_path, backup_path)
                console.print(f"[dim]Backup: {backup_path}[/dim]")

            # Replace the block (symmetric with _remove_behavioral_rules)
            before = existing_content[:start_idx].rstrip("\n")
            after = existing_content[end_idx:].lstrip("\n")
            if before and after:
                new_content = before + "\n\n" + rules_text + "\n\n" + after
            elif before:
                new_content = before + "\n\n" + rules_text + "\n"
            else:
                new_content = rules_text + "\n" + after if after else rules_text + "\n"
            try:
                claude_md_path.write_text(new_content, encoding="utf-8")
            except PermissionError:
                console.print(f"[red][FAIL][/red] Permission denied writing to {claude_md_path}")
                console.print("Check file permissions and try again.")
                return
            console.print("[green][OK][/green] Updated behavioral rules to latest version")
            return

    # Fresh install - show and confirm
    console.print("\n[bold]Proposed behavioral rules for ~/.claude/CLAUDE.md:[/bold]")
    console.print(f"[dim]{rules_text}[/dim]")
    if not force and sys.stdin.isatty() and not click.confirm("Add these behavioral rules to your global CLAUDE.md?"):
        console.print("[yellow][-][/yellow] Skipped behavioral rules")
        return

    # Backup before modifying (if file exists and no backup yet)
    if claude_md_path.exists():
        backup_path = claude_md_path.with_suffix(".md.pre-jacked")
        if not backup_path.exists():
            shutil.copy2(claude_md_path, backup_path)
            console.print(f"[dim]Backup: {backup_path}[/dim]")

    # Ensure parent directory exists
    claude_md_path.parent.mkdir(parents=True, exist_ok=True)

    # Build new content atomically
    if existing_content and not existing_content.endswith("\n\n"):
        if existing_content.endswith("\n"):
            new_content = existing_content + "\n" + rules_text + "\n"
        else:
            new_content = existing_content + "\n\n" + rules_text + "\n"
    else:
        new_content = existing_content + rules_text + "\n"

    try:
        claude_md_path.write_text(new_content, encoding="utf-8")
    except PermissionError:
        console.print(f"[red][FAIL][/red] Permission denied writing to {claude_md_path}")
        console.print("Check file permissions and try again.")
        return
    console.print("[green][OK][/green] Installed behavioral rules in CLAUDE.md")


def _remove_behavioral_rules(claude_md_path: Path) -> bool:
    """Remove jacked behavioral rules block from CLAUDE.md.

    Returns True if rules were found and removed.
    """
    if not claude_md_path.exists():
        return False

    content = claude_md_path.read_text(encoding="utf-8")
    marker_prefix = "# jacked-behaviors-v"
    end_marker = _behavioral_rules_end_marker()

    if marker_prefix not in content or end_marker not in content:
        return False

    start_idx = content.index(marker_prefix)
    end_idx = content.index(end_marker) + len(end_marker)

    # Strip the block and any extra blank lines around it
    before = content[:start_idx].rstrip("\n")
    after = content[end_idx:].lstrip("\n")

    if before and after:
        new_content = before + "\n\n" + after
    elif before:
        new_content = before + "\n"
    else:
        new_content = after

    try:
        claude_md_path.write_text(new_content, encoding="utf-8")
    except PermissionError:
        console.print(f"[red][FAIL][/red] Permission denied writing to {claude_md_path}")
        return False
    return True


def _security_hook_marker() -> str:
    """Marker to identify jacked security gatekeeper hooks."""
    return "# jacked-security"



def _install_security_hook(existing: dict, settings_path: Path):
    """Install security gatekeeper command hook for Bash PreToolUse events.

    Uses a PreToolUse command hook (blocking) that calls a Python script.
    The script evaluates commands via local rules, Anthropic API, or claude -p
    and returns permissionDecision:"allow" to auto-approve safe commands.

    Handles fresh install, version upgrades, and migration from PermissionRequest.
    """
    import json
    import shutil

    marker = _security_hook_marker()
    script_path = _get_data_root() / "hooks" / "security_gatekeeper.py"

    if not script_path.exists():
        console.print(f"[red][FAIL][/red] Security gatekeeper script not found: {script_path}")
        console.print("[yellow]Skipping security gatekeeper installation[/yellow]")
        return

    # Find python executable — prefer the one running this process
    python_exe = sys.executable
    if not python_exe or not Path(python_exe).exists():
        python_exe = shutil.which("python3") or shutil.which("python") or "python"

    # Use forward slashes for the command (works on Windows too)
    python_path = str(Path(python_exe)).replace("\\", "/")
    script_str = str(script_path).replace("\\", "/")
    command_str = f"{python_path} {script_str}"

    # Migrate: remove old PermissionRequest hooks with our marker
    if "PermissionRequest" in existing.get("hooks", {}):
        old_hooks = existing["hooks"]["PermissionRequest"]
        before = len(old_hooks)
        existing["hooks"]["PermissionRequest"] = [
            h for h in old_hooks
            if marker not in str(h) and "security_gatekeeper" not in str(h)
        ]
        if len(existing["hooks"]["PermissionRequest"]) < before:
            console.print("[green][OK][/green] Migrated security hook from PermissionRequest to PreToolUse")

    if "PreToolUse" not in existing["hooks"]:
        existing["hooks"]["PreToolUse"] = []

    # Check if already installed and whether it needs upgrading
    hook_index = None
    needs_upgrade = False
    for i, hook_entry in enumerate(existing["hooks"]["PreToolUse"]):
        hook_str = str(hook_entry)
        if marker in hook_str or "security_gatekeeper" in hook_str:
            hook_index = i
            for h in hook_entry.get("hooks", []):
                installed_cmd = h.get("command", "")
                if installed_cmd != command_str:
                    needs_upgrade = True
            break

    if hook_index is not None and not needs_upgrade:
        console.print("[yellow][-][/yellow] Security gatekeeper hook already configured")
        return

    hook_entry = {
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": command_str,
            "timeout": 30,
        }]
    }

    if hook_index is not None and needs_upgrade:
        existing["hooks"]["PreToolUse"][hook_index] = hook_entry
        settings_path.write_text(json.dumps(existing, indent=2))
        console.print("[green][OK][/green] Updated security gatekeeper to latest version")
    else:
        existing["hooks"]["PreToolUse"].append(hook_entry)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(existing, indent=2))
        console.print("[green][OK][/green] Installed security gatekeeper (PreToolUse, blocking)")

    # Create customizable prompt file if it doesn't exist
    from jacked.data.hooks import security_gatekeeper as gk
    prompt_path = Path.home() / ".claude" / "gatekeeper-prompt.txt"
    if not prompt_path.exists():
        prompt_path.write_text(gk.SECURITY_PROMPT, encoding="utf-8")
        console.print("[green][OK][/green] Created gatekeeper prompt: ~/.claude/gatekeeper-prompt.txt")
    else:
        try:
            if prompt_path.read_text(encoding="utf-8").strip() != gk.SECURITY_PROMPT.strip():
                console.print("[yellow][-][/yellow] Custom gatekeeper prompt detected (not overwriting)")
            else:
                console.print("[dim][-][/dim] Gatekeeper prompt unchanged")
        except Exception:
            console.print("[dim][-][/dim] Gatekeeper prompt unchanged")


def _remove_security_hook(settings_path: Path) -> bool:
    """Remove jacked security gatekeeper hook. Returns True if removed.

    Checks both PreToolUse (current) and PermissionRequest (legacy).
    """
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text())
    marker = _security_hook_marker()
    modified = False

    for hook_type in ["PreToolUse", "PermissionRequest"]:
        if hook_type not in settings.get("hooks", {}):
            continue
        before = len(settings["hooks"][hook_type])
        settings["hooks"][hook_type] = [
            h for h in settings["hooks"][hook_type]
            if marker not in str(h)
        ]
        if len(settings["hooks"][hook_type]) < before:
            modified = True

    if modified:
        settings_path.write_text(json.dumps(settings, indent=2))
        console.print("[green][OK][/green] Removed security gatekeeper hook")
        return True

    return False


@main.command()
@click.option("--sounds", is_flag=True, help="Install sound notification hooks")
@click.option("--search", is_flag=True, help="Install session indexing hook (requires [search] extra)")
@click.option("--security", is_flag=True, help="Install security gatekeeper hook (requires [security] extra)")
@click.option("--no-rules", is_flag=True, help="Skip behavioral rules in CLAUDE.md")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing agents/commands without prompting")
def install(sounds: bool, search: bool, security: bool, no_rules: bool, force: bool):
    """Auto-install skill, agents, commands, and optional hooks.

    Base install: agents, commands, behavioral rules, /jacked skill.
    Use --search to add session indexing (requires qdrant-client).
    Use --security to add security gatekeeper (requires anthropic SDK).
    """
    import os
    import json
    import shutil

    home = Path.home()
    pkg_root = _get_data_root()

    # Auto-detect extras: if the package is installed, enable by default
    has_qdrant = False
    try:
        import qdrant_client  # noqa: F401
        has_qdrant = True
    except ImportError:
        pass

    install_search = search or has_qdrant
    install_security = security

    console.print("[bold]Installing Jacked...[/bold]\n")

    # Check for existing settings
    settings_path = home / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    else:
        existing = {}

    if "hooks" not in existing:
        existing["hooks"] = {}
    if "Stop" not in existing["hooks"]:
        existing["hooks"]["Stop"] = []

    # Stop hook for session indexing — only if search extra available
    if install_search:
        hook_config_stop = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": 'jacked index --repo "$CLAUDE_PROJECT_DIR"',
                    "async": True
                }
            ]
        }

        hook_index = None
        needs_async_update = False
        for i, hook_entry in enumerate(existing["hooks"]["Stop"]):
            for h in hook_entry.get("hooks", []):
                if "jacked" in h.get("command", ""):
                    hook_index = i
                    if not h.get("async"):
                        needs_async_update = True
                    break

        if hook_index is None:
            existing["hooks"]["Stop"].append(hook_config_stop)
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(existing, indent=2))
            console.print(f"[green][OK][/green] Added Stop hook (session indexing)")
        elif needs_async_update:
            existing["hooks"]["Stop"][hook_index] = hook_config_stop
            settings_path.write_text(json.dumps(existing, indent=2))
            console.print(f"[green][OK][/green] Updated Stop hook with async: true")
        else:
            console.print(f"[yellow][-][/yellow] Stop hook already configured")
    else:
        console.print("[dim][-][/dim] Skipping session indexing hook (install \[search] extra to enable)")

    # Copy skill file with Python path templating
    # Claude Code expects skills in subdirectories with SKILL.md
    skill_dir = home / ".claude" / "skills" / "jacked"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_src = pkg_root / "skills" / "jacked" / "SKILL.md"
    skill_dst = skill_dir / "SKILL.md"

    if skill_src.exists():
        shutil.copy(skill_src, skill_dst)
        console.print(f"[green][OK][/green] Installed skill: /jacked")
    else:
        console.print(f"[yellow][-][/yellow] Skill file not found at {skill_src}")

    # Copy agents (with conflict detection)
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    if agents_src.exists():
        agents_dst.mkdir(parents=True, exist_ok=True)
        agent_count = 0
        skipped = 0
        for agent_file in agents_src.glob("*.md"):
            dst_file = agents_dst / agent_file.name
            src_content = agent_file.read_text(encoding="utf-8")
            if dst_file.exists():
                dst_content = dst_file.read_text(encoding="utf-8")
                if src_content == dst_content:
                    skipped += 1
                    continue  # Same content, skip silently
                # Different content - ask before overwriting (unless --force)
                if not force and sys.stdin.isatty() and not click.confirm(f"Agent '{agent_file.name}' exists with different content. Overwrite?"):
                    console.print(f"[yellow][-][/yellow] Skipped {agent_file.name}")
                    continue
            shutil.copy(agent_file, dst_file)
            agent_count += 1
        msg = f"[green][OK][/green] Installed {agent_count} agents"
        if skipped:
            msg += f" ({skipped} unchanged)"
        console.print(msg)
    else:
        console.print(f"[yellow][-][/yellow] Agents directory not found")

    # Copy commands (with conflict detection)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    if commands_src.exists():
        commands_dst.mkdir(parents=True, exist_ok=True)
        cmd_count = 0
        skipped = 0
        for cmd_file in commands_src.glob("*.md"):
            dst_file = commands_dst / cmd_file.name
            src_content = cmd_file.read_text(encoding="utf-8")
            if dst_file.exists():
                dst_content = dst_file.read_text(encoding="utf-8")
                if src_content == dst_content:
                    skipped += 1
                    continue  # Same content, skip silently
                # Different content - ask before overwriting (unless --force)
                if not force and sys.stdin.isatty() and not click.confirm(f"Command '{cmd_file.name}' exists with different content. Overwrite?"):
                    console.print(f"[yellow][-][/yellow] Skipped {cmd_file.name}")
                    continue
            shutil.copy(cmd_file, dst_file)
            cmd_count += 1
        msg = f"[green][OK][/green] Installed {cmd_count} commands"
        if skipped:
            msg += f" ({skipped} unchanged)"
        console.print(msg)
    else:
        console.print(f"[yellow][-][/yellow] Commands directory not found")

    # Install sound hooks if requested
    if sounds:
        _install_sound_hooks(existing, settings_path)

    # Install security gatekeeper — only if --security flag passed
    if install_security:
        _install_security_hook(existing, settings_path)
        # Auto-run static permission audit
        console.print("")
        audit_results = _scan_permission_rules()
        if audit_results:
            warns = [r for r in audit_results if r[1] == "WARN"]
            if warns:
                console.print(f"[yellow][AUDIT] Found {len(warns)} dangerous permission wildcard(s):[/yellow]")
                for pat, _, prefix, reason in warns:
                    console.print(f"  [red][WARN][/red] {pat} — {reason}")
                console.print(f"[dim]Run 'jacked gatekeeper audit' for full details[/dim]")
            else:
                console.print("[green][AUDIT] Permission rules look clean[/green]")
    else:
        console.print("[dim][-][/dim] Skipping security gatekeeper (use --security to enable)")

    # Install behavioral rules in CLAUDE.md (default on, --no-rules to skip)
    if not no_rules:
        claude_md_path = home / ".claude" / "CLAUDE.md"
        _install_behavioral_rules(claude_md_path, force=force)

    console.print("\n[bold]Installation complete![/bold]")
    console.print("\n[yellow]IMPORTANT: Restart Claude Code for new commands to take effect![/yellow]")
    console.print("\nWhat you get:")
    console.print("  - /jacked - Search past Claude sessions")
    console.print("  - /dc - Double-check reviewer")
    console.print("  - /pr - PR workflow helper")
    console.print("  - /learn - Distill lessons into CLAUDE.md rules")
    console.print("  - /techdebt - Project tech debt audit")
    console.print("  - /redo - Scrap and re-implement with hindsight")
    console.print("  - /audit-rules - CLAUDE.md quality audit")
    console.print("  - 10 specialized agents (readme, wiki, tests, etc.)")
    if install_search:
        console.print("  - Session indexing hook (auto-indexes after each response)")
    if install_security:
        console.print("  - Security gatekeeper (auto-approves safe Bash commands)")
    if not no_rules:
        console.print("  - Behavioral rules in CLAUDE.md")

    # Show next steps based on what's installed
    console.print("\nNext steps:")
    console.print("  1. Restart Claude Code (exit and run 'claude' again)")
    if install_search:
        console.print("  2. Set Qdrant credentials (run 'jacked configure' for help)")
        console.print("  3. Run 'jacked backfill' to index existing sessions")
        console.print("  4. Use '/jacked <description>' to search past sessions")
    else:
        console.print("\nOptional extras:")
        console.print('  pip install "claude-jacked\[search]"    # Session search via Qdrant')
        console.print('  pip install "claude-jacked\[security]"  # Auto-approve safe Bash commands')
        console.print('  pip install "claude-jacked\[all]"       # Everything')


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--sounds", is_flag=True, help="Remove only sound hooks")
@click.option("--security", is_flag=True, help="Remove only security gatekeeper hook")
@click.option("--rules", is_flag=True, help="Remove only behavioral rules from CLAUDE.md")
def uninstall(yes: bool, sounds: bool, security: bool, rules: bool):
    """Remove jacked hooks, skill, agents, and commands from Claude Code."""
    import json
    import shutil

    home = Path.home()
    pkg_root = _get_data_root()
    settings_path = home / ".claude" / "settings.json"

    # If --sounds flag, only remove sound hooks
    if sounds:
        if _remove_sound_hooks(settings_path):
            console.print("[bold]Sound hooks removed![/bold]")
        else:
            console.print("[yellow]No sound hooks found[/yellow]")
        return

    # If --security flag, only remove security hook
    if security:
        if _remove_security_hook(settings_path):
            console.print("[bold]Security gatekeeper removed![/bold]")
        else:
            console.print("[yellow]No security gatekeeper hook found[/yellow]")
        return

    # If --rules flag, only remove behavioral rules
    if rules:
        claude_md_path = home / ".claude" / "CLAUDE.md"
        if _remove_behavioral_rules(claude_md_path):
            console.print("[bold]Behavioral rules removed from CLAUDE.md![/bold]")
        else:
            console.print("[yellow]No behavioral rules found in CLAUDE.md[/yellow]")
        return

    if not yes:
        if not click.confirm("Remove jacked from Claude Code? (This won't delete your Qdrant index)"):
            console.print("Cancelled")
            return

    console.print("[bold]Uninstalling Jacked...[/bold]\n")

    # Also remove sound, security hooks, and behavioral rules during full uninstall
    _remove_sound_hooks(settings_path)
    _remove_security_hook(settings_path)
    claude_md_path = home / ".claude" / "CLAUDE.md"
    if _remove_behavioral_rules(claude_md_path):
        console.print("[green][OK][/green] Removed behavioral rules from CLAUDE.md")

    # Remove Stop hook from settings.json
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            if "hooks" in settings and "Stop" in settings["hooks"]:
                # Filter out jacked hooks
                original_count = len(settings["hooks"]["Stop"])
                settings["hooks"]["Stop"] = [
                    h for h in settings["hooks"]["Stop"]
                    if "jacked" not in str(h.get("hooks", []))
                ]
                removed_count = original_count - len(settings["hooks"]["Stop"])
                if removed_count > 0:
                    settings_path.write_text(json.dumps(settings, indent=2))
                    console.print(f"[green][OK][/green] Removed Stop hook from {settings_path}")
                else:
                    console.print(f"[yellow][-][/yellow] No jacked hook found in settings")
        except (json.JSONDecodeError, KeyError) as e:
            console.print(f"[red][FAIL][/red] Error reading settings: {e}")
    else:
        console.print(f"[yellow][-][/yellow] No settings.json found")

    # Remove skill directory
    skill_dir = home / ".claude" / "skills" / "jacked"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        console.print(f"[green][OK][/green] Removed skill: /jacked")
    else:
        console.print(f"[yellow][-][/yellow] Skill not found")

    # Remove only jacked-installed agents (not the whole directory!)
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    if agents_src.exists() and agents_dst.exists():
        agent_count = 0
        for agent_file in agents_src.glob("*.md"):
            dst_file = agents_dst / agent_file.name
            if dst_file.exists():
                dst_file.unlink()
                agent_count += 1
        if agent_count > 0:
            console.print(f"[green][OK][/green] Removed {agent_count} agents")
        else:
            console.print(f"[yellow][-][/yellow] No jacked agents found")
    else:
        console.print(f"[yellow][-][/yellow] Agents directory not found")

    # Remove only jacked-installed commands (not the whole directory!)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    if commands_src.exists() and commands_dst.exists():
        cmd_count = 0
        for cmd_file in commands_src.glob("*.md"):
            dst_file = commands_dst / cmd_file.name
            if dst_file.exists():
                dst_file.unlink()
                cmd_count += 1
        if cmd_count > 0:
            console.print(f"[green][OK][/green] Removed {cmd_count} commands")
        else:
            console.print(f"[yellow][-][/yellow] No jacked commands found")
    else:
        console.print(f"[yellow][-][/yellow] Commands directory not found")

    console.print("\n[bold]Uninstall complete![/bold]")
    console.print("\n[dim]Note: Your Qdrant index is still intact. Run 'pipx uninstall claude-jacked' to fully remove.[/dim]")


@main.group()
def gatekeeper():
    """View or customize the security gatekeeper LLM prompt."""
    pass


@gatekeeper.command(name="show")
def gatekeeper_show():
    """Print the current gatekeeper LLM prompt."""
    from jacked.data.hooks.security_gatekeeper import SECURITY_PROMPT, PROMPT_PATH

    if PROMPT_PATH.exists():
        try:
            prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
            console.print(f"[dim]Source: {PROMPT_PATH}[/dim]\n")
        except Exception:
            prompt = SECURITY_PROMPT
            console.print("[dim]Source: built-in (file read failed)[/dim]\n")
    else:
        prompt = SECURITY_PROMPT
        console.print("[dim]Source: built-in default[/dim]\n")

    console.print(prompt)


@gatekeeper.command(name="reset")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def gatekeeper_reset(yes: bool):
    """Reset gatekeeper prompt to built-in default."""
    from jacked.data.hooks.security_gatekeeper import SECURITY_PROMPT, PROMPT_PATH

    if not yes:
        if PROMPT_PATH.exists():
            try:
                current = PROMPT_PATH.read_text(encoding="utf-8").strip()
                if current == SECURITY_PROMPT.strip():
                    console.print("[yellow]Prompt is already the built-in default[/yellow]")
                    return
            except Exception:
                pass
        if not click.confirm("Reset gatekeeper prompt to built-in default?"):
            console.print("Cancelled")
            return

    PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_PATH.write_text(SECURITY_PROMPT, encoding="utf-8")
    console.print(f"[green][OK][/green] Reset gatekeeper prompt to built-in default")
    console.print(f"[dim]{PROMPT_PATH}[/dim]")


HIGH_RISK_PREFIXES = {
    "python": "arbitrary code execution via -c",
    "python3": "arbitrary code execution via -c",
    "python.exe": "arbitrary code execution via -c",
    "node": "arbitrary code execution via -e",
    "bash": "shell-in-shell, can run anything",
    "sh": "shell-in-shell, can run anything",
    "zsh": "shell-in-shell, can run anything",
    "cmd": "shell-in-shell, can run anything",
    "powershell": "can run encoded commands or scripts",
    "curl": "potential data exfiltration",
    "wget": "potential data exfiltration",
    "rm": "file deletion beyond deny pattern coverage",
    "del": "file deletion beyond deny pattern coverage",
    "ssh": "remote command execution",
    "scp": "file transfer to remote",
    "rsync": "file transfer to remote",
    "nc": "raw network connections",
    "ncat": "raw network connections",
    "netcat": "raw network connections",
}

MEDIUM_RISK_PREFIXES = {
    "cat": "deny patterns cover sensitive files, but not all",
}

# Prefixes that are always low-risk and get [OK]
LOW_RISK_PREFIXES = {
    "git", "gh", "grep", "rg", "find", "fd", "ls", "dir", "pwd",
    "echo", "which", "where", "env", "printenv", "npm", "pip",
    "pytest", "make", "cargo", "go", "docker", "jacked", "claude",
    "npx", "tsc", "ruff", "flake8", "pylint", "mypy", "eslint",
    "prettier", "black", "isort", "jest", "conda", "pipx",
}


def _extract_prefix_from_pattern(pattern: str) -> str:
    """Extract the command prefix from a Bash permission pattern.

    'Bash(git :*)' → 'git'
    'Bash(python:*)' → 'python'
    'Bash(gh pr list:*)' → 'gh'
    """
    inner = pattern[5:]  # strip 'Bash('
    if inner.endswith(")"):
        inner = inner[:-1]
    if inner.endswith(":*"):
        inner = inner[:-2]
    return inner.split()[0].strip()


def _classify_permission(pattern: str) -> tuple[str, str, str]:
    """Classify a permission pattern as high/medium/low risk.

    Returns (level, prefix, reason).
    level is 'WARN', 'INFO', or 'OK'.
    """
    inner = pattern[5:]
    if inner.endswith(")"):
        inner = inner[:-1]
    is_wildcard = inner.endswith(":*")

    prefix = _extract_prefix_from_pattern(pattern)

    if is_wildcard and prefix in HIGH_RISK_PREFIXES:
        return "WARN", prefix, HIGH_RISK_PREFIXES[prefix]
    if is_wildcard and prefix in MEDIUM_RISK_PREFIXES:
        return "INFO", prefix, MEDIUM_RISK_PREFIXES[prefix]
    if not is_wildcard:
        return "OK", prefix, "scoped (low risk)"
    if prefix in LOW_RISK_PREFIXES:
        return "OK", prefix, "read-only (low risk)"
    return "INFO", prefix, "unrecognized wildcard — review manually"


def _scan_permission_rules() -> list[tuple[str, str, str, str]]:
    """Scan all settings files for Bash permission rules.

    Returns list of (pattern, level, prefix, reason).
    """
    from jacked.data.hooks.security_gatekeeper import _load_permissions
    results = []
    seen = set()

    settings_files = [
        Path.home() / ".claude" / "settings.json",
        Path(".claude") / "settings.json",
        Path(".claude") / "settings.local.json",
    ]

    for settings_path in settings_files:
        patterns = _load_permissions(settings_path)
        for pat in patterns:
            if pat in seen:
                continue
            seen.add(pat)
            level, prefix, reason = _classify_permission(pat)
            results.append((pat, level, prefix, reason))

    return results


def _parse_log_for_perms_commands(log_path: Path, limit: int = 50) -> list[str]:
    """Parse hooks-debug.log for auto-approved PERMS MATCH commands.

    Finds PERMS MATCH lines and extracts the command from the preceding EVALUATING line.
    Returns up to `limit` commands (most recent first).
    """
    if not log_path.exists():
        return []

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    commands = []
    for i, line in enumerate(lines):
        if "PERMS MATCH" in line:
            # Look backwards for the EVALUATING line
            for j in range(i - 1, max(i - 5, -1), -1):
                if "EVALUATING:" in lines[j]:
                    # Extract command after "EVALUATING: "
                    idx = lines[j].index("EVALUATING:") + len("EVALUATING:")
                    cmd = lines[j][idx:].strip()
                    commands.append(cmd)
                    break

    # Return most recent N
    return commands[-limit:]


@gatekeeper.command(name="audit")
@click.option("--log", "scan_log", is_flag=True, help="Also scan recent auto-approved commands via LLM")
@click.option("--limit", "-n", default=50, help="Number of recent log entries to scan")
def gatekeeper_audit(scan_log, limit):
    """Audit permission rules for dangerous wildcards."""
    import os
    import json
    from jacked.data.hooks.security_gatekeeper import LOG_PATH, STATE_PATH

    console.print("[bold]Scanning permission rules...[/bold]\n")

    console.print("[dim]Sources:[/dim]")
    console.print("[dim]  ~/.claude/settings.json[/dim]")
    console.print("[dim]  .claude/settings.json[/dim]")
    console.print("[dim]  .claude/settings.local.json[/dim]\n")

    results = _scan_permission_rules()

    if not results:
        console.print("[yellow]No Bash permission rules found[/yellow]")
        console.print("[dim]Permission rules are set via Claude Code's /permissions command[/dim]")
        return

    warn_count = 0
    info_count = 0
    ok_count = 0

    for pat, level, prefix, reason in results:
        if level == "WARN":
            console.print(f"  [red][WARN][/red] {pat} — {reason}")
            console.print(f"         Gatekeeper deny patterns won't catch all {prefix} inline code.")
            console.print(f"         Consider removing and letting the gatekeeper evaluate individually.\n")
            warn_count += 1
        elif level == "INFO":
            console.print(f"  [yellow][INFO][/yellow] {pat} — {reason}")
            info_count += 1
        else:
            console.print(f"  [green][OK][/green] {pat} — {reason}")
            ok_count += 1

    console.print(f"\n{warn_count} warnings, {info_count} info, {ok_count} OK")

    if warn_count > 0:
        console.print(f"\n[yellow]TIP: Remove dangerous wildcards and let the gatekeeper LLM evaluate them individually.[/yellow]")

    # Log scanning
    if scan_log:
        console.print(f"\n[bold]Scanning last {limit} auto-approved commands from hooks-debug.log...[/bold]\n")

        commands = _parse_log_for_perms_commands(LOG_PATH, limit=limit)
        if not commands:
            console.print("[yellow]No PERMS MATCH entries found in log[/yellow]")
            console.print(f"[dim]Log path: {LOG_PATH}[/dim]")
            return

        # Send to LLM for evaluation
        cmd_list = "\n".join(f"  {i+1}. {cmd}" for i, cmd in enumerate(commands))
        audit_prompt = f"""You are a security auditor. Review these {len(commands)} Bash commands that were auto-approved via permission rules (bypassing LLM evaluation).

Flag any that look dangerous — data exfiltration, destructive operations, arbitrary code execution, secret access, etc. Most will be safe.

Commands:
{cmd_list}

Respond with ONLY a JSON object:
{{"flagged": [{{"index": 1, "command": "the command", "reason": "brief reason"}}], "safe_count": N}}

If all are safe, return: {{"flagged": [], "safe_count": {len(commands)}}}"""

        console.print(f"[dim]Sending {len(commands)} commands to LLM for review...[/dim]")

        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                console.print("[red]ANTHROPIC_API_KEY not set — cannot run LLM audit[/red]")
                console.print("[dim]Set ANTHROPIC_API_KEY or install anthropic SDK[/dim]")
                return

            client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": audit_prompt}],
            )
            text = response.content[0].text.strip()

            # Strip markdown fences
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            parsed = json.loads(text)
            flagged = parsed.get("flagged", [])
            safe_count = parsed.get("safe_count", len(commands) - len(flagged))

            if flagged:
                for item in flagged:
                    console.print(f"  [red][WARN][/red] {item.get('command', '?')}")
                    console.print(f"         LLM says: {item.get('reason', '?')}\n")
            console.print(f"{safe_count}/{len(commands)} commands look safe.")
            if flagged:
                console.print(f"[red]{len(flagged)} commands flagged[/red] — consider tightening your permission rules.")
            else:
                console.print("[green]No dangerous commands found.[/green]")

        except ImportError:
            console.print("[red]anthropic SDK not installed — cannot run LLM audit[/red]")
            console.print('[dim]Install it: pip install "claude-jacked[security]"[/dim]')
        except json.JSONDecodeError:
            console.print(f"[yellow]LLM returned non-JSON response:[/yellow] {text[:200]}")
        except Exception as e:
            console.print(f"[red]LLM audit failed:[/red] {e}")

    # Show counter info
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            count = state.get("perms_count", 0)
            if count > 0:
                console.print(f"\n[dim]Total permission auto-approvals since last reset: {count}[/dim]")
        except Exception:
            pass


@gatekeeper.command(name="diff")
def gatekeeper_diff():
    """Show diff between custom prompt and built-in default."""
    import difflib
    from jacked.data.hooks.security_gatekeeper import SECURITY_PROMPT, PROMPT_PATH

    if not PROMPT_PATH.exists():
        console.print("[yellow]No custom prompt file found — using built-in default[/yellow]")
        console.print(f"[dim]Create one at: {PROMPT_PATH}[/dim]")
        return

    try:
        custom = PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        console.print(f"[red]Error reading prompt file:[/red] {e}")
        return

    if custom.strip() == SECURITY_PROMPT.strip():
        console.print("[green]No differences — custom prompt matches built-in default[/green]")
        return

    diff = difflib.unified_diff(
        SECURITY_PROMPT.splitlines(keepends=True),
        custom.splitlines(keepends=True),
        fromfile="built-in",
        tofile=str(PROMPT_PATH),
    )
    for line in diff:
        if line.startswith('+') and not line.startswith('+++'):
            console.print(f"[green]{line.rstrip()}[/green]")
        elif line.startswith('-') and not line.startswith('---'):
            console.print(f"[red]{line.rstrip()}[/red]")
        else:
            console.print(line.rstrip())


if __name__ == "__main__":
    main()
