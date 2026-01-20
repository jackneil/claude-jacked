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


def get_config() -> SmartForkConfig:
    """Load configuration from environment."""
    try:
        return SmartForkConfig.from_env()
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        console.print("\nSet these environment variables:")
        console.print("  QDRANT_CLAUDE_SESSIONS_ENDPOINT=<your-qdrant-url>")
        console.print("  QDRANT_CLAUDE_SESSIONS_API_KEY=<your-api-key>")
        sys.exit(1)


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
    """
    import os
    from jacked.indexer import SessionIndexer

    config = get_config()
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
    """Index all existing Claude sessions."""
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

    By default, searches plan, subagent_summary, summary_label, and user_message content.
    Use --type to filter to specific content types.
    """
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

    Modes:
      smart  - Plan + agent summaries + labels + user messages (default)
      plan   - Just the plan file
      labels - Just summary labels (tiny)
      agents - All subagent summaries
      full   - Everything including full transcript
    """
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
    """List indexed sessions."""
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
    """Delete a session from the index."""
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

    Only deletes YOUR data (matching your user_name), not teammates' data.
    Use this before re-indexing with a new schema or to start fresh.
    """
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
    """Show indexing health and Qdrant connectivity."""
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


# Import for configure command
from jacked.config import SmartForkConfig


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


@main.command()
@click.option("--sounds", is_flag=True, help="Install sound notification hooks")
def install(sounds: bool):
    """Auto-install hook config, skill, agents, and commands."""
    import os
    import json
    import shutil

    home = Path.home()
    pkg_root = _get_data_root()

    # Hook configuration - assumes jacked is on PATH (installed via pipx)
    hook_config = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'jacked index --repo "$CLAUDE_PROJECT_DIR"'
                        }
                    ]
                }
            ]
        }
    }

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

    # Merge hook config
    if "hooks" not in existing:
        existing["hooks"] = {}
    if "Stop" not in existing["hooks"]:
        existing["hooks"]["Stop"] = []

    # Check if hook already exists
    hook_exists = any(
        "jacked" in str(h.get("hooks", []))
        for h in existing["hooks"]["Stop"]
    )

    if not hook_exists:
        existing["hooks"]["Stop"].append(hook_config["hooks"]["Stop"][0])
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(existing, indent=2))
        console.print(f"[green][OK][/green] Added Stop hook to {settings_path}")
    else:
        console.print(f"[yellow][-][/yellow] Stop hook already exists in {settings_path}")

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

    # Copy agents
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    if agents_src.exists():
        agents_dst.mkdir(parents=True, exist_ok=True)
        agent_count = 0
        for agent_file in agents_src.glob("*.md"):
            shutil.copy(agent_file, agents_dst / agent_file.name)
            agent_count += 1
        console.print(f"[green][OK][/green] Installed {agent_count} agents")
    else:
        console.print(f"[yellow][-][/yellow] Agents directory not found")

    # Copy commands
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    if commands_src.exists():
        commands_dst.mkdir(parents=True, exist_ok=True)
        cmd_count = 0
        for cmd_file in commands_src.glob("*.md"):
            shutil.copy(cmd_file, commands_dst / cmd_file.name)
            cmd_count += 1
        console.print(f"[green][OK][/green] Installed {cmd_count} commands")
    else:
        console.print(f"[yellow][-][/yellow] Commands directory not found")

    # Install sound hooks if requested
    if sounds:
        _install_sound_hooks(existing, settings_path)

    console.print("\n[bold]Installation complete![/bold]")
    console.print("\n[yellow]IMPORTANT: Restart Claude Code for new commands to take effect![/yellow]")
    console.print("\nWhat you get:")
    console.print("  - /jacked - Search past Claude sessions")
    console.print("  - /dc - Double-check reviewer")
    console.print("  - /pr - PR workflow helper")
    console.print("  - 10 specialized agents (readme, wiki, tests, etc.)")
    console.print("\nNext steps:")
    console.print("  1. Restart Claude Code (exit and run 'claude' again)")
    console.print("  2. Set environment variables (run 'jacked configure' for help)")
    console.print("  3. Run 'jacked backfill' to index existing sessions")
    console.print("  4. Use '/jacked <description>' in Claude to search past sessions")


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--sounds", is_flag=True, help="Remove only sound hooks")
def uninstall(yes: bool, sounds: bool):
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

    if not yes:
        if not click.confirm("Remove jacked from Claude Code? (This won't delete your Qdrant index)"):
            console.print("Cancelled")
            return

    console.print("[bold]Uninstalling Jacked...[/bold]\n")

    # Also remove sound hooks during full uninstall
    _remove_sound_hooks(settings_path)

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


if __name__ == "__main__":
    main()
