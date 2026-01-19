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
            f"{result['intent_chunks']} intent chunks, "
            f"{result['transcript_chunks']} transcript chunks"
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
def search(query: str, repo: Optional[str], limit: int, mine: bool, user: Optional[str]):
    """Search for sessions by semantic similarity with multi-factor ranking."""
    import os
    from jacked.searcher import SessionSearcher

    config = get_config()
    searcher = SessionSearcher(config)

    # Use current repo if not specified
    current_repo = repo or os.getenv("CLAUDE_PROJECT_DIR")

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
        )

        progress.remove_task(task)

    if not results:
        console.print("[yellow]No matching sessions found[/yellow]")
        return

    table = Table(title="Search Results", show_header=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Score", style="cyan", width=6)
    table.add_column("User", style="yellow", width=12)
    table.add_column("Date", style="green", width=12)
    table.add_column("Repository", style="magenta")
    table.add_column("Preview")

    for i, result in enumerate(results, 1):
        date_str = result.timestamp.strftime("%Y-%m-%d") if result.timestamp else "?"
        preview = result.intent_preview[:50] + "..." if len(result.intent_preview) > 50 else result.intent_preview
        user_display = "YOU" if result.is_own else f"@{result.user_name}"
        table.add_row(
            str(i),
            f"{result.score:.0f}%",
            user_display,
            date_str,
            result.repo_name,
            preview,
        )

    console.print(table)
    console.print(f"\n[dim]Use 'jacked retrieve <session_id>' to get full transcript[/dim]")
    console.print(f"[dim]Use 'jacked retrieve <id1> <id2> ...' to get multiple transcripts[/dim]")

    # Print session IDs for easy copy
    console.print("\nSession IDs:")
    for i, result in enumerate(results, 1):
        console.print(f"  {i}. {result.session_id}")


@main.command()
@click.argument("session_id")
@click.option("--output", "-o", type=click.Path(), help="Save transcript to file")
@click.option("--summary", "-s", is_flag=True, help="Show summary instead of full transcript")
def retrieve(session_id: str, output: Optional[str], summary: bool):
    """Retrieve a session's full transcript."""
    from jacked.retriever import SessionRetriever

    config = get_config()
    retriever = SessionRetriever(config)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Retrieving {session_id}...", total=None)

        session = retriever.retrieve(session_id)

        progress.remove_task(task)

    if not session:
        console.print(f"[red]Session {session_id} not found[/red]")
        sys.exit(1)

    # Show metadata
    console.print(Panel(
        f"Session: {session.session_id}\n"
        f"Repository: {session.repo_name}\n"
        f"Machine: {session.machine}\n"
        f"Local: {'Yes' if session.is_local else 'No'}",
        title="Session Info",
    ))

    if session.is_local:
        resume_cmd = retriever.get_resume_command(session)
        console.print(f"\n[green][OK] Session exists locally![/green]")
        console.print(f"To resume natively: [bold]{resume_cmd}[/bold]")

    if summary:
        text = retriever.get_summary(session)
    else:
        text = session.full_transcript

    if output:
        Path(output).write_text(text, encoding="utf-8")
        console.print(f"\n[green]Saved to {output}[/green]")
    else:
        console.print(f"\n[bold]Transcript ({len(session.full_transcript)} chars):[/bold]")
        console.print(text)


@main.command()
@click.option("--repo", "-r", help="Filter by repository path")
@click.option("--limit", "-n", default=20, help="Maximum results")
def list(repo: Optional[str], limit: int):
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


@main.command()
def install():
    """Auto-install hook config, skill, agents, and commands."""
    import os
    import json
    import shutil

    home = Path.home()
    pkg_root = Path(__file__).parent.parent

    # Hook configuration
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

    # Copy skill file - Claude Code expects skills in subdirectories with SKILL.md
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


if __name__ == "__main__":
    main()
