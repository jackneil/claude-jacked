"""
CLI for Jacked.

Provides command-line interface for indexing, searching, and
retrieving Claude Code sessions.
"""

import os
import shutil
import subprocess
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
        console.print("\nInstall it with:")
        console.print(r'  [bold]uv tool install "claude-jacked\[search]" --force[/bold]')
        return False


DB_PATH = Path.home() / ".claude" / "jacked.db"
_VALID_TABLES = {
    "command_usage",
    "agent_invocations",
    "hook_executions",
    "version_checks",
}


def _log_to_db(table: str, **kwargs):
    """Fire-and-forget DB write. Never blocks, never crashes."""
    if table not in _VALID_TABLES:
        return
    import threading

    def _do_write():
        import sqlite3
        from datetime import datetime, timezone

        if not DB_PATH.exists():
            return
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=0.5)
            conn.execute("PRAGMA journal_mode=WAL")
            kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    try:
        t = threading.Thread(target=_do_write, daemon=True)
        t.start()
        t.join(timeout=0.1)
    except Exception:
        pass


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
    Requires: uv tool install "claude-jacked[search]"
    """
    import os
    import time

    _index_start = time.time()

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
            console.print("\nInstall it with:")
            console.print(r'  [bold]uv tool install "claude-jacked\[search]" --force[/bold]')
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
                console.print(
                    "[red]Error:[/red] --repo is required when indexing a file path"
                )
                sys.exit(1)
        else:
            # Assume it's a session ID, find the file
            if not repo:
                repo = os.getenv("CLAUDE_PROJECT_DIR")
            if not repo:
                console.print(
                    "[red]Error:[/red] --repo or CLAUDE_PROJECT_DIR is required"
                )
                sys.exit(1)

            from jacked.config import get_session_dir_for_repo

            session_dir = get_session_dir_for_repo(config.claude_projects_dir, repo)
            session_path = session_dir / f"{session}.jsonl"
            repo_path = repo

            if not session_path.exists():
                console.print(
                    f"[red]Error:[/red] Session file not found: {session_path}"
                )
                sys.exit(1)
    else:
        # Index current session
        session_id = os.getenv("CLAUDE_SESSION_ID")
        repo_path = os.getenv("CLAUDE_PROJECT_DIR")

        if not session_id or not repo_path:
            console.print(
                "[red]Error:[/red] CLAUDE_SESSION_ID and CLAUDE_PROJECT_DIR not set"
            )
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
        console.print(
            f"[yellow][-][/yellow] Session {result['session_id']} unchanged, skipped"
        )
    else:
        console.print(f"[red][FAIL][/red] Failed: {result.get('error')}")
        _log_to_db(
            "hook_executions",
            hook_type="Stop",
            hook_name="session_indexing",
            session_id=os.getenv("CLAUDE_SESSION_ID", ""),
            repo_path=os.getenv("CLAUDE_PROJECT_DIR", ""),
            success=False,
            duration_ms=(time.time() - _index_start) * 1000,
        )
        sys.exit(1)

    _log_to_db(
        "hook_executions",
        hook_type="Stop",
        hook_name="session_indexing",
        session_id=os.getenv("CLAUDE_SESSION_ID", ""),
        repo_path=os.getenv("CLAUDE_PROJECT_DIR", ""),
        success=result.get("indexed", False),
        duration_ms=(time.time() - _index_start) * 1000,
    )


@main.command()
@click.option("--repo", "-r", help="Filter by repository name pattern")
@click.option("--force", "-f", is_flag=True, help="Re-index all sessions")
def backfill(repo: Optional[str], force: bool):
    """Index all existing Claude sessions. Requires: uv tool install "claude-jacked[search]" """
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

    _log_to_db("command_usage", command_name="backfill")


@main.command()
@click.argument("query")
@click.option("--repo", "-r", help="Boost results from this repository path")
@click.option("--limit", "-n", default=5, help="Maximum results")
@click.option("--mine", "-m", is_flag=True, help="Only show my sessions")
@click.option("--user", "-u", help="Only show sessions from this user")
@click.option(
    "--type",
    "-t",
    "content_types",
    multiple=True,
    help="Filter by content type (plan, subagent_summary, summary_label, user_message, chunk)",
)
def search(
    query: str,
    repo: Optional[str],
    limit: int,
    mine: bool,
    user: Optional[str],
    content_types: tuple,
):
    """Search for sessions by semantic similarity with multi-factor ranking.

    Requires: uv tool install "claude-jacked[search]"
    """
    if not _require_search("search"):
        sys.exit(1)

    import os
    from jacked.searcher import SessionSearcher

    _log_to_db(
        "command_usage",
        command_name="search",
        repo_path=os.getenv("CLAUDE_PROJECT_DIR", ""),
        session_id=os.getenv("CLAUDE_SESSION_ID", ""),
    )

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

        preview = (
            result.intent_preview[:40] + "..."
            if len(result.intent_preview) > 40
            else result.intent_preview
        )
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
    console.print(
        "[dim]Use 'jacked retrieve <id> --mode smart' for optimized context (default)[/dim]"
    )
    console.print(
        "[dim]Use 'jacked retrieve <id> --mode full' for complete transcript[/dim]"
    )

    # Print session IDs for easy copy
    console.print("\nSession IDs:")
    for i, result in enumerate(results, 1):
        console.print(f"  {i}. {result.session_id}")


@main.command()
@click.argument("session_id")
@click.option("--output", "-o", type=click.Path(), help="Save output to file")
@click.option("--summary", "-s", is_flag=True, help="Show summary instead of content")
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["smart", "plan", "labels", "agents", "full"]),
    default="smart",
    help="Retrieval mode (default: smart)",
)
@click.option(
    "--max-tokens", "-t", default=15000, help="Max token budget for smart mode"
)
@click.option("--inject", "-i", is_flag=True, help="Format for context injection")
def retrieve(
    session_id: str,
    output: Optional[str],
    summary: bool,
    mode: str,
    max_tokens: int,
    inject: bool,
):
    """Retrieve a session's context with smart mode support.

    Requires: uv tool install "claude-jacked[search]"
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
        content_parts.append(
            f"Agent summaries: {len(session.content.subagent_summaries)} ({tokens['subagent_summaries']} tokens)"
        )
    if session.content.summary_labels:
        content_parts.append(
            f"Labels: {len(session.content.summary_labels)} ({tokens['summary_labels']} tokens)"
        )
    if session.content.user_messages:
        content_parts.append(
            f"User messages: {len(session.content.user_messages)} ({tokens['user_messages']} tokens)"
        )
    if session.content.chunks:
        content_parts.append(
            f"Transcript chunks: {len(session.content.chunks)} ({tokens['chunks']} tokens)"
        )

    console.print(
        Panel(
            f"Session: {session.session_id}\n"
            f"Repository: {session.repo_name}\n"
            f"Machine: {session.machine}\n"
            f"Age: {session.format_relative_time()}\n"
            f"Local: {'Yes' if session.is_local else 'No'}\n"
            f"\nContent available:\n  "
            + "\n  ".join(content_parts)
            + f"\n\nEstimated tokens (smart): {tokens['total']}",
            title="Session Info",
        )
    )

    if session.is_local:
        resume_cmd = retriever.get_resume_command(session)
        console.print("\n[green][OK] Session exists locally![/green]")
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


@main.command()
@click.option("--cwd", default=None, help="Working directory to recover (default: current dir)")
@click.option("--exclude", default=None, help="Session id to exclude (the live one)")
@click.option("--session", "session_id", default=None, help="Recover this specific session id")
@click.option("--digest", "as_digest", is_flag=True, help="Emit the working-state digest for --session")
@click.option("--limit", "-n", default=3, help="How many candidates to list")
@click.option("--budget", default=12000, help="Digest size budget in characters")
@click.option("--json", "as_json", is_flag=True, help="Emit candidates as JSON")
def recover(cwd, exclude, session_id, as_digest, limit, budget, as_json):
    """Recover a crashed session for this folder from its on-disk transcript.

    Works on a bare install — no Qdrant/search extra required.
    Phase 1: 'jacked recover --json' ranks candidate sessions.
    Phase 2: 'jacked recover --session <id> --digest' prints the injection digest.
    """
    import json as _json
    from datetime import datetime, timezone
    from jacked import recover as rec

    target_cwd = cwd or os.getcwd()
    project_dir = rec.resolve_project_dir(target_cwd)

    if project_dir is None:
        if as_json:
            click.echo(_json.dumps({"project_dir": None, "chosen": None, "candidates": [], "count": 0}))
        else:
            console.print(f"[yellow]No recorded Claude sessions found for[/yellow] {target_cwd}")
        return

    # Phase 2 — digest for a specific session
    if session_id and as_digest:
        session_path = project_dir / f"{session_id}.jsonl"
        if not session_path.exists():
            console.print(f"[red]Session {session_id} not found in {project_dir}[/red]")
            sys.exit(1)
        digest = rec.build_digest(session_path)
        click.echo(rec.render_digest(digest, budget_chars=budget))
        return

    # Phase 1 — rank candidates
    exclude_id = exclude or os.getenv("CLAUDE_CODE_SESSION_ID") or os.getenv("CLAUDE_SESSION_ID")
    candidates = rec.list_candidates(project_dir, exclude_session_id=exclude_id)
    now = datetime.now(timezone.utc)
    idx = rec.recommend_index(candidates) if candidates else 0
    chosen = candidates[idx] if candidates else None
    top = candidates[:limit]
    # ensure the recommended candidate is present in the returned list
    if chosen is not None and chosen not in top:
        top = [chosen] + top[: max(0, limit - 1)]

    if as_json:
        payload = {
            "project_dir": str(project_dir),
            "chosen": chosen.to_dict(now) if chosen else None,
            "candidates": [c.to_dict(now) for c in top],
            "count": len(candidates),
        }
        click.echo(_json.dumps(payload))
        return

    if not top:
        console.print(f"[yellow]No prior session to recover in[/yellow] {project_dir}")
        return
    for c in top:
        marker = "->" if c is chosen else "  "
        click.echo(f"{marker} {c.session_id}  ({c.ai_title or 'untitled'})  "
                   f"{rec._relative_age(c.last_ts, now)}  [{c.git_branch or '?'}]")
        if c.last_prompt:
            click.echo(f"     last: {c.last_prompt[:120]}")


@main.command(name="sessions")
@click.option("--repo", "-r", help="Filter by repository path")
@click.option("--limit", "-n", default=20, help="Maximum results")
def list_sessions(repo: Optional[str], limit: int):
    """List indexed sessions. Requires: uv tool install "claude-jacked[search]" """
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
    """Delete a session from the index. Requires: uv tool install "claude-jacked[search]" """
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

    Requires: uv tool install "claude-jacked[search]"
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

    console.print(
        Panel(
            f"[bold red]WARNING: This will permanently delete ALL your indexed data![/bold red]\n\n"
            f"User: [cyan]{user_name}[/cyan]\n"
            f"Points to delete: [red]{count}[/red]\n\n"
            f"This only affects YOUR data. Teammates' data will be untouched.\n"
            f"After clearing, run 'jacked backfill' to re-index.",
            title="Clear Database",
        )
    )

    # Require typing confirmation phrase
    console.print("\n[bold]To confirm, type: DELETE MY DATA[/bold]")
    confirmation = click.prompt("Confirmation", default="", show_default=False)

    if confirmation != "DELETE MY DATA":
        console.print("[yellow]Cancelled - confirmation did not match[/yellow]")
        return

    # Do the delete
    deleted = client.delete_by_user(user_name)
    console.print(
        f"\n[green][OK][/green] Deleted {deleted} points for user '{user_name}'"
    )
    console.print("\n[dim]Run 'jacked backfill' to re-index your sessions[/dim]")


@main.command()
def status():
    """Show indexing health and Qdrant connectivity. Requires: uv tool install "claude-jacked[search]" """
    if not _require_search("status"):
        sys.exit(1)

    from jacked.client import QdrantSessionClient

    config = get_config()

    console.print(
        Panel(
            f"Endpoint: {config.qdrant_endpoint}\n"
            f"Collection: {config.collection_name}\n"
            f"Projects Dir: {config.claude_projects_dir}\n"
            f"Machine: {config.machine_name}",
            title="Configuration",
        )
    )

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
        console.print(
            Panel(
                f"Status: [green]{info['status']}[/green]\n"
                f"Points: {info['points_count']}\n"
                f"Segments: {info['segments_count']}\n"
                f"Indexed Vectors: {info['indexed_vectors_count']}",
                title="Qdrant Collection",
            )
        )
    else:
        console.print(
            Panel(
                "[red]Collection not found or Qdrant unreachable[/red]\n"
                "Run 'jacked backfill' to create collection and index sessions",
                title="Qdrant Status",
            )
        )


@main.command(name="webux")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
@click.option("--reload", is_flag=True, help="Auto-reload on file changes (dev mode)")
def webux(host: str, port: int, no_browser: bool, reload: bool):
    """Start the jacked web dashboard."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        console.print("[red]Error:[/red] webux requires the web extra.")
        console.print("Install it with:")
        console.print(r'  [bold]uv tool install "claude-jacked\[web]" --force[/bold]')
        sys.exit(1)

    # Propagate host/port to app via env vars (used for dynamic CORS + WebSocket origin checks)
    import os as _os

    _os.environ["JACKED_HOST"] = host
    _os.environ["JACKED_PORT"] = str(port)

    url = f"http://{host}:{port}"
    console.print(f"[bold]Starting jacked dashboard at {url}[/bold]")
    if reload:
        console.print("[dim]Auto-reload enabled — watching for file changes[/dim]")

    if not no_browser:
        import webbrowser

        webbrowser.open(url)

    uvicorn.run(
        "jacked.api.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["jacked"] if reload else None,
    )


def _service_http_ok(port: int, timeout: float = 1.0) -> bool:
    """True if the dashboard answers HTTP on 127.0.0.1:port.

    Always probes loopback, never the bind host — the service may bind
    0.0.0.0 (unroutable as a client target), but it's always reachable on
    127.0.0.1 once up. Any HTTP response, including a 4xx/5xx, means the
    server process is alive; only connection/timeout errors count as down.
    """
    import urllib.error as _ue
    import urllib.request as _ur

    try:
        with _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout):
            return True
    except _ue.HTTPError:
        return True  # server responded — it's up
    except Exception:
        return False


def _wait_service_ready(port: int, timeout: float = 15.0) -> bool:
    """Poll _service_http_ok until the dashboard answers or timeout elapses."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if _service_http_ok(port):
            return True
        _time.sleep(0.4)
    return _service_http_ok(port)


def _spawn_service_detached(host: str, port: int):
    """Spawn `jacked service start` detached so it survives the caller exiting.

    Returns the log path the detached service writes to. The child runs the
    tray icon + uvicorn (ServiceRunner). Windows uses DETACHED_PROCESS for the
    windowless pythonw.exe path and CREATE_NO_WINDOW for the jacked.exe fallback
    (a console trampoline that would otherwise pop a window); POSIX uses
    start_new_session. Shared by `jacked start` and `jacked service restart`.
    """
    import subprocess as _subprocess

    from jacked.findbin import find_bin
    from jacked.service import CLAUDE_DIR

    jacked_bin = find_bin("jacked") or sys.executable
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CLAUDE_DIR / "jacked-service.log"
    try:
        log_fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        log_fh = _subprocess.DEVNULL

    svc_args = ["service", "start", "--host", host, "--port", str(port)]
    if sys.platform == "win32":
        # ROOT CAUSE of "close the window and the tray dies": the uv `jacked.exe`
        # console-trampoline spawns python WITH a new console window even when we
        # launch it DETACHED_PROCESS. That visible console is the "command window"
        # users were closing — and closing it sends CTRL_CLOSE, killing the tray.
        #
        # Fix: launch the GUI-subsystem `pythonw.exe -m jacked` instead. pythonw
        # never gets a console, so the service is truly windowless and outlives
        # the launching terminal. Fall back to jacked.exe if pythonw isn't found.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            cmd = [str(pythonw), "-m", "jacked", *svc_args]
            # pythonw is GUI-subsystem and never touches a console, so
            # DETACHED_PROCESS (no console at all) is correct and windowless.
            _console = getattr(_subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            cmd = [jacked_bin, *svc_args]
            # jacked.exe is the console trampoline: under DETACHED_PROCESS it
            # auto-allocates a visible console. CREATE_NO_WINDOW gives it a
            # hidden one so the fallback stays as windowless as the pythonw path.
            _console = getattr(_subprocess, "CREATE_NO_WINDOW", 0x08000000)
        # Console flag (per binary above) + breakaway (escape any kill-on-close
        # job the terminal may have placed us in), with a fallback if breakaway
        # is disallowed by the job.
        _breakaway = getattr(_subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        _win_kwargs = dict(
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            close_fds=True,
        )
        try:
            _subprocess.Popen(
                cmd, creationflags=_console | _breakaway, **_win_kwargs
            )
        except OSError:
            _subprocess.Popen(cmd, creationflags=_console, **_win_kwargs)
    else:
        _subprocess.Popen(
            [jacked_bin, *svc_args],
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    return log_path


@main.command(name="start")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
@click.option(
    "--restart", is_flag=True, help="Force a restart even if already healthy."
)
def start(host: str | None, port: int | None, restart: bool):
    """Make sure the jacked service (dashboard + tray icon) is running.

    Idempotent. If it's already up and answering, this no-ops. If it's
    dead, crashed, stale, or hung, it (re)starts it DETACHED so it keeps
    running after this terminal closes. This is the command to run any time
    the tray disappears or the dashboard stops responding — you don't have
    to know whether it's down, just run `jacked start`.
    """
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
    from jacked.service.process import (
        is_port_available,
        is_process_alive,
        read_pid,
        remove_pid,
        stop_process_graceful,
        wait_for_port_free,
    )

    the_host = host or DEFAULT_HOST
    the_port = port or DEFAULT_PORT

    info = read_pid(PID_FILE)
    pid_alive = bool(info) and is_process_alive(info["pid"])
    responding = _service_http_ok(the_port)

    # Already healthy → nothing to do (unless forced).
    if pid_alive and responding and not restart:
        console.print(
            f"[green][OK][/green] jacked already running "
            f"(PID {info['pid']}, tray + dashboard on :{info['port']})"
        )
        console.print(f"[dim]http://127.0.0.1:{info['port']}[/dim]")
        return

    # Tear down whatever's there if it's stuck, or if a restart was forced.
    if pid_alive and (restart or not responding):
        why = (
            "restart requested"
            if restart
            else "process alive but dashboard not answering — restarting"
        )
        console.print(f"[dim]{why}...[/dim]")
        result = stop_process_graceful(PID_FILE)
        if result["was_running"] and not result["died"]:
            console.print("[red]Couldn't stop the stuck service. Aborting.[/red]")
            sys.exit(1)
        wait_for_port_free(the_host, the_port, timeout=10.0)
    elif info and not pid_alive:
        remove_pid(PID_FILE)
        console.print("[dim]Cleared a stale PID file left by a previous crash[/dim]")

    # Port held by something that isn't us?
    if not is_port_available(the_host, the_port):
        console.print(
            f"[red]Port {the_port} is in use by another process.[/red] "
            "Free it or pass --port."
        )
        sys.exit(1)

    console.print(f"[dim]Starting jacked service (detached) on :{the_port}...[/dim]")
    log_path = _spawn_service_detached(the_host, the_port)

    if _wait_service_ready(the_port, timeout=15.0):
        console.print(
            f"[green][OK][/green] jacked running — tray icon up, "
            f"dashboard at http://127.0.0.1:{the_port}"
        )
    else:
        console.print(
            f"[yellow]Started, but :{the_port} didn't answer within 15s.[/yellow]"
        )
        console.print(f"[dim]Check {log_path}[/dim]")
        sys.exit(1)


@main.command(name="check-version")
def check_version():
    """Check if a newer version of claude-jacked is available on PyPI."""
    from jacked import __version__
    from jacked.version_check import check_version_cached

    result = check_version_cached(__version__)
    if result is None:
        console.print("[yellow]Could not reach PyPI[/yellow]")
        return

    _log_to_db(
        "version_checks",
        current_version=__version__,
        latest_version=result["latest"],
        outdated=result["outdated"],
    )

    if result["outdated"]:
        console.print(
            f"[yellow]Update available:[/yellow] {__version__} \u2192 {result['latest']}"
        )
        console.print(
            "Run: [bold]jacked upgrade[/bold]  (installs, migrates settings, restarts service)"
        )
    else:
        console.print(f"[green]Up to date:[/green] {__version__}")


@main.command()
@click.option(
    "--extras",
    default="tray",
    help="Optional extras to install (tray, search, all). Default: tray.",
)
@click.option(
    "--skip-service",
    is_flag=True,
    help="Don't touch the running service — just upgrade the package + migrate settings.",
)
def upgrade(extras: str, skip_service: bool):
    """Upgrade claude-jacked end-to-end.

    Runs all three steps the tray 'Update' button would do:
      1. uv tool install 'claude-jacked[<extras>]' --force  (new code on disk)
      2. jacked install --force                              (migrate settings.json)
      3. jacked service restart                              (reload running service)

    On POSIX (macOS, Linux): runs inline. Inode semantics let us replace
    ourselves safely while the interpreter keeps running.

    On Windows: spawns a detached cmd.exe helper that waits for this
    process to exit before running the install. Windows can't overwrite
    a running .exe, so we have to step out of the way. This process
    exits cleanly and the helper takes over.
    """
    from jacked import __version__
    from jacked.findbin import find_bin
    from jacked.install_method import (
        can_auto_upgrade,
        detect_install_method,
        upgrade_command,
        upgrade_command_label,
    )
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE

    # Pre-flight: refuse editable / pip installs before touching the service.
    _ok, _reason = can_auto_upgrade()
    if not _ok:
        console.print(f"[red]Cannot auto-upgrade:[/red] {_reason}")
        sys.exit(2)

    method = detect_install_method()
    cmd = upgrade_command(extras)
    label = upgrade_command_label(extras)

    # uv-based flow requires `uv` on PATH; pip/pipx flows don't.
    if method == "uv":
        uv = find_bin("uv")
        if not uv:
            console.print(
                "[red]Error:[/red] jacked was installed via `uv tool install` "
                "but `uv` isn't on PATH. Install it from https://docs.astral.sh/uv/"
            )
            sys.exit(1)
        cmd[0] = uv  # use resolved absolute path

    console.print(
        f"[bold]Upgrading claude-jacked from v{__version__}...[/bold]  "
        f"[dim](install method: {method})[/dim]\n"
    )

    # Windows can't overwrite a running .exe. Spawn a detached cmd.exe that
    # waits for this process to die, then does the install + migrate + restart.
    if sys.platform == "win32":
        _spawn_windows_upgrade_helper(cmd, label, extras, skip_service)
        return

    # POSIX path: run inline.
    _run_upgrade_inline(cmd, label, extras, skip_service, PID_FILE, DEFAULT_HOST, DEFAULT_PORT)


def _run_upgrade_inline(
    cmd: list[str], label: str, extras: str, skip_service: bool,
    pid_file, host: str, port: int,
):
    """Inline upgrade for POSIX. Running binary gets replaced safely via inode."""
    import subprocess
    from jacked.findbin import find_bin
    from jacked.service.process import (
        is_process_alive,
        read_pid,
        stop_process_graceful,
        wait_for_port_free,
    )

    # Step 1: package upgrade (uv / pipx / pip, auto-detected).
    console.print(f"[dim]$ {label}[/dim]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        console.print(
            f"[red]Package upgrade failed (exit {result.returncode}). Aborting.[/red]"
        )
        sys.exit(result.returncode)

    # Re-resolve jacked — path may have changed after --force.
    jacked = find_bin("jacked")
    if not jacked:
        console.print(
            "[red]`jacked` not found after install.[/red] "
            "Check your PATH includes `~/.local/bin`."
        )
        sys.exit(1)

    # Step 2: migrate settings.json.
    console.print(f"\n[dim]$ {jacked} install --force[/dim]")
    result = subprocess.run([jacked, "install", "--force"])
    if result.returncode != 0:
        console.print(
            f"[yellow]`jacked install` exited {result.returncode}.[/yellow] "
            "Your settings.json may be in a partial state — check ~/.claude/settings.json.bak-*"
        )

    # Step 3: stop the tray if it's running, then start fresh detached.
    #
    # We call stop_process_graceful() directly — not `jacked service stop` —
    # because the subprocess version sends SIGTERM and returns without
    # waiting, and pystray's AppKit runloop on macOS can swallow SIGTERM.
    # The upgrade must not move on until the old PID is actually dead,
    # otherwise the detached `service start` below hits "port in use" and
    # the user is left with the pre-upgrade tray still running.
    if skip_service:
        console.print("\n[dim]Skipping service restart (--skip-service)[/dim]")
    else:
        info = read_pid(pid_file)
        was_running = bool(info) and is_process_alive(info["pid"])
        if was_running:
            console.print(f"\n[dim]$ stopping service (PID {info['pid']})[/dim]")
            result = stop_process_graceful(pid_file)
            if not result["died"]:
                console.print(
                    f"[red]Could not stop PID {info['pid']} — port {port} may still be in use.[/red]"
                )
                console.print(
                    "[dim]Run manually: "
                    f"kill -9 {info['pid']}   then:   {jacked} service start[/dim]"
                )
                sys.exit(1)
            if result["killed"]:
                console.print(
                    "[yellow]Tray ignored SIGTERM — force-killed.[/yellow]"
                )
            # Port can linger a beat after the PID dies (TIME_WAIT-ish).
            if not wait_for_port_free(host, port, timeout=10.0):
                console.print(
                    f"[red]Port {port} still in use after stop — aborting start.[/red]"
                )
                console.print(
                    f"[dim]Investigate with: lsof -iTCP:{port} -sTCP:LISTEN[/dim]"
                )
                sys.exit(1)

        # Start detached — the tray must survive this upgrade process exiting.
        console.print(f"\n[dim]$ {jacked} service start  (detached)[/dim]")
        from jacked.service import CLAUDE_DIR
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = CLAUDE_DIR / "jacked-service.log"
        try:
            log_fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
            subprocess.Popen(
                [jacked, "service", "start"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
                close_fds=True,
            )
            console.print(f"[dim]Logs: {log_path}[/dim]")
        except Exception as exc:
            console.print(f"[yellow]Could not spawn detached service: {exc}[/yellow]")
            console.print(f"[dim]Run manually: {jacked} service start[/dim]")

    console.print("\n[green][OK][/green] Upgrade complete.")


def _spawn_windows_upgrade_helper(
    cmd: list[str], label: str, extras: str, skip_service: bool,
):
    """Windows: spawn a detached cmd.exe helper and exit this process.

    Running jacked.exe can't be overwritten while we're holding it open.
    The helper is cmd.exe (a system binary we don't own), which stays
    valid no matter what the upgrade command does to the jacked venv
    or user site-packages.

    Helper steps:
      1. Wait for our PID to exit (avoids racing against the .exe lock).
      2. Run the detected upgrade command (uv / pipx / pip).
      3. `jacked install --force` (migrate settings.json).
      4. `jacked service restart` (unless --skip-service).
      5. Append progress to ~/.claude/jacked-update.log.
    """
    import os
    import subprocess
    import tempfile
    from jacked.service import CLAUDE_DIR

    my_pid = os.getpid()
    log_path = CLAUDE_DIR / "jacked-update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Re-quote the upgrade command for cmd.exe. Paths may contain spaces
    # (uv from AppData, user site-packages python.exe, etc.); every argv
    # element must be individually quoted to survive cmd's tokenization.
    upgrade_line = " ".join(f'"{arg}"' for arg in cmd)

    restart_line = (
        'if "%SKIP_SERVICE%"=="" (\r\n'
        '    echo [%date% %time%] service restart >> "%LOGFILE%"\r\n'
        '    jacked service restart >> "%LOGFILE%" 2>&1\r\n'
        ')\r\n'
    )
    batch_body = (
        '@echo off\r\n'
        'set LOGFILE=' + str(log_path) + '\r\n'
        'set SKIP_SERVICE=' + ("1" if skip_service else "") + '\r\n'
        'echo [%date% %time%] jacked upgrade helper starting (parent PID ' + str(my_pid) + ') >> "%LOGFILE%"\r\n'
        'echo [%date% %time%] upgrade command: ' + label + ' >> "%LOGFILE%"\r\n'
        ':wait\r\n'
        'tasklist /FI "PID eq ' + str(my_pid) + '" 2>NUL | find "' + str(my_pid) + '" >NUL\r\n'
        'if not errorlevel 1 (\r\n'
        '    timeout /t 1 /nobreak >NUL\r\n'
        '    goto wait\r\n'
        ')\r\n'
        'echo [%date% %time%] parent exited, running upgrade command >> "%LOGFILE%"\r\n'
        + upgrade_line + ' >> "%LOGFILE%" 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    echo [%date% %time%] ERROR: upgrade command failed >> "%LOGFILE%"\r\n'
        '    echo Jacked upgrade failed. See %LOGFILE% for details. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    echo Recovery: ' + label + ' ^&^& jacked install --force >> "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'echo [%date% %time%] running jacked install --force >> "%LOGFILE%"\r\n'
        'jacked install --force >> "%LOGFILE%" 2>&1\r\n'
        + restart_line +
        'echo [%date% %time%] upgrade complete >> "%LOGFILE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )

    # Write the batch file to %TEMP% — it deletes itself at the end.
    fd, batch_path = tempfile.mkstemp(suffix=".bat", prefix="jacked-upgrade-")
    try:
        with os.fdopen(fd, "w", newline="\r\n") as f:
            f.write(batch_body)
    except Exception:
        try:
            os.unlink(batch_path)
        except OSError:
            pass
        raise

    # Spawn the batch CREATE_NO_WINDOW, NOT DETACHED_PROCESS. The batch runs a
    # pile of console children — the `tasklist | find "<pid>"` + `timeout /t 1`
    # poll loop, then the uv/pip upgrade, `jacked install --force`, and
    # `jacked service restart`. DETACHED_PROCESS gives cmd.exe NO console, so
    # every one of those children auto-allocates its OWN visible console window
    # — that's the "find <pid>" window that flashes once a second and reappears
    # the instant you close it. CREATE_NO_WINDOW gives cmd.exe a HIDDEN console
    # that all children inherit, so nothing pops. CREATE_BREAKAWAY_FROM_JOB is
    # orthogonal to the console flag and still lets the helper survive the
    # launching terminal closing (modern terminals kill their job on close).
    # Fall back to CREATE_NO_WINDOW alone if the job forbids breakaway — never
    # back to DETACHED_PROCESS, or the windows come right back.
    NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    _helper_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", batch_path],
            creationflags=NO_WINDOW | BREAKAWAY,
            **_helper_kwargs,
        )
    except OSError:
        subprocess.Popen(
            ["cmd.exe", "/c", batch_path],
            creationflags=NO_WINDOW,
            **_helper_kwargs,
        )

    console.print(
        "[yellow]Windows upgrade:[/yellow] spawned detached helper. "
        "This process will now exit so `jacked.exe` can be replaced."
    )
    console.print(f"[dim]Watching log: {log_path}[/dim]")
    console.print(
        f"The helper will run `{label}` + `jacked install --force`"
        + ("" if skip_service else " + `jacked service restart`")
        + " after this process exits."
    )
    # Exit immediately so the lock on jacked.exe releases.
    sys.exit(0)


def _valid_hook_names() -> frozenset[str]:
    """Allowlist of hook names derived from files in data/hooks/.

    Using the filesystem as the single source of truth means adding a
    new hook doesn't require updating a separate list.
    """
    hooks_dir = _get_data_root() / "hooks"
    if not hooks_dir.exists():
        return frozenset()
    return frozenset(
        p.stem
        for p in hooks_dir.glob("*.py")
        if not p.stem.startswith("_")
    )


@main.command(name="_update_status_init", hidden=True)
@click.argument("from_version")
@click.argument("to_version")
@click.argument("method")
@click.option("--log-path", default=None)
def _update_status_init_shim(from_version, to_version, method, log_path):
    """Internal: initialize a fresh update-status file.

    Exit 0 on success, 2 on LockBusy (another updater active).
    The Windows batch checks errorlevel and aborts on 2.
    """
    from jacked.service import update_status as us_mod
    try:
        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version=from_version,
            to_version=to_version,
            method=method,
            log_path=log_path,
        )
    except us_mod.LockBusy as exc:
        click.echo(f"[update-status] lock busy: {exc}", err=True)
        sys.exit(2)


@main.command(name="_update_status", hidden=True)
@click.argument("phase")
@click.argument("status")
@click.option("--error", default=None)
@click.option("--recovery", default=None)
def _update_status_shim(phase, status, error, recovery):
    """Internal: write one status transition. `status` is in_progress|ok|failed."""
    from jacked.service import update_status as us_mod
    path = us_mod.UPDATE_STATUS_FILE
    try:
        if status == "in_progress":
            us_mod.begin_phase(path, phase)
        else:
            us_mod.end_phase(path, phase, status=status, error=error, recovery=recovery)
    except ValueError as exc:
        # Exit non-zero so the Windows batch's `if errorlevel 1` check fires
        # on phase-name drift between the batch and update_phases.PHASES.
        click.echo(f"[update-status] {exc}", err=True)
        sys.exit(1)


@main.command(name="_update_status_succeed", hidden=True)
def _update_status_succeed_shim():
    """Internal: mark overall=succeeded on the update-status file."""
    from jacked.service import update_status as us_mod
    us_mod.mark_succeeded(us_mod.UPDATE_STATUS_FILE)


@main.command(name="_hook", hidden=True)
@click.argument("name")
def _hook_shim(name: str):
    """Internal: dispatch to a hook handler by name.

    Called by Claude Code hooks via `jacked _hook <name>`. The handler's
    main() reads hook input from stdin as usual.

    Indirection keeps settings.json paths stable across `uv tool upgrade`.
    """
    if name not in _valid_hook_names():
        click.echo(f"Unknown hook: {name}", err=True)
        sys.exit(2)

    import importlib
    try:
        module = importlib.import_module(f"jacked.data.hooks.{name}")
    except ImportError as e:
        click.echo(f"Hook import failed: {name} ({e})", err=True)
        sys.exit(2)

    if not hasattr(module, "main"):
        click.echo(f"Hook has no main(): {name}", err=True)
        sys.exit(2)

    module.main()


@main.command()
@click.argument("category", type=click.Choice(["command", "agent", "hook"]))
@click.argument("name")
@click.option("--session-id", envvar="CLAUDE_SESSION_ID")
@click.option("--repo", envvar="CLAUDE_PROJECT_DIR")
def log(category, name, session_id, repo):
    """Record a command/agent/hook invocation to the analytics DB."""
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    if not DB_PATH.exists():
        return
    # Normalize repo_path to canonical forward-slash format
    norm_repo = str(_Path(repo).resolve()).replace("\\", "/") if repo else ""
    table_map = {
        "command": "command_usage",
        "agent": "agent_invocations",
        "hook": "hook_executions",
    }
    name_col = {"command": "command_name", "agent": "agent_name", "hook": "hook_name"}
    table = table_map[category]
    col = name_col[category]
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=0.5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            f"INSERT INTO {table} ({col}, timestamp, session_id, repo_path, success) VALUES (?, ?, ?, ?, ?)",
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                session_id or "",
                norm_repo,
                True,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


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
            console.print(
                Panel(
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
                )
            )
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
    console.print(
        "    Your name for session attribution (default: git user.name or system user)"
    )
    console.print(
        f"    Current: {os.getenv('JACKED_USER_NAME', SmartForkConfig._default_user_name())}\n"
    )

    console.print("[bold cyan]Ranking Weights (Optional):[/bold cyan]\n")
    console.print("  JACKED_TEAMMATE_WEIGHT")
    console.print("    Multiplier for teammate sessions vs yours (default: 0.8)\n")
    console.print("  JACKED_OTHER_REPO_WEIGHT")
    console.print("    Multiplier for other repos vs current (default: 0.7)\n")
    console.print("  JACKED_TIME_DECAY_HALFLIFE_WEEKS")
    console.print("    Weeks until session relevance halves (default: 35)\n")

    console.print("[bold]Example shell profile setup:[/bold]\n")
    console.print("  # Required")
    console.print(
        '  export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"'
    )
    console.print('  export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"')
    console.print("")
    console.print("  # Team setup (optional)")
    console.print('  export JACKED_USER_NAME="yourname"')
    console.print("")
    console.print("[dim]Run 'jacked configure --show' to see current values[/dim]")


def _get_data_root() -> Path:
    """Find the data root directory for skills/agents/commands.

    Data is now inside the package at jacked/data/.
    """
    return Path(__file__).parent / "data"


def _is_editable_install() -> bool:
    """Check if package is installed in editable (dev) mode.

    >>> # In a git repo with editable install, returns True
    >>> isinstance(_is_editable_install(), bool)
    True
    """
    repo_root = _get_data_root().parent.parent
    return (repo_root / ".git").is_dir()


def _jacked_home() -> Path:
    """Resolve jacked's home dir for manifest/last-install/asset install.

    Honors $JACKED_HOME so tests (and unusual setups) can redirect the
    ~/.claude tree; defaults to the real home directory.
    """
    import os as _os

    return Path(_os.getenv("JACKED_HOME") or Path.home())


# Path markers identifying jacked-managed hook entries in settings.json.
# Anchored to tokens we actually write — won't match a user's unrelated
# script that happens to share a hook name.
_JACKED_HOOK_PATH_MARKERS = (
    "/site-packages/jacked/data/hooks/",   # normal install
    "/claude-jacked/jacked/data/hooks/",   # editable clone path
    "jacked\" _hook ",                      # shim form we write: "<path>/jacked" _hook <name>
    "-m jacked _hook ",                     # fallback form (dev without PATH shim)
)


def _is_jacked_managed_hook_path(command: str) -> bool:
    """True if this settings.json command value was installed by jacked.

    Anchored to path substrings we write — won't falsely match a user's
    own script named security_gatekeeper.py in an unrelated directory.
    """
    if not command:
        return False
    return any(marker in command for marker in _JACKED_HOOK_PATH_MARKERS)


def _build_hook_command(hook_name: str) -> str:
    """Build the settings.json command for a jacked hook.

    Prefers the `jacked _hook <name>` shim (upgrade-safe via uv's stable
    binary path). Falls back to `{python} -m jacked _hook <name>` when
    `jacked` isn't on PATH (dev/editable installs). Never writes a bare
    site-packages path — that's the stale-path bug this exists to fix.
    """
    from jacked.findbin import find_bin

    jacked_bin = find_bin("jacked")
    if jacked_bin:
        return f'"{jacked_bin}" _hook {hook_name}'

    # Fallback for dev/editable without the shim on PATH.
    python_exe = sys.executable or shutil.which("python3") or shutil.which("python")
    return f'"{python_exe}" -m jacked _hook {hook_name}'


def _snapshot_settings(settings_path: Path) -> Path | None:
    """Copy settings.json to a timestamped backup. Returns backup path or None.

    No-op if source doesn't exist.
    """
    import shutil as _shutil
    import time

    if not settings_path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = settings_path.parent / f"{settings_path.name}.bak-{stamp}"
    i = 0
    while backup.exists():
        i += 1
        backup = settings_path.parent / f"{settings_path.name}.bak-{stamp}-{i}"
    _shutil.copy2(settings_path, backup)
    return backup


def _rotate_backups(dir_path: Path, prefix: str, keep: int = 5) -> None:
    """Keep only the newest `keep` backups; delete older ones."""
    backups = sorted(dir_path.glob(f"{prefix}*"))
    while len(backups) > keep:
        backups[0].unlink(missing_ok=True)
        backups = backups[1:]


def _write_settings_atomic(settings_path: Path, data: dict) -> None:
    """Atomically write settings.json via tempfile + os.replace.

    Prevents half-written JSON if the process is killed mid-install.
    """
    import json as _json
    import tempfile

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".tmp",
        dir=str(settings_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, settings_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _link_or_copy(src: Path, dst: Path) -> str:
    """Symlink src→dst for editable installs, copy otherwise.

    Fallback chain (editable mode): symlink → hardlink → copy.
    Windows symlinks need admin; hardlinks need same volume.

    Returns 'symlinked', 'hardlinked', or 'copied'.

    >>> isinstance(_link_or_copy.__doc__, str)
    True
    """
    import os as _os
    import shutil as _shutil

    # Remove existing file/symlink at destination
    if dst.is_symlink() or dst.exists():
        dst.unlink()

    if not _is_editable_install():
        _shutil.copy(src, dst)
        return "copied"

    # Editable mode — try symlink first
    try:
        _os.symlink(src.resolve(), dst)
        return "symlinked"
    except OSError:
        pass

    # Fallback: hardlink (same volume only)
    try:
        if src.stat().st_dev == dst.parent.stat().st_dev:
            _os.link(src, dst)
            return "hardlinked"
    except OSError:
        pass

    # Last resort: plain copy
    _shutil.copy(src, dst)
    return "copied"


def _install_asset_dir(
    src_dir: Path,
    dst_dir: Path,
    asset_label: str,
    *,
    glob_pattern: str = "*.md",
    force: bool = False,
) -> tuple[int, int, str | None]:
    """Install assets from src_dir to dst_dir with conflict handling.

    Handles: symlink detection, hardlink detection, content comparison,
    force overwrite, and interactive conflict prompts.

    Returns (installed_count, skipped_count, link_method).
    """
    if not src_dir.exists():
        return 0, 0, None

    dst_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    skipped = 0
    link_method = None

    for src_file in sorted(src_dir.glob(glob_pattern)):
        dst_file = dst_dir / src_file.name

        # Already a correct symlink — always skip
        if dst_file.is_symlink() and dst_file.resolve() == src_file.resolve():
            skipped += 1
            continue

        # Already a hardlink to same inode — always skip
        if not dst_file.is_symlink() and dst_file.exists():
            try:
                if dst_file.stat().st_ino == src_file.stat().st_ino:
                    skipped += 1
                    continue
            except OSError:
                pass

        # Existing file with same content — skip unless --force
        if not force and not dst_file.is_symlink() and dst_file.exists():
            if src_file.read_text(encoding="utf-8") == dst_file.read_text(
                encoding="utf-8"
            ):
                skipped += 1
                continue
            if sys.stdin.isatty() and not click.confirm(
                f"{asset_label.title()} '{src_file.name}' exists with different content. Overwrite?"
            ):
                skipped += 1
                continue

        link_method = _link_or_copy(src_file, dst_file)
        installed += 1

    return installed, skipped, link_method


def _sound_hook_marker() -> str:
    """Marker to identify jacked sound hooks."""
    return "# jacked-sound: "


def _get_sound_command(hook_type: str) -> str:
    """Generate platform-specific sound command.

    Detects OS at install time via sys.platform instead of runtime shell
    detection, because Claude Code runs hooks through cmd.exe on Windows
    which can't parse Unix shell syntax.

    Args:
        hook_type: 'notification' or 'complete'
    """
    import sys

    if hook_type == "notification":
        win_sound = "Exclamation"
        mac_sound = "Basso.aiff"
        linux_sound = "dialog-warning.oga"
    else:  # complete
        win_sound = "Asterisk"
        mac_sound = "Glass.aiff"
        linux_sound = "complete.oga"

    if sys.platform == "win32":
        return f'powershell -Command "[System.Media.SystemSounds]::{win_sound}.Play()"'

    log_cmd = f"(jacked log hook sound_{hook_type} 2>/dev/null &); "

    if sys.platform == "darwin":
        return (
            log_cmd
            + f'afplay /System/Library/Sounds/{mac_sound} 2>/dev/null || printf "\\a"'
        )

    # Linux (including WSL)
    return (
        log_cmd + "(if grep -qi microsoft /proc/version 2>/dev/null; then "
        f'powershell.exe -Command "[System.Media.SystemSounds]::{win_sound}.Play()" 2>/dev/null || printf "\\a"; '
        "else "
        f'paplay /usr/share/sounds/freedesktop/stereo/{linux_sound} 2>/dev/null || printf "\\a"; '
        "fi)"
    )


def _replace_stale_sound_hook(hook_entries: list, marker: str, hook_type: str) -> bool:
    """Replace a stale Unix-style sound hook with the current platform-specific one.

    Returns True if a replacement was made.
    """
    for entry in hook_entries:
        for hook in entry.get("hooks", []):
            cmd = str(hook.get("command", ""))
            if marker in cmd and "uname" in cmd:
                hook["command"] = marker + _get_sound_command(hook_type)
                return True
    return False


def _install_sound_hooks(existing: dict, settings_path: Path):
    """Install sound notification hooks."""
    import json

    marker = _sound_hook_marker()

    # Notification hook
    if "Notification" not in existing["hooks"]:
        existing["hooks"]["Notification"] = []

    notif_exists = any(marker in str(h) for h in existing["hooks"]["Notification"])
    if not notif_exists:
        existing["hooks"]["Notification"].append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": marker + _get_sound_command("notification"),
                    }
                ],
            }
        )
        console.print("[green][OK][/green] Added Notification sound hook")
    elif _replace_stale_sound_hook(
        existing["hooks"]["Notification"], marker, "notification"
    ):
        console.print(
            "[green][OK][/green] Updated Notification sound hook (fixed for this OS)"
        )
    else:
        console.print("[yellow][-][/yellow] Notification sound hook exists")

    # Stop sound hook (separate from index)
    if "Stop" not in existing["hooks"]:
        existing["hooks"]["Stop"] = []

    stop_exists = any(marker in str(h) for h in existing["hooks"]["Stop"])
    if not stop_exists:
        existing["hooks"]["Stop"].append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": marker + _get_sound_command("complete"),
                    }
                ],
            }
        )
        console.print("[green][OK][/green] Added Stop sound hook")
    elif _replace_stale_sound_hook(existing["hooks"]["Stop"], marker, "complete"):
        console.print("[green][OK][/green] Updated Stop sound hook (fixed for this OS)")
    else:
        console.print("[yellow][-][/yellow] Stop sound hook exists")

    settings_path.write_text(json.dumps(existing, indent=2))


def _remove_sound_hooks(settings_path: Path) -> bool:
    """Remove jacked sound hooks. Returns True if any removed."""
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    marker = _sound_hook_marker()
    modified = False

    for hook_type in ["Notification", "Stop"]:
        if hook_type in settings.get("hooks", {}):
            before = len(settings["hooks"][hook_type])
            settings["hooks"][hook_type] = [
                h for h in settings["hooks"][hook_type] if marker not in str(h)
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
        console.print(
            f"[red][FAIL][/red] Found {which} marker but no {missing} marker in CLAUDE.md"
        )
        console.print(
            "Your CLAUDE.md has a corrupted jacked rules block. Please fix it manually:"
        )
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
            console.print(
                "[yellow][-][/yellow] Behavioral rules already configured correctly"
            )
            return
        else:
            # Version upgrade needed
            console.print("\n[bold]Behavioral rules update available:[/bold]")
            console.print(f"[dim]{rules_text}[/dim]")
            if (
                not force
                and sys.stdin.isatty()
                and not click.confirm("Update behavioral rules in CLAUDE.md?")
            ):
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
                console.print(
                    f"[red][FAIL][/red] Permission denied writing to {claude_md_path}"
                )
                console.print("Check file permissions and try again.")
                return
            console.print(
                "[green][OK][/green] Updated behavioral rules to latest version"
            )
            return

    # Fresh install - show and confirm
    console.print("\n[bold]Proposed behavioral rules for ~/.claude/CLAUDE.md:[/bold]")
    console.print(f"[dim]{rules_text}[/dim]")
    if (
        not force
        and sys.stdin.isatty()
        and not click.confirm("Add these behavioral rules to your global CLAUDE.md?")
    ):
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
        console.print(
            f"[red][FAIL][/red] Permission denied writing to {claude_md_path}"
        )
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
        console.print(
            f"[red][FAIL][/red] Permission denied writing to {claude_md_path}"
        )
        return False
    return True



def _session_tracker_marker() -> str:
    """Marker to identify jacked session-account tracker hooks."""
    return "# jacked-session-tracker"


SESSION_TRACKER_EVENTS = [
    ("SessionStart", ""),
    ("Notification", "auth_success"),
    ("SessionEnd", ""),
    ("Stop", ""),
    ("UserPromptSubmit", ""),
]


def _install_session_tracker_hook(existing: dict, settings_path: Path):
    """Install session-account tracker hooks for SessionStart, Notification(auth_success), SessionEnd, and Stop (heartbeat).

    Registers hooks that track which Anthropic account each Claude Code session
    is using by reading ~/.claude/.credentials.json at session start and on re-auth.
    The Stop hook fires a throttled heartbeat to keep sessions visible in the dashboard.
    """
    marker = _session_tracker_marker()
    script_path = _get_data_root() / "hooks" / "session_account_tracker.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] Session tracker script not found: {script_path}"
        )
        console.print("[yellow]Skipping session tracker installation[/yellow]")
        return

    command_str = _build_hook_command("session_account_tracker")

    modified = False
    for event_name, matcher in SESSION_TRACKER_EVENTS:
        if event_name not in existing["hooks"]:
            existing["hooks"][event_name] = []

        # Find existing hook for this event+matcher.
        # Match jacked-managed entries by anchored path markers OR the new shim form.
        hook_index = None
        needs_upgrade = False
        for i, hook_entry in enumerate(existing["hooks"][event_name]):
            entry_matcher = hook_entry.get("matcher", "")
            if entry_matcher != matcher:
                continue
            entry_cmd = ""
            for h in hook_entry.get("hooks", []):
                entry_cmd = h.get("command", "")
                break
            hook_str = str(hook_entry)
            is_ours = (
                marker in hook_str
                or _is_jacked_managed_hook_path(entry_cmd)
                or (
                    "session_account_tracker" in entry_cmd
                    and _is_jacked_managed_hook_path(entry_cmd)
                )
            )
            if is_ours:
                hook_index = i
                for h in hook_entry.get("hooks", []):
                    if h.get("command", "") != command_str:
                        needs_upgrade = True
                break

        if hook_index is not None and not needs_upgrade:
            continue

        hook_entry = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command_str,
                    "async": True,
                }
            ],
        }

        if hook_index is not None and needs_upgrade:
            existing["hooks"][event_name][hook_index] = hook_entry
            modified = True
        else:
            existing["hooks"][event_name].append(hook_entry)
            modified = True

    if not modified:
        console.print("[yellow][-][/yellow] Session tracker hooks already configured")
        return

    _write_settings_atomic(settings_path, existing)
    events_str = ", ".join(e for e, _ in SESSION_TRACKER_EVENTS)
    console.print(f"[green][OK][/green] Installed session tracker for: {events_str}")

    # Post-install verification: warn if any expected event is missing
    _verify_session_tracker_hooks(existing)


def _verify_session_tracker_hooks(settings: dict):
    """Verify all SESSION_TRACKER_EVENTS are present in the hooks config.

    Prints a warning for any event that's missing its session_account_tracker
    entry.  Called after install to catch partial writes or manual edits.

    >>> _verify_session_tracker_hooks({"hooks": {
    ...     "SessionStart": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "Notification": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "SessionEnd": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "Stop": [{"hooks": [{"command": "session_account_tracker"}]}],
    ... }})

    >>> _verify_session_tracker_hooks({"hooks": {"SessionStart": []}})  # doctest: +SKIP
    """
    hooks = settings.get("hooks", {})
    for event_name, _ in SESSION_TRACKER_EVENTS:
        entries = hooks.get(event_name, [])
        found = any("session_account_tracker" in str(e) for e in entries)
        if not found:
            console.print(
                f"[yellow][WARN][/yellow] Session tracker missing for {event_name}"
            )


def _ensure_permission_request_hook(existing: dict, command_str: str):
    """Ensure the gatekeeper is registered for PermissionRequest events."""
    if "PermissionRequest" not in existing.get("hooks", {}):
        existing.setdefault("hooks", {})["PermissionRequest"] = []
    # Only strip jacked-managed entries — leave user custom hooks alone.
    existing["hooks"]["PermissionRequest"] = [
        h for h in existing["hooks"]["PermissionRequest"]
        if not any(
            _is_jacked_managed_hook_path(inner.get("command", ""))
            for inner in h.get("hooks", [])
        )
    ]
    existing["hooks"]["PermissionRequest"].append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command_str, "timeout": 10}],
    })


def _install_security_hook(existing: dict, settings_path: Path):
    """Install a single catch-all security gatekeeper PreToolUse hook.

    Uses an empty matcher to intercept ALL tool calls. The gatekeeper script
    decides internally which tools to process vs pass-through based on the
    DB/registry config. Migrates old per-tool entries to catch-all mode.

    Handles fresh install, version upgrades, and migration from PermissionRequest.
    Only rewrites jacked-managed entries — user-custom hooks are left alone.
    """
    script_path = _get_data_root() / "hooks" / "security_gatekeeper.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] Security gatekeeper script not found: {script_path}"
        )
        console.print("[yellow]Skipping security gatekeeper installation[/yellow]")
        return

    command_str = _build_hook_command("security_gatekeeper")

    def _entry_is_jacked_gatekeeper(entry: dict) -> bool:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if _is_jacked_managed_hook_path(cmd):
                return True
        return False

    # Migrate: remove old jacked-managed PermissionRequest gatekeeper hooks
    if "PermissionRequest" in existing.get("hooks", {}):
        old_hooks = existing["hooks"]["PermissionRequest"]
        before = len(old_hooks)
        existing["hooks"]["PermissionRequest"] = [
            h for h in old_hooks if not _entry_is_jacked_gatekeeper(h)
        ]
        if len(existing["hooks"]["PermissionRequest"]) < before:
            console.print(
                "[green][OK][/green] Migrated security hook from PermissionRequest to PreToolUse"
            )

    if "PreToolUse" not in existing["hooks"]:
        existing["hooks"]["PreToolUse"] = []

    # Migrate: remove old jacked-managed per-tool gatekeeper entries (non-empty matcher)
    existing["hooks"]["PreToolUse"] = [
        h for h in existing["hooks"]["PreToolUse"]
        if not (
            _entry_is_jacked_gatekeeper(h)
            and h.get("matcher", "") != ""
        )
    ]

    # Check if jacked catch-all already exists; upgrade its command if needed.
    for entry in existing["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "" and _entry_is_jacked_gatekeeper(entry):
            for h in entry.get("hooks", []):
                if h.get("command", "") != command_str:
                    h["command"] = command_str

            _ensure_permission_request_hook(existing, command_str)
            _write_settings_atomic(settings_path, existing)
            console.print(
                "[green][OK][/green] Security gatekeeper hook configured"
            )
            return

    # Add catch-all PreToolUse entry
    existing["hooks"]["PreToolUse"].append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command_str, "timeout": 30}],
    })

    # Also register as PermissionRequest to auto-approve comment-stripped
    # commands and provide updatedInput (clean command without # comments).
    _ensure_permission_request_hook(existing, command_str)

    _write_settings_atomic(settings_path, existing)
    console.print("[green][OK][/green] Installed security gatekeeper (PreToolUse + PermissionRequest)")

    # Clean up stale prompt file from older versions (v0.3.9 and earlier created
    # this automatically, but it goes stale on upgrades and triggers warnings).
    # Users who want a custom prompt can create it manually.
    from jacked.data.hooks import security_gatekeeper as gk

    prompt_path = Path.home() / ".claude" / "gatekeeper-prompt.txt"
    if prompt_path.exists():
        try:
            existing_prompt = prompt_path.read_text(encoding="utf-8").strip()
            # If it's an unmodified built-in prompt (current or stale), remove it
            if existing_prompt == gk.SECURITY_PROMPT.strip():
                prompt_path.unlink()
                console.print(
                    "[dim][-][/dim] Removed default gatekeeper prompt (built-in is used automatically)"
                )
            else:
                # Check if it's a stale built-in that's missing required placeholders
                if not all(
                    p in existing_prompt
                    for p in ["{command}", "{cwd}", "{file_context}"]
                ):
                    prompt_path.unlink()
                    console.print(
                        "[yellow][OK][/yellow] Removed stale gatekeeper prompt (missing required placeholders)"
                    )
                else:
                    console.print(
                        "[yellow][-][/yellow] Custom gatekeeper prompt detected (not overwriting)"
                    )
        except Exception:
            pass


def _remove_security_hook(settings_path: Path) -> bool:
    """Remove jacked security gatekeeper hook. Returns True if removed.

    Checks both PreToolUse (current) and PermissionRequest (legacy).
    """
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    modified = False

    for hook_type in ["PreToolUse", "PermissionRequest"]:
        if hook_type not in settings.get("hooks", {}):
            continue
        before = len(settings["hooks"][hook_type])
        settings["hooks"][hook_type] = [
            h
            for h in settings["hooks"][hook_type]
            if "security_gatekeeper" not in str(h)
        ]
        if len(settings["hooks"][hook_type]) < before:
            modified = True

    if modified:
        settings_path.write_text(json.dumps(settings, indent=2))
        console.print("[green][OK][/green] Removed security gatekeeper hook")
        # Clean up default prompt file but preserve genuinely customized ones
        prompt_path = Path.home() / ".claude" / "gatekeeper-prompt.txt"
        if prompt_path.exists():
            try:
                from jacked.data.hooks import security_gatekeeper as gk

                existing_prompt = prompt_path.read_text(encoding="utf-8").strip()
                if existing_prompt == gk.SECURITY_PROMPT.strip():
                    prompt_path.unlink()
                    console.print("[dim][-][/dim] Removed default gatekeeper prompt")
                elif not all(
                    p in existing_prompt
                    for p in ["{command}", "{cwd}", "{file_context}"]
                ):
                    prompt_path.unlink()
                    console.print(
                        "[dim][-][/dim] Removed stale gatekeeper prompt (missing placeholders)"
                    )
                else:
                    console.print(
                        "[yellow][-][/yellow] Keeping custom gatekeeper prompt file"
                    )
            except Exception:
                pass
        return True

    return False


def _remove_session_tracker_hooks(settings_path: Path) -> bool:
    """Remove jacked session-account tracker hooks. Returns True if removed."""
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    modified = False

    for event_name, _ in SESSION_TRACKER_EVENTS:
        if event_name not in settings.get("hooks", {}):
            continue
        before = len(settings["hooks"][event_name])
        settings["hooks"][event_name] = [
            h
            for h in settings["hooks"][event_name]
            if "session_account_tracker" not in str(h)
        ]
        if len(settings["hooks"][event_name]) < before:
            modified = True

    if modified:
        settings_path.write_text(json.dumps(settings, indent=2))
        console.print("[green][OK][/green] Removed session tracker hooks")
        return True

    return False


def _qa_hook_marker() -> str:
    """Marker to identify jacked QA suggestion hook."""
    return "# jacked-qa-suggest"


def _install_qa_hook(existing: dict, settings_path: Path):
    """Install QA suggestion Stop hook that detects UI file changes.

    Registers a Stop hook that checks git diff for UI file changes
    and suggests running /qa when changes are detected.

    >>> # Smoke test — function exists and is callable
    >>> callable(_install_qa_hook)
    True
    """
    script_path = _get_data_root() / "hooks" / "qa_suggest.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] QA suggest script not found: {script_path}"
        )
        return

    command_str = _build_hook_command("qa_suggest")

    if "Stop" not in existing["hooks"]:
        existing["hooks"]["Stop"] = []

    def _is_jacked_qa_entry(entry: dict) -> bool:
        for h in entry.get("hooks", []):
            if _is_jacked_managed_hook_path(h.get("command", "")):
                if "qa_suggest" in h.get("command", ""):
                    return True
        return False

    # Check if already installed; upgrade the command if path changed.
    for entry in existing["hooks"]["Stop"]:
        if _is_jacked_qa_entry(entry):
            for h in entry.get("hooks", []):
                if h.get("command", "") != command_str:
                    h["command"] = command_str
                    _write_settings_atomic(settings_path, existing)
                    console.print(
                        "[green][OK][/green] Updated QA suggest hook (path migrated to shim)"
                    )
                    return
            console.print(
                "[yellow][-][/yellow] QA suggest hook already configured"
            )
            return

    existing["hooks"]["Stop"].append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command_str, "async": True}],
    })

    _write_settings_atomic(settings_path, existing)
    console.print("[green][OK][/green] Installed QA suggest hook (Stop event)")


def _remove_qa_hook(settings_path: Path) -> bool:
    """Remove jacked QA suggestion hook. Returns True if removed.

    >>> # Smoke test — function exists and is callable
    >>> callable(_remove_qa_hook)
    True
    """
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    if "Stop" not in settings.get("hooks", {}):
        return False

    before = len(settings["hooks"]["Stop"])
    settings["hooks"]["Stop"] = [
        h for h in settings["hooks"]["Stop"] if "qa_suggest" not in str(h)
    ]

    if len(settings["hooks"]["Stop"]) < before:
        settings_path.write_text(json.dumps(settings, indent=2))
        console.print("[green][OK][/green] Removed QA suggest hook")
        return True

    return False


CHROME_DEVTOOLS_MODES: dict[str, list[str]] = {
    "autoConnect": ["--autoConnect"],
    "browserUrl": ["--browserUrl", "http://127.0.0.1:9222"],
    "launch": [],
    "headless": ["--headless"],
}


def _run_claude_mcp(
    *args: str, timeout: int = 10
) -> "subprocess.CompletedProcess[str] | None":
    """Run a ``claude mcp`` subcommand, returning the result or None on error."""
    import shutil
    import subprocess

    from jacked.winproc import NO_WINDOW

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None

    try:
        return subprocess.run(
            [claude_bin, "mcp", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _run_claude_plugin(
    *args: str, timeout: int = 120
) -> "subprocess.CompletedProcess[str] | None":
    """Run a ``claude plugin`` subcommand, returning the result or None on error.

    stdin is closed (DEVNULL) so an unexpected confirmation prompt can never
    hang ``jacked install``.
    """
    import shutil
    import subprocess

    from jacked.winproc import NO_WINDOW

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return None

    try:
        return subprocess.run(
            [claude_bin, "plugin", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _install_chrome_devtools_mcp(force: bool = False) -> None:
    """Install Chrome DevTools MCP server (user-scoped via ``claude mcp add``)."""
    result = _run_claude_mcp("get", "chrome-devtools")
    already_installed = result is not None and result.returncode == 0

    if already_installed and not force:
        console.print("[yellow][-][/yellow] Chrome DevTools MCP already configured")
        return

    if already_installed and force:
        rm = _run_claude_mcp("remove", "chrome-devtools", "-s", "user")
        if rm is None or rm.returncode != 0:
            console.print("[yellow][WARN][/yellow] Could not remove existing Chrome DevTools MCP — attempting overwrite")

    add = _run_claude_mcp(
        "add", "-s", "user", "chrome-devtools", "--",
        "npx", "chrome-devtools-mcp@latest", "--autoConnect",
        timeout=30,
    )
    if add is None:
        console.print("[red][FAIL][/red] Chrome DevTools MCP setup failed (claude CLI not found or timed out)")
    elif add.returncode == 0:
        console.print("[green][OK][/green] Chrome DevTools MCP configured (autoConnect)")
        console.print("[dim]     Requires Chrome 144+ with remote debugging enabled[/dim]")
        console.print("[dim]     Enable at: chrome://inspect/#remote-debugging[/dim]")
    else:
        console.print(f"[red][FAIL][/red] Chrome DevTools MCP setup failed: {add.stderr.strip()}")


def _remove_chrome_devtools_mcp() -> bool:
    """Remove Chrome DevTools MCP server. Returns True if removed."""
    result = _run_claude_mcp("remove", "chrome-devtools", "-s", "user")
    if result is not None and result.returncode == 0:
        console.print("[green][OK][/green] Removed Chrome DevTools MCP")
        return True
    return False


def _get_chrome_devtools_mcp_status() -> dict:
    """Get Chrome DevTools MCP configuration status.

    Returns dict with keys: installed (bool), mode (str | None), details (str).
    """
    result = _run_claude_mcp("get", "chrome-devtools")
    if result is None:
        return {"installed": False, "mode": None, "details": "claude CLI not found or timed out"}
    if result.returncode != 0:
        return {"installed": False, "mode": None, "details": "Not configured"}

    output = result.stdout.strip()
    # Parse mode from the args line
    if "--autoConnect" in output:
        mode = "autoConnect"
    elif "--browserUrl" in output:
        mode = "browserUrl"
    elif "--headless" in output:
        mode = "headless"
    else:
        mode = "launch"
    return {"installed": True, "mode": mode, "details": output}


def _set_chrome_devtools_mcp_mode(mode: str) -> tuple[bool, str]:
    """Reconfigure Chrome DevTools MCP connection mode.

    Returns (success, message). Captures existing config before removal
    so it can be restored if the re-add fails.
    """
    if mode not in CHROME_DEVTOOLS_MODES:
        return False, f"Unknown mode: {mode}. Valid: {', '.join(CHROME_DEVTOOLS_MODES)}"

    # Capture current mode for rollback
    current = _run_claude_mcp("get", "chrome-devtools")
    had_existing = current is not None and current.returncode == 0
    prev_mode_args: list[str] = []
    if had_existing:
        output = current.stdout
        for m, args in CHROME_DEVTOOLS_MODES.items():
            if args and args[0] in output:
                prev_mode_args = args
                break

    # Remove existing
    if had_existing:
        rm = _run_claude_mcp("remove", "chrome-devtools", "-s", "user")
        if rm is None or rm.returncode != 0:
            return False, "Failed to remove existing configuration"

    # Re-add with new mode
    add_args = ["add", "-s", "user", "chrome-devtools", "--",
                "npx", "chrome-devtools-mcp@latest"] + CHROME_DEVTOOLS_MODES[mode]
    add = _run_claude_mcp(*add_args, timeout=30)

    if add is not None and add.returncode == 0:
        return True, f"Chrome DevTools MCP set to {mode}"

    # Rollback: restore previous config if add failed
    if had_existing:
        _run_claude_mcp(
            "add", "-s", "user", "chrome-devtools", "--",
            "npx", "chrome-devtools-mcp@latest", *prev_mode_args,
            timeout=30,
        )
    error = add.stderr.strip() if add else "timed out or claude CLI not found"
    return False, f"Failed to set mode: {error}"


def _detect_project_env() -> str | None:
    """Detect the project's Python env root from the running interpreter.

    Prefers sys.executable (avoids detecting wrong env when running from
    conda base).  Falls back to CONDA_PREFIX if sys.executable doesn't
    look like an env.

    >>> import sys; _detect_project_env() is None or isinstance(_detect_project_env(), str)
    True
    """
    import os as _os

    exe = Path(sys.executable).resolve()
    # Windows: envs/jacked/python.exe  -> parent = envs/jacked
    # Linux:   envs/jacked/bin/python  -> parent.parent = envs/jacked
    for env_root in (exe.parent, exe.parent.parent):
        if (env_root / "conda-meta").exists() or (env_root / "pyvenv.cfg").exists():
            return str(env_root).replace("\\", "/")

    prefix = _os.environ.get("CONDA_PREFIX")
    if prefix and (Path(prefix) / "conda-meta").exists():
        return prefix.replace("\\", "/")
    return None


def _validate_env_path(env_path: str) -> str | None:
    """Validate env_path is a real Python env.  Returns error message or None.

    >>> _validate_env_path("") is not None
    True
    >>> _validate_env_path("relative/path") is not None
    True
    """
    if not env_path or len(env_path) > 500:
        return "Invalid path length"
    if "\x00" in env_path or ".." in env_path:
        return "Path contains invalid characters"
    p = Path(env_path)
    if not p.is_absolute():
        return "Must be an absolute path"
    if not (p / "conda-meta").exists() and not (p / "pyvenv.cfg").exists():
        return "Not a recognized Python environment (no conda-meta or pyvenv.cfg)"
    return None


def _write_project_env(repo_path: str, env_path: str) -> bool:
    """Write env path to .git/jacked/env for hook consumption.

    Returns True if written, False if repo has no .git directory.

    >>> # Only writes when .git exists
    """
    git_dir = Path(repo_path) / ".git"
    if not git_dir.is_dir():
        return False
    jacked_dir = git_dir / "jacked"
    jacked_dir.mkdir(parents=True, exist_ok=True)
    (jacked_dir / "env").write_text(env_path + "\n", encoding="utf-8")
    return True


@main.command()
@click.option("--sounds", is_flag=True, help="Install sound notification hooks")
@click.option(
    "--search",
    is_flag=True,
    help="Install session indexing hook (requires [search] extra)",
)
@click.option(
    "--no-security",
    is_flag=True,
    help="Skip installing the security gatekeeper hook (hook is installed but disabled by default)",
)
@click.option("--no-rules", is_flag=True, help="Skip behavioral rules in CLAUDE.md")
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing agents/commands without prompting",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the change-summary as JSON instead of the human summary",
)
def install(
    sounds: bool,
    search: bool,
    no_security: bool,
    no_rules: bool,
    force: bool,
    as_json: bool,
):
    """Auto-install skill, agents, commands, and optional hooks.

    Base install: agents, commands, behavioral rules, /jacked skill,
    and the security gatekeeper hook (installed disabled — Claude Code's
    auto permission mode handles approvals natively; turn the gatekeeper on
    from the dashboard at Settings > Gatekeeper when you want LLM-evaluated
    interception layered on top).
    Use --no-security to skip installing the gatekeeper hook entirely.
    Use --search to add session indexing (requires qdrant-client).
    """
    import json

    home = _jacked_home()
    pkg_root = _get_data_root()

    # Capture the prior manifest BEFORE we touch anything, so the diff
    # reflects source-now vs source-at-last-install (correct for both copy
    # and editable/symlink installs).
    from datetime import datetime, timezone

    from jacked import __version__ as _ver
    from jacked import install_manifest as _mani
    from jacked import install_summary as _isum

    _manifest_path = home / ".claude" / "jacked-manifest.json"
    _prior_manifest = _mani.load(_manifest_path)
    _prior_version = _prior_manifest.get("version") if _prior_manifest else None

    install_search = search
    install_security = not no_security

    # In --json mode, suppress the per-step "[OK] ..." chatter (and the same
    # chatter emitted by helper functions) so stdout carries only the JSON
    # record. The try/finally guarantees the module-level console is restored
    # even if install raises — otherwise a later in-process command (tray)
    # would silently inherit quiet=True.
    _prev_quiet = console.quiet
    if as_json:
        console.quiet = True
    try:
        _run_install(
            home=home,
            pkg_root=pkg_root,
            sounds=sounds,
            no_rules=no_rules,
            force=force,
            as_json=as_json,
            install_search=install_search,
            install_security=install_security,
        )
    finally:
        console.quiet = _prev_quiet

    # --- Change summary (manifest-driven) ---
    # Hash source-now, diff against the prior manifest, prune artifacts that
    # jacked installed before but no longer ships, then persist the new
    # manifest + the dashboard-readable last-install record.
    _current_hashes = _mani.hash_source(pkg_root)
    _d = _mani.diff(_prior_manifest, _current_hashes)
    _mani.prune_removed(_d, home)
    _now = datetime.now(timezone.utc).isoformat()
    _mani.write(_manifest_path, _ver, _current_hashes, _now)
    _record = _isum.build_record(_d, _prior_version, _ver, _now)
    _isum.write_last_install(_record, home / ".claude" / "jacked-last-install.json")

    if as_json:
        click.echo(json.dumps(_record))
    else:
        console.print("")
        console.print(_isum.render_terminal(_record))
        # Required-plugin blocker only — the full recommendations now live in
        # `jacked doctor`.
        _warn_required_plugins_missing()


def _run_install(
    *,
    home: Path,
    pkg_root: Path,
    sounds: bool,
    no_rules: bool,
    force: bool,
    as_json: bool,
    install_search: bool,
    install_security: bool,
) -> None:
    """Run the artifact/hook/rules installation (no manifest, no summary).

    Split out of `install` so the change-summary orchestration can wrap it in
    a try/finally that always restores console state.
    """
    import json
    import shutil

    if not as_json:
        console.print("[bold]Installing Jacked...[/bold]\n")

    # Check for existing settings
    settings_path = home / ".claude" / "settings.json"
    if settings_path.exists():
        # Snapshot before we mutate — timestamped, keeps last 5.
        backup = _snapshot_settings(settings_path)
        if backup:
            _rotate_backups(settings_path.parent, prefix="settings.json.bak-", keep=5)
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
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
                    "async": True,
                }
            ],
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
            console.print("[green][OK][/green] Added Stop hook (session indexing)")
        elif needs_async_update:
            existing["hooks"]["Stop"][hook_index] = hook_config_stop
            settings_path.write_text(json.dumps(existing, indent=2))
            console.print("[green][OK][/green] Updated Stop hook with async: true")
        else:
            console.print("[yellow][-][/yellow] Stop hook already configured")
    else:
        console.print(
            r"[dim][-][/dim] Skipping session indexing hook (install \[search] extra to enable)"
        )

    # Install skills — iterate all skills/*/SKILL.md in data root
    # Claude Code expects skills in subdirectories with SKILL.md
    skills_src_dir = pkg_root / "skills"
    skill_count = 0
    if skills_src_dir.exists():
        for skill_md in skills_src_dir.glob("*/SKILL.md"):
            skill_name = skill_md.parent.name
            skill_dir = home / ".claude" / "skills" / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            dst = skill_dir / "SKILL.md"
            # Use _link_or_copy (symlink in editable mode, copy otherwise).
            # Plain shutil.copy raises SameFileError if dst is already a
            # symlink pointing to src — broke the tray-triggered upgrade
            # flow when a manual symlink from dev/testing was present.
            _link_or_copy(skill_md, dst)
            skill_count += 1
    if skill_count > 0:
        console.print(f"[green][OK][/green] Installed {skill_count} skills")
    else:
        console.print("[yellow][-][/yellow] No skills found to install")

    # Copy jacked reference doc (comprehensive knowledge for Claude about jacked)
    ref_src = pkg_root / "rules" / "jacked-reference.md"
    ref_dst = home / ".claude" / "jacked-reference.md"
    if ref_src.exists():
        src_content = ref_src.read_text(encoding="utf-8")
        if ref_dst.exists():
            dst_content = ref_dst.read_text(encoding="utf-8")
            if src_content != dst_content:
                shutil.copy(ref_src, ref_dst)
                console.print("[green][OK][/green] Updated jacked reference doc")
        else:
            shutil.copy(ref_src, ref_dst)
            console.print("[green][OK][/green] Installed jacked reference doc")

    # Install agents (symlink for editable, copy otherwise)
    editable = _is_editable_install()
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    agent_count, agent_skipped, agent_method = _install_asset_dir(
        agents_src, agents_dst, "agent", glob_pattern="*.md", force=force
    )
    if agents_src.exists():
        method_label = f" ({agent_method})" if agent_method and editable else ""
        msg = f"[green][OK][/green] Installed {agent_count} agents{method_label}"
        if agent_skipped:
            msg += f" ({agent_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[yellow][-][/yellow] Agents directory not found")

    # Install commands (symlink for editable, copy otherwise)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    cmd_count, cmd_skipped, cmd_method = _install_asset_dir(
        commands_src, commands_dst, "command", glob_pattern="*.md", force=force
    )
    if commands_src.exists():
        method_label = f" ({cmd_method})" if cmd_method and editable else ""
        msg = f"[green][OK][/green] Installed {cmd_count} commands{method_label}"
        if cmd_skipped:
            msg += f" ({cmd_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[yellow][-][/yellow] Commands directory not found")

    # Install lenses (symlink for editable, copy otherwise)
    lenses_src = pkg_root / "lenses"
    lenses_dst = home / ".claude" / "lenses"
    lens_count, lens_skipped, lens_method = _install_asset_dir(
        lenses_src, lenses_dst, "lens", glob_pattern="*.md", force=force
    )
    if lenses_src.exists():
        method_label = f" ({lens_method})" if lens_method and editable else ""
        msg = f"[green][OK][/green] Installed {lens_count} lenses{method_label}"
        if lens_skipped:
            msg += f" ({lens_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[dim][-][/dim] No lenses found to install")

    # Install HTML artifact templates (scaffolds for plans, specs, research,
    # checkpoints). The format preference rule in jacked_behaviors.md points
    # Claude here as the starting point for any human-consumed artifact.
    templates_src = pkg_root / "templates"
    templates_dst = home / ".claude" / "jacked-templates"
    tpl_count, tpl_skipped, tpl_method = _install_asset_dir(
        templates_src, templates_dst, "template", glob_pattern="*.html", force=force
    )
    if templates_src.exists():
        method_label = f" ({tpl_method})" if tpl_method and editable else ""
        msg = f"[green][OK][/green] Installed {tpl_count} HTML templates{method_label}"
        if tpl_skipped:
            msg += f" ({tpl_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[dim][-][/dim] No HTML templates found to install")

    # Install sound hooks if requested
    if sounds:
        _install_sound_hooks(existing, settings_path)

    # Install security gatekeeper (default — skip with --no-security).
    # Hook is wired up but the runtime config defaults to enabled=False so
    # Claude Code's auto permission mode handles approvals until the user
    # explicitly turns the gatekeeper on from the dashboard.
    if install_security:
        _install_security_hook(existing, settings_path)
        console.print(
            "[dim]    Gatekeeper is installed disabled by default. "
            "Toggle it on from Settings > Gatekeeper in the dashboard if you want "
            "LLM-evaluated interception on top of Claude Code's auto mode.[/dim]"
        )
        # Auto-run static permission audit
        console.print("")
        audit_results = _scan_permission_rules()
        if audit_results:
            warns = [r for r in audit_results if r[1] == "WARN"]
            if warns:
                console.print(
                    f"[yellow][AUDIT] Found {len(warns)} dangerous permission wildcard(s):[/yellow]"
                )
                for pat, _, prefix, reason in warns:
                    console.print(f"  [red][WARN][/red] {pat} — {reason}")
                console.print(
                    "[dim]Run 'jacked gatekeeper audit' for full details, "
                    "or 'jacked gatekeeper audit --fix' to prune them interactively.[/dim]"
                )
            else:
                console.print("[green][AUDIT] Permission rules look clean[/green]")
    else:
        console.print(
            "[dim][-][/dim] Skipping security gatekeeper (remove --no-security to enable)"
        )

    # Install session-account tracker hooks (always — lightweight, no deps)
    _install_session_tracker_hook(existing, settings_path)

    # Install QA suggestion hook (always — lightweight, no deps)
    _install_qa_hook(existing, settings_path)

    # Install behavioral rules in CLAUDE.md (default on, --no-rules to skip)
    if not no_rules:
        claude_md_path = home / ".claude" / "CLAUDE.md"
        _install_behavioral_rules(claude_md_path, force=force)

    # Deploy guardrails and hook templates
    from jacked.guardrails import deploy_templates

    deploy_result = deploy_templates(force=force)
    g_count = sum(1 for t in deploy_result["guardrails"] if t.get("deployed"))
    h_count = sum(1 for t in deploy_result["hooks"] if t.get("deployed"))
    g_skip = sum(1 for t in deploy_result["guardrails"] if t.get("skipped"))
    h_skip = sum(1 for t in deploy_result["hooks"] if t.get("skipped"))
    if g_count or h_count:
        console.print(
            f"[green][OK][/green] Deployed {g_count} guardrails + {h_count} hook templates"
        )
    if g_skip or h_skip:
        console.print(
            f"[dim][-][/dim] Skipped {g_skip + h_skip} existing templates (use --force to overwrite)"
        )

    # Install Chrome DevTools MCP server (user-scoped)
    _install_chrome_devtools_mcp(force=force)

    # Auto-install required Claude Code plugins (user-scoped, best-effort)
    _install_required_plugins(force=force)

    # Ensure analytics DB exists
    try:
        from jacked.web.database import Database

        Database()
        console.print("[green][OK][/green] Analytics database ready")
    except Exception:
        console.print("[dim][-][/dim] Analytics database setup skipped")

    # Detect and store project env if we're inside a git repo
    import os as _os

    cwd = _os.getcwd()
    if (Path(cwd) / ".git").is_dir():
        env_path = _detect_project_env()
        if env_path:
            err = _validate_env_path(env_path)
            if err is None:
                if _write_project_env(cwd, env_path):
                    console.print(f"[green][OK][/green] Project env: {env_path}")
                    # Also store in DB if available
                    try:
                        db = Database()
                        db.update_installation_env(cwd, env_path)
                    except Exception:
                        pass
            else:
                console.print(f"[dim][-][/dim] Detected env failed validation: {err}")
        else:
            console.print("[dim][-][/dim] No project env detected")


# Plugins that jacked's behaviors and skills genuinely depend on. Missing
# any of these means key workflows are broken, so install surfaces them as a
# blocker; the full (optional/recommended) list lives in `jacked doctor`.
_REQUIRED_PLUGINS = {
    "superpowers@claude-plugins-official": "brainstorming, planning, TDD, subagent workflows",
    "playwright@claude-plugins-official": "/qa and /ux browser testing",
    "commit-commands@claude-plugins-official": "/commit, /commit-push-pr",
    "code-review@claude-code-plugins": "/code-review multi-agent PR review",
}


def _installed_plugins() -> set[str]:
    """Set of enabled Claude Code plugin ids from settings.json (empty if none)."""
    import json

    settings_path = _jacked_home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            return set(settings.get("enabledPlugins", []))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _warn_required_plugins_missing() -> None:
    """Print one yellow warning per genuinely-required, currently-missing plugin.

    Prints nothing when every required plugin is enabled. This is the only
    plugin nag the `install` command keeps — full recommendations moved to
    `jacked doctor`.
    """
    installed = _installed_plugins()
    missing = [(p, d) for p, d in _REQUIRED_PLUGINS.items() if p not in installed]
    if not missing:
        return
    console.print("")
    for plugin, desc in missing:
        name = plugin.split("@")[0]
        console.print(
            f"[yellow]! Required plugin missing:[/yellow] {name} — {desc} "
            "(enable via /plugins)"
        )


def _install_required_plugins(force: bool = False) -> None:
    """Auto-install jacked's required Claude Code plugins (best-effort).

    Runs ``claude plugin install <id> -s user`` for each required plugin that
    isn't already present in settings.json's enabledPlugins. Idempotent and
    non-fatal: a missing ``claude`` binary, a timeout, or a plugin error just
    prints a warning with the manual fallback — install never aborts over it.
    A plugin the user explicitly disabled (key present but false) is left alone
    unless ``force`` is set.
    """
    import shutil

    if not shutil.which("claude"):
        console.print(
            "[yellow][WARN][/yellow] `claude` CLI not found — skipping plugin "
            "install. Enable required plugins via /plugins."
        )
        return

    installed = _installed_plugins()
    for plugin, desc in _REQUIRED_PLUGINS.items():
        name = plugin.split("@")[0]
        if plugin in installed and not force:
            console.print(f"[dim][-][/dim] Plugin already configured: {name}")
            continue
        result = _run_claude_plugin("install", plugin, "-s", "user")
        if result is None:
            console.print(
                f"[yellow][WARN][/yellow] Could not install {name} — enable via /plugins"
            )
        elif result.returncode == 0:
            console.print(f"[green][OK][/green] Plugin installed: {name} — {desc}")
        else:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            msg = tail[-1] if tail else "unknown error"
            console.print(
                f"[yellow][WARN][/yellow] Plugin install failed for {name}: {msg} "
                "— enable via /plugins"
            )


def _recommend_external_tools():
    """Print recommendations for useful external tools and Claude Code plugins."""
    import json
    import shutil
    import sys

    tools = []
    plugins_needed = []

    # ---------------------------------------------------------------
    # Claude Code plugins — check which are installed
    # ---------------------------------------------------------------
    settings_path = _jacked_home() / ".claude" / "settings.json"
    installed_plugins: set[str] = set()
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            installed_plugins = set(settings.get("enabledPlugins", []))
        except (json.JSONDecodeError, OSError):
            pass

    # Plugins that jacked's behaviors and skills depend on
    required_plugins = dict(_REQUIRED_PLUGINS)

    # Nice-to-have plugins
    optional_plugins = {
        "frontend-design@claude-plugins-official": "UI/UX design quality in code review",
        "code-simplifier@claude-plugins-official": "code simplification agent",
        "claude-md-management@claude-plugins-official": "CLAUDE.md audit and improvement",
    }

    for plugin, desc in required_plugins.items():
        if plugin not in installed_plugins:
            plugins_needed.append((plugin, desc, True))

    for plugin, desc in optional_plugins.items():
        if plugin not in installed_plugins:
            plugins_needed.append((plugin, desc, False))

    if plugins_needed:
        required = [(p, d) for p, d, r in plugins_needed if r]
        optional = [(p, d) for p, d, r in plugins_needed if not r]

        if required:
            console.print("\n[bold]Required Claude Code plugins:[/bold]")
            console.print("  Enable these in Claude Code settings or via /plugins:")
            for plugin, desc in required:
                name = plugin.split("@")[0]
                console.print(f"    {name:30s} — {desc}")

        if optional:
            console.print("\n  Optional plugins:")
            for plugin, desc in optional:
                name = plugin.split("@")[0]
                console.print(f"    {name:30s} — {desc}")

    # ---------------------------------------------------------------
    # External CLI tools
    # ---------------------------------------------------------------
    ab = shutil.which("agent-browser")
    if ab:
        ab_path = Path(ab).resolve()
        has_dogfood = False
        for candidate in [
            ab_path.parent.parent / "libexec" / "lib" / "node_modules" / "agent-browser" / "skills",
            ab_path.parent.parent / "lib" / "node_modules" / "agent-browser" / "skills",
            ab_path.parent / "node_modules" / "agent-browser" / "skills",
        ]:
            if (candidate / "dogfood").exists():
                has_dogfood = True
                break
        if not has_dogfood:
            if sys.platform == "darwin" and shutil.which("brew"):
                tools.append(
                    "  brew upgrade agent-browser                            "
                    "# Update for /dogfood QA skill"
                )
            else:
                tools.append(
                    "  npm install -g agent-browser@latest                   "
                    "# Update for /dogfood QA skill"
                )
    else:
        if sys.platform == "darwin" and shutil.which("brew"):
            tools.append(
                "  brew install agent-browser                             "
                "# Browser QA testing (/dogfood skill)"
            )
        else:
            tools.append(
                "  npm install -g agent-browser                           "
                "# Browser QA testing (/dogfood skill)"
            )

    # Firecrawl CLI — web search & scraping used by jacked skills. We use the
    # CLI, not the (buggy) firecrawl MCP plugin.
    if not shutil.which("firecrawl"):
        tools.append(
            "  npm install -g firecrawl-cli                           "
            "# Web search & scraping (then: firecrawl login)"
        )

    if tools:
        console.print("\nRecommended tools:")
        for t in tools:
            console.print(t)


@main.command()
def doctor():
    """Diagnose a broken jacked install and print recovery commands.

    Checks version, install method, launchd/systemd plist/unit, and
    service running state (via PID + HTTP probe, not just port).
    Prints exact commands to paste for any detected issue.

    Read-only diagnostic — does not attempt any repair.
    """
    import httpx as _httpx
    from jacked import __version__
    from jacked.install_method import detect_install_method
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
    from jacked.service.process import (
        is_port_available, is_process_alive, read_pid,
    )

    console.print(f"[bold]Version:[/bold] {__version__}")
    try:
        method = detect_install_method()
    except Exception as exc:
        method = f"unknown ({exc})"
    console.print(f"[bold]Install method:[/bold] {method}")

    # Plist/unit check
    if sys.platform == "darwin":
        from jacked.service.platform import _get_launchd_plist_path
        plist = _get_launchd_plist_path()
        if plist.exists():
            console.print(f"[bold]Launchd plist:[/bold] [green]OK[/green] ({plist})")
        else:
            console.print("[bold]Launchd plist:[/bold] [yellow]MISSING[/yellow]")
            console.print("  Recovery: [cyan]jacked service install[/cyan]")
    elif sys.platform.startswith("linux"):
        from jacked.service.platform import _get_systemd_user_unit_path
        unit = _get_systemd_user_unit_path()
        if unit.exists():
            console.print(f"[bold]Systemd user unit:[/bold] [green]OK[/green] ({unit})")
        else:
            console.print(
                "[bold]Systemd user unit:[/bold] [yellow]NOT INSTALLED[/yellow]"
            )
            console.print("  Linux users configure their own auto-start; see docs.")
    else:
        console.print("[bold]Native lifecycle manager:[/bold] [dim]none (Windows)[/dim]")

    # Service health — real probes, not just port availability
    port_free = is_port_available(DEFAULT_HOST, DEFAULT_PORT)
    pid_info = read_pid(PID_FILE)
    pid_alive = (
        pid_info is not None
        and is_process_alive(pid_info.get("pid", 0))
    )

    if port_free:
        console.print(
            f"[bold]Service:[/bold] [yellow]NOT RUNNING[/yellow] "
            f"(port {DEFAULT_PORT} free)"
        )
        console.print("  Recovery: [cyan]jacked service start[/cyan]")
        if pid_info and not pid_alive:
            console.print(
                f"  [dim]Stale PID file at {PID_FILE} "
                f"(pid {pid_info.get('pid')} is dead).[/dim]"
            )
    else:
        # Port held — probe HTTP to distinguish healthy vs crashed-mid-init
        try:
            resp = _httpx.get(
                f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/version",
                timeout=2.0,
            )
            if resp.status_code == 200:
                console.print(
                    f"[bold]Service:[/bold] [green]HEALTHY[/green] "
                    f"(port {DEFAULT_PORT}, HTTP 200)"
                )
            else:
                console.print(
                    f"[bold]Service:[/bold] [yellow]PORT HELD BUT UNHEALTHY[/yellow] "
                    f"(HTTP {resp.status_code})"
                )
                console.print("  Recovery: [cyan]jacked service restart[/cyan]")
        except Exception as exc:
            console.print(
                f"[bold]Service:[/bold] [red]PORT HELD BUT UNREACHABLE[/red] "
                f"({type(exc).__name__}: {exc})"
            )
            if pid_alive:
                console.print(
                    f"  PID {pid_info['pid']} is alive but HTTP probe failed — "
                    f"service may have crashed mid-init."
                )
            else:
                console.print(
                    f"  Port held by a process that is NOT the jacked service "
                    f"(our PID file is stale or missing).  "
                    f"Run [cyan]lsof -iTCP:{DEFAULT_PORT} -sTCP:LISTEN[/cyan] "
                    "to see the owner."
                )
            console.print("  Recovery: [cyan]jacked service restart[/cyan]")

    # Install-method-specific recovery
    if method == "editable":
        console.print(
            "\n[bold yellow]Editable (dev-clone) install detected.[/bold yellow]\n"
            "  Auto-upgrade disabled.  Upgrade via:\n"
            "  [cyan]cd <your-repo> && git pull && uv sync[/cyan]"
        )
    elif method == "pip":
        console.print(
            "\n[bold yellow]pip install detected.[/bold yellow]\n"
            "  Auto-upgrade disabled.  Migrate to uv with:\n"
            "  [cyan]uv tool install \"claude-jacked[tray]\" --force[/cyan]"
        )
    elif str(method).startswith("unknown"):
        console.print(
            "\n[bold red]Could not detect install method.[/bold red]\n"
            "  Nuclear-option recovery:\n"
            "  [cyan]uv tool install \"claude-jacked[tray]\" --force[/cyan]"
        )

    # Plugin + external-tool recommendations (moved off the install banner).
    _recommend_external_tools()


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--sounds", is_flag=True, help="Remove only sound hooks")
@click.option("--security", is_flag=True, help="Remove only security gatekeeper hook")
@click.option(
    "--rules", is_flag=True, help="Remove only behavioral rules from CLAUDE.md"
)
def uninstall(yes: bool, sounds: bool, security: bool, rules: bool):
    """Remove jacked hooks, skill, agents, and commands from Claude Code."""
    import json
    import shutil

    home = _jacked_home()
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
        if not click.confirm(
            "Remove jacked from Claude Code? (This won't delete your Qdrant index)"
        ):
            console.print("Cancelled")
            return

    console.print("[bold]Uninstalling Jacked...[/bold]\n")

    # Also remove sound, security, session tracker hooks, and behavioral rules during full uninstall
    _remove_sound_hooks(settings_path)
    _remove_security_hook(settings_path)
    _remove_session_tracker_hooks(settings_path)
    _remove_qa_hook(settings_path)
    _remove_chrome_devtools_mcp()
    claude_md_path = home / ".claude" / "CLAUDE.md"
    if _remove_behavioral_rules(claude_md_path):
        console.print("[green][OK][/green] Removed behavioral rules from CLAUDE.md")

    # Remove Stop hook from settings.json
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if "hooks" in settings and "Stop" in settings["hooks"]:
                # Filter out jacked hooks
                original_count = len(settings["hooks"]["Stop"])
                settings["hooks"]["Stop"] = [
                    h
                    for h in settings["hooks"]["Stop"]
                    if "jacked" not in str(h.get("hooks", []))
                ]
                removed_count = original_count - len(settings["hooks"]["Stop"])
                if removed_count > 0:
                    settings_path.write_text(json.dumps(settings, indent=2))
                    console.print(
                        f"[green][OK][/green] Removed Stop hook from {settings_path}"
                    )
                else:
                    console.print(
                        "[yellow][-][/yellow] No jacked hook found in settings"
                    )
        except (json.JSONDecodeError, KeyError) as e:
            console.print(f"[red][FAIL][/red] Error reading settings: {e}")
    else:
        console.print("[yellow][-][/yellow] No settings.json found")

    # Remove skill directories — iterate all skills/*/SKILL.md in data root
    skills_src_dir = pkg_root / "skills"
    skill_count = 0
    if skills_src_dir.exists():
        for skill_md in skills_src_dir.glob("*/SKILL.md"):
            skill_name = skill_md.parent.name
            skill_dir = home / ".claude" / "skills" / skill_name
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
                skill_count += 1
    if skill_count > 0:
        console.print(f"[green][OK][/green] Removed {skill_count} skills")
    else:
        console.print("[yellow][-][/yellow] No skills found")

    # Remove jacked reference doc
    ref_path = home / ".claude" / "jacked-reference.md"
    if ref_path.exists():
        ref_path.unlink()
        console.print("[green][OK][/green] Removed jacked reference doc")

    # Remove only jacked-installed agents (not the whole directory!)
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    if agents_src.exists() and agents_dst.exists():
        agent_count = 0
        for agent_file in agents_src.glob("*.md"):
            dst_file = agents_dst / agent_file.name
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
                agent_count += 1
        if agent_count > 0:
            console.print(f"[green][OK][/green] Removed {agent_count} agents")
        else:
            console.print("[yellow][-][/yellow] No jacked agents found")
    else:
        console.print("[yellow][-][/yellow] Agents directory not found")

    # Remove only jacked-installed commands (not the whole directory!)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    if commands_src.exists() and commands_dst.exists():
        cmd_count = 0
        for cmd_file in commands_src.glob("*.md"):
            dst_file = commands_dst / cmd_file.name
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
                cmd_count += 1
        if cmd_count > 0:
            console.print(f"[green][OK][/green] Removed {cmd_count} commands")
        else:
            console.print("[yellow][-][/yellow] No jacked commands found")
    else:
        console.print("[yellow][-][/yellow] Commands directory not found")

    # Remove only jacked-installed lenses (not the whole directory!)
    lenses_src = pkg_root / "lenses"
    lenses_dst = home / ".claude" / "lenses"
    if lenses_src.exists() and lenses_dst.exists():
        lens_count = 0
        for lens_file in lenses_src.glob("*.md"):
            dst_file = lenses_dst / lens_file.name
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
                lens_count += 1
        if lens_count > 0:
            console.print(f"[green][OK][/green] Removed {lens_count} lenses")
        else:
            console.print("[yellow][-][/yellow] No jacked lenses found")
    else:
        console.print("[yellow][-][/yellow] Lenses directory not found")

    # Remove only jacked-installed HTML templates (preserve any user-added files)
    templates_src = pkg_root / "templates"
    templates_dst = home / ".claude" / "jacked-templates"
    if templates_src.exists() and templates_dst.exists():
        tpl_count = 0
        for tpl_file in templates_src.glob("*.html"):
            dst_file = templates_dst / tpl_file.name
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
                tpl_count += 1
        if tpl_count > 0:
            console.print(f"[green][OK][/green] Removed {tpl_count} HTML templates")
        # Drop the dir only if it's now empty so user-added templates survive.
        try:
            templates_dst.rmdir()
        except OSError:
            pass

    # Manifest-aware cleanup: remove any artifact jacked recorded but that the
    # current source no longer ships (covers pruned-then-reinstalled history,
    # which the source-glob loops above would miss), then drop the bookkeeping
    # files so a fresh install starts clean.
    from jacked import install_manifest as _mani

    _manifest_path = home / ".claude" / "jacked-manifest.json"
    _prior_manifest = _mani.load(_manifest_path)
    if _prior_manifest:
        # Treat current source as empty so every recorded artifact counts as
        # "removed" and gets pruned from ~/.claude.
        _empty = {cat.key: {} for cat in _mani.CATEGORIES}
        _d = _mani.diff(_prior_manifest, _empty)
        _pruned = _mani.prune_removed(_d, home)
        if _pruned:
            console.print(
                f"[green][OK][/green] Removed {len(_pruned)} manifest-tracked artifacts"
            )
    if _manifest_path.exists():
        _manifest_path.unlink()
    _last_install_path = home / ".claude" / "jacked-last-install.json"
    if _last_install_path.exists():
        _last_install_path.unlink()

    console.print("\n[bold]Uninstall complete![/bold]")
    console.print(
        "\n[dim]Note: Your Qdrant index is still intact. Run 'uv tool uninstall claude-jacked' to fully remove.[/dim]"
    )


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
                    console.print(
                        "[yellow]Prompt is already the built-in default[/yellow]"
                    )
                    return
            except Exception:
                pass
        if not click.confirm("Reset gatekeeper prompt to built-in default?"):
            console.print("Cancelled")
            return

    PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_PATH.write_text(SECURITY_PROMPT, encoding="utf-8")
    console.print("[green][OK][/green] Reset gatekeeper prompt to built-in default")
    console.print(f"[dim]{PROMPT_PATH}[/dim]")


@main.group()
def profiles():
    """Manage security profiles -- export, import, list, delete."""
    pass


@profiles.command(name="list")
def profiles_list():
    """List saved security profiles."""
    from jacked.profiles import PROFILE_DIR_NAME, list_profiles

    profiles_dir = Path.home() / ".claude" / "jacked" / PROFILE_DIR_NAME
    items = list_profiles(profiles_dir)

    if not items:
        console.print("[dim]No saved profiles.[/dim]")
        return

    table = Table(title="Security Profiles", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    table.add_column("Author", style="dim")
    table.add_column("Version", style="dim")
    table.add_column("Created", style="dim")

    for p in items:
        created = p.get("created_at", "")
        if created:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(created)
                created = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        table.add_row(
            p.get("name", "?"),
            p.get("description", ""),
            p.get("author", ""),
            p.get("jacked_version", ""),
            created,
        )

    console.print(table)


@profiles.command(name="export")
@click.argument("name")
@click.option("-d", "--description", default="", help="Profile description")
def profiles_export(name: str, description: str):
    """Export current gatekeeper config + rules as a named profile."""
    import json as _json

    from jacked.profiles import PROFILE_DIR_NAME, export_profile
    from jacked.web.database import Database

    profiles_dir = Path.home() / ".claude" / "jacked" / PROFILE_DIR_NAME

    # Read settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_json: dict = {}
    if settings_path.exists():
        try:
            settings_json = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    try:
        db = Database()
    except Exception as e:
        console.print(f"[red]Cannot open database: {e}[/red]")
        raise SystemExit(1)

    try:
        filepath = export_profile(
            name=name,
            description=description,
            author="",
            db=db,
            settings_json=settings_json,
            profiles_dir=profiles_dir,
        )
        console.print(f"[green][OK][/green] Profile exported: {filepath}")
    except Exception as e:
        console.print(f"[red]Export failed: {e}[/red]")
        raise SystemExit(1)
    finally:
        db.close()


@profiles.command(name="import")
@click.argument("path", type=click.Path(exists=True))
def profiles_import(path: str):
    """Import a profile from a JSON file."""
    import json as _json

    from jacked.profiles import (
        BACKUP_DIR_NAME,
        PROFILE_DIR_NAME,
        import_profile,
        validate_profile,
    )
    from jacked.web.database import Database

    profiles_dir = Path.home() / ".claude" / "jacked" / PROFILE_DIR_NAME
    backup_dir = profiles_dir / BACKUP_DIR_NAME

    # Read profile file
    try:
        profile_data = _json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]Cannot read profile: {e}[/red]")
        raise SystemExit(1)

    # Validate
    try:
        warnings = validate_profile(profile_data)
    except ValueError as e:
        console.print(f"[red]Validation failed: {e}[/red]")
        raise SystemExit(1)

    if warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(f"  [yellow]- {w}[/yellow]")

    if not click.confirm("Apply this profile?"):
        console.print("Cancelled")
        return

    # Read settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_json: dict = {}
    if settings_path.exists():
        try:
            settings_json = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _write_settings(data: dict):
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(settings_path)

    try:
        db = Database()
    except Exception as e:
        console.print(f"[red]Cannot open database: {e}[/red]")
        raise SystemExit(1)

    try:
        backup_path, import_warnings = import_profile(
            profile_data=profile_data,
            db=db,
            settings_json=settings_json,
            write_settings_fn=_write_settings,
            profiles_dir=profiles_dir,
            backup_dir=backup_dir,
        )
        console.print("[green][OK][/green] Profile imported!")
        console.print(f"[dim]Backup saved to: {backup_path}[/dim]")
    except Exception as e:
        console.print(f"[red]Import failed: {e}[/red]")
        raise SystemExit(1)
    finally:
        db.close()


@profiles.command(name="delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def profiles_delete(name: str, yes: bool):
    """Delete a saved profile."""
    from jacked.profiles import PROFILE_DIR_NAME, delete_profile

    profiles_dir = Path.home() / ".claude" / "jacked" / PROFILE_DIR_NAME

    if not yes:
        if not click.confirm(f'Delete profile "{name}"?'):
            console.print("Cancelled")
            return

    deleted = delete_profile(name, profiles_dir)
    if deleted:
        console.print(f"[green][OK][/green] Profile '{name}' deleted")
    else:
        console.print(f"[yellow]Profile '{name}' not found[/yellow]")


@main.group()
def service():
    """Manage the jacked background service (tray icon + auto-start)."""
    pass


@service.command(name="start")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
def service_start(host: str | None, port: int | None):
    """Start jacked as a background service with system tray icon."""
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT
    from jacked.service.tray import ServiceRunner

    runner = ServiceRunner(host=host or DEFAULT_HOST, port=port or DEFAULT_PORT)
    runner.run()


@service.command(name="stop")
def service_stop():
    """Stop the running jacked service.

    Uses stop_process_graceful which waits for actual PID death and
    escalates to SIGKILL if SIGTERM is ignored — pystray's AppKit
    runloop on macOS can silently swallow Python signals.
    """
    from jacked.service import PID_FILE
    from jacked.service.process import stop_process_graceful

    result = stop_process_graceful(PID_FILE)
    if not result["was_running"]:
        console.print("[yellow]Service is not running[/yellow]")
        return

    if not result["died"]:
        console.print("[red]Could not stop service — still alive after SIGKILL[/red]")
        sys.exit(1)

    if result["killed"]:
        console.print("[yellow][OK][/yellow] Service ignored SIGTERM — force-killed")
    else:
        console.print("[green][OK][/green] Stopped jacked service")


@service.command(name="restart")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
@click.option(
    "--foreground",
    is_flag=True,
    help="Run the new service in the foreground (default: detach and return immediately).",
)
def service_restart(host: str | None, port: int | None, foreground: bool):
    """Restart the jacked service.

    By default, runs the NEW service detached — this command returns
    immediately and tray logs go to ~/.claude/jacked-service.log. This
    lets `jacked upgrade` and other automation call us without blocking
    on the pystray event loop.

    Use --foreground to run interactively (tray logs to your terminal).
    """
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
    from jacked.service.platform import ensure_native_lifecycle, native_restart
    from jacked.service.process import (
        stop_process_graceful,
        wait_for_port_free,
    )

    the_port = port or DEFAULT_PORT
    the_host = host or DEFAULT_HOST

    # Preferred path: make sure native lifecycle (launchd plist / systemd
    # unit) is configured, then delegate.  Skip kickstart when the plist
    # was just installed — RunAtLoad already started the service fresh
    # and kickstart would race the boot.
    # `--foreground` is an explicit debug path — skip native handoff.
    if not foreground:
        ok_ens, state, reason_ens = ensure_native_lifecycle()
        if ok_ens:
            if state == "just_installed":
                console.print(f"[green][OK][/green] {reason_ens}")
                return
            # already_installed → run native_restart for atomic kickstart
            ok, reason = native_restart()
            if ok:
                console.print(f"[green][OK][/green] {reason}")
                return
            console.print(f"[yellow]native_restart failed: {reason}[/yellow]")
        else:
            console.print(f"[dim]native lifecycle unavailable: {reason_ens}[/dim]")

    # 1. Stop any running service. stop_process_graceful waits for actual PID
    # death and escalates to SIGKILL if SIGTERM is ignored (pystray's AppKit
    # runloop can swallow signals until it yields to Python).
    result = stop_process_graceful(PID_FILE)
    if result["was_running"]:
        if result["killed"]:
            console.print("[yellow]Tray ignored SIGTERM — force-killed[/yellow]")
        elif result["died"]:
            console.print("[dim]Stopped existing service[/dim]")
        if not result["died"]:
            console.print("[red]Could not stop existing service — aborting restart[/red]")
            sys.exit(1)
        # Port can linger a beat after the PID dies.
        if not wait_for_port_free(the_host, the_port, timeout=10.0):
            console.print(f"[red]Port {the_port} still in use — aborting start[/red]")
            sys.exit(1)

    # 2. Start the new service.
    if foreground:
        from jacked.service.tray import ServiceRunner
        ServiceRunner(host=the_host, port=the_port).run()
        return

    # Detached — the tray must survive this command returning.
    log_path = _spawn_service_detached(the_host, the_port)

    console.print(f"[green][OK][/green] Started jacked service (detached) on :{the_port}")
    console.print(f"[dim]Logs: {log_path}[/dim]")


@service.command(name="status")
def service_status():
    """Show whether the jacked service is running."""
    from jacked.service import PID_FILE
    from jacked.service.process import read_pid, is_process_alive
    from jacked.service.platform import detect_autostart

    info = read_pid(PID_FILE)
    autostart = detect_autostart()
    autostart_label = "[green]enabled[/green]" if autostart else "[dim]disabled[/dim]"

    if info and is_process_alive(info["pid"]):
        import time
        pid_mtime = PID_FILE.stat().st_mtime
        uptime_secs = time.time() - pid_mtime
        hours, remainder = divmod(int(uptime_secs), 3600)
        minutes, _ = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m" if hours else f"{minutes}m"

        console.print("[bold green]Jacked Service: running[/bold green]")
        console.print(f"  PID:       {info['pid']}")
        console.print(f"  Port:      {info['port']}")
        console.print(f"  Uptime:    {uptime}")
        console.print(f"  Autostart: {autostart_label}")
        console.print(f"  Dashboard: http://127.0.0.1:{info['port']}")
    else:
        console.print("[bold yellow]Jacked Service: stopped[/bold yellow]")
        console.print(f"  Autostart: {autostart_label}")
        if info:
            from jacked.service.process import remove_pid
            remove_pid(PID_FILE)


@service.command(name="install")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
def service_install(host: str | None, port: int | None):
    """Configure jacked to start automatically on login."""
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT
    from jacked.service.platform import install_autostart

    result = install_autostart(host or DEFAULT_HOST, port or DEFAULT_PORT)
    if result.startswith("Could not find"):
        console.print(f"[red]Error:[/red] {result}")
    else:
        console.print(f"[green][OK][/green] {result}")


@service.command(name="uninstall")
def service_uninstall():
    """Remove jacked auto-start configuration."""
    from jacked.service.platform import uninstall_autostart

    result = uninstall_autostart()
    if "not supported" in result.lower() or "not found" in result.lower():
        console.print(f"[yellow]{result}[/yellow]")
    else:
        console.print(f"[green][OK][/green] {result}")


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
    "uv": "uv run executes arbitrary code, uv tool install runs arbitrary packages",
    "nc": "raw network connections",
    "ncat": "raw network connections",
    "netcat": "raw network connections",
}

MEDIUM_RISK_PREFIXES = {
    "cat": "deny patterns cover sensitive files, but not all",
}

# Prefixes that are always low-risk and get [OK]
LOW_RISK_PREFIXES = {
    "git",
    "gh",
    "grep",
    "rg",
    "find",
    "fd",
    "ls",
    "dir",
    "pwd",
    "echo",
    "which",
    "where",
    "env",
    "printenv",
    "npm",
    "pip",
    "pytest",
    "make",
    "cargo",
    "go",
    "docker",
    "jacked",
    "claude",
    "npx",
    "tsc",
    "ruff",
    "flake8",
    "pylint",
    "mypy",
    "eslint",
    "prettier",
    "black",
    "isort",
    "jest",
    "conda",
    "pipx",
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


def _settings_files_to_search() -> list[Path]:
    """All settings.json files where permission rules may live."""
    return [
        Path.home() / ".claude" / "settings.json",
        Path(".claude") / "settings.json",
        Path(".claude") / "settings.local.json",
    ]


def _remove_permission_patterns(
    settings_path: Path, patterns_to_remove: set[str]
) -> tuple[int, list[str]]:
    """Remove matching Bash permission wildcards from a settings.json file.

    Writes atomically with a timestamped backup. Returns (removed_count,
    actually_removed_list). No-op if the file doesn't exist.
    """
    import json as _json

    if not settings_path.exists():
        return 0, []
    try:
        raw = _json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return 0, []

    perms = raw.get("permissions") or {}
    allow = perms.get("allow") or []
    if not isinstance(allow, list):
        return 0, []

    # Snapshot before mutation.
    try:
        _snapshot_settings(settings_path)
        _rotate_backups(settings_path.parent, prefix=f"{settings_path.name}.bak-", keep=5)
    except Exception:
        pass

    kept = []
    removed_list = []
    for entry in allow:
        if isinstance(entry, str) and entry in patterns_to_remove:
            removed_list.append(entry)
            continue
        kept.append(entry)

    if not removed_list:
        return 0, []

    raw.setdefault("permissions", {})["allow"] = kept
    _write_settings_atomic(settings_path, raw)
    return len(removed_list), removed_list


def _prune_dangerous_permissions(
    patterns: set[str], interactive: bool = True
) -> tuple[int, list[tuple[Path, list[str]]]]:
    """Remove each given pattern from whichever settings.json contains it.

    Returns (total_removed, per-file-results).
    """
    per_file: list[tuple[Path, list[str]]] = []
    total = 0
    for settings_path in _settings_files_to_search():
        count, removed = _remove_permission_patterns(settings_path, patterns)
        if count > 0:
            per_file.append((settings_path, removed))
            total += count
    return total, per_file


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
@click.option(
    "--log",
    "scan_log",
    is_flag=True,
    help="Also scan recent auto-approved commands via LLM",
)
@click.option("--limit", "-n", default=50, help="Number of recent log entries to scan")
@click.option(
    "--fix",
    is_flag=True,
    help="Interactively remove dangerous permission wildcards. Pairs with --yes for non-interactive prune.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="With --fix, remove all dangerous wildcards without confirmation.",
)
def gatekeeper_audit(scan_log, limit, fix, yes):
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
        console.print(
            "[dim]Permission rules are set via Claude Code's /permissions command[/dim]"
        )
        return

    warn_count = 0
    info_count = 0
    ok_count = 0

    for pat, level, prefix, reason in results:
        if level == "WARN":
            console.print(f"  [red][WARN][/red] {pat} — {reason}")
            console.print(
                f"         Gatekeeper deny patterns won't catch all {prefix} inline code."
            )
            console.print(
                "         Consider removing and letting the gatekeeper evaluate individually.\n"
            )
            warn_count += 1
        elif level == "INFO":
            console.print(f"  [yellow][INFO][/yellow] {pat} — {reason}")
            info_count += 1
        else:
            console.print(f"  [green][OK][/green] {pat} — {reason}")
            ok_count += 1

    console.print(f"\n{warn_count} warnings, {info_count} info, {ok_count} OK")

    if warn_count > 0 and not fix:
        console.print(
            "\n[yellow]TIP: Remove dangerous wildcards and let the gatekeeper LLM evaluate them individually.[/yellow]"
        )
        console.print(
            "[dim]Run 'jacked gatekeeper audit --fix' to prune them interactively.[/dim]"
        )

    # --fix: interactive prune of dangerous wildcards
    if fix:
        warn_patterns = {pat for pat, level, _, _ in results if level == "WARN"}
        if not warn_patterns:
            console.print(
                "\n[green]Nothing to fix — no dangerous wildcards found.[/green]"
            )
        else:
            console.print("")  # spacer
            to_remove: set[str] = set()

            if yes:
                to_remove = set(warn_patterns)
                console.print(
                    f"[yellow]--yes: will remove all {len(to_remove)} dangerous wildcard(s).[/yellow]"
                )
            else:
                console.print(
                    "[bold]For each dangerous wildcard, choose: [y]es remove / [n]o keep / [a]ll remove / [q]uit[/bold]\n"
                )
                remove_all = False
                for pat in sorted(warn_patterns):
                    if remove_all:
                        to_remove.add(pat)
                        continue
                    choice = click.prompt(
                        f"Remove {pat}? [y/n/a/q]",
                        type=click.Choice(["y", "n", "a", "q"], case_sensitive=False),
                        default="n",
                        show_default=False,
                    ).lower()
                    if choice == "y":
                        to_remove.add(pat)
                    elif choice == "a":
                        to_remove.add(pat)
                        remove_all = True
                    elif choice == "q":
                        break

            if not to_remove:
                console.print("\n[dim]No changes made.[/dim]")
            else:
                total, per_file = _prune_dangerous_permissions(to_remove)
                if total == 0:
                    console.print(
                        "\n[yellow]Selected patterns not found in any settings file — nothing to remove.[/yellow]"
                    )
                else:
                    console.print(
                        f"\n[green][OK][/green] Removed {total} wildcard(s) across {len(per_file)} file(s):"
                    )
                    for settings_path, removed in per_file:
                        console.print(f"  [dim]{settings_path}[/dim]")
                        for pat in removed:
                            console.print(f"    - {pat}")
                    console.print(
                        "[dim]Backups saved next to each modified file as <name>.bak-YYYYMMDD-HHMMSS.[/dim]"
                    )
                    console.print(
                        "[dim]Gatekeeper will now evaluate these commands via LLM on each use.[/dim]"
                    )

    # Log scanning
    if scan_log:
        console.print(
            f"\n[bold]Scanning last {limit} auto-approved commands from hooks-debug.log...[/bold]\n"
        )

        commands = _parse_log_for_perms_commands(LOG_PATH, limit=limit)
        if not commands:
            console.print("[yellow]No PERMS MATCH entries found in log[/yellow]")
            console.print(f"[dim]Log path: {LOG_PATH}[/dim]")
            return

        # Send to LLM for evaluation
        cmd_list = "\n".join(f"  {i + 1}. {cmd}" for i, cmd in enumerate(commands))
        audit_prompt = f"""You are a security auditor. Review these {len(commands)} Bash commands that were auto-approved via permission rules (bypassing LLM evaluation).

Flag any that look dangerous — data exfiltration, destructive operations, arbitrary code execution, secret access, etc. Most will be safe.

Commands:
{cmd_list}

Respond with ONLY a JSON object:
{{"flagged": [{{"index": 1, "command": "the command", "reason": "brief reason"}}], "safe_count": N}}

If all are safe, return: {{"flagged": [], "safe_count": {len(commands)}}}"""

        console.print(
            f"[dim]Sending {len(commands)} commands to LLM for review...[/dim]"
        )

        try:
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                console.print(
                    "[red]ANTHROPIC_API_KEY not set — cannot run LLM audit[/red]"
                )
                console.print(
                    "[dim]Set ANTHROPIC_API_KEY or install anthropic SDK[/dim]"
                )
                return

            # Use configured model from gatekeeper settings if available
            audit_model = "claude-haiku-4-5-20251001"
            try:
                import sys as _sys

                _gk_dir = str(Path(__file__).resolve().parent / "data" / "hooks")
                if _gk_dir not in _sys.path:
                    _sys.path.insert(0, _gk_dir)
                from security_gatekeeper import _read_gatekeeper_config

                gk_config = _read_gatekeeper_config()
                audit_model = gk_config["model"]
                api_key = gk_config["api_key"] or api_key
            except Exception:
                pass

            client = anthropic.Anthropic(api_key=api_key, timeout=30.0)
            response = client.messages.create(
                model=audit_model,
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
                console.print(
                    f"[red]{len(flagged)} commands flagged[/red] — consider tightening your permission rules."
                )
            else:
                console.print("[green]No dangerous commands found.[/green]")

        except ImportError:
            console.print(
                "[red]anthropic SDK not installed — cannot run LLM audit[/red]"
            )
            console.print(
                '[dim]Activate it: jacked install --force[/dim]'
            )
        except json.JSONDecodeError:
            console.print(
                f"[yellow]LLM returned non-JSON response:[/yellow] {text[:200]}"
            )
        except Exception as e:
            console.print(f"[red]LLM audit failed:[/red] {e}")

    # Show counter info
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            count = state.get("perms_count", 0)
            if count > 0:
                console.print(
                    f"\n[dim]Total permission auto-approvals since last reset: {count}[/dim]"
                )
        except Exception:
            pass


@gatekeeper.command(name="diff")
def gatekeeper_diff():
    """Show diff between custom prompt and built-in default."""
    import difflib
    from jacked.data.hooks.security_gatekeeper import SECURITY_PROMPT, PROMPT_PATH

    if not PROMPT_PATH.exists():
        console.print(
            "[yellow]No custom prompt file found — using built-in default[/yellow]"
        )
        console.print(f"[dim]Create one at: {PROMPT_PATH}[/dim]")
        return

    try:
        custom = PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        console.print(f"[red]Error reading prompt file:[/red] {e}")
        return

    if custom.strip() == SECURITY_PROMPT.strip():
        console.print(
            "[green]No differences — custom prompt matches built-in default[/green]"
        )
        return

    diff = difflib.unified_diff(
        SECURITY_PROMPT.splitlines(keepends=True),
        custom.splitlines(keepends=True),
        fromfile="built-in",
        tofile=str(PROMPT_PATH),
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            console.print(f"[green]{line.rstrip()}[/green]")
        elif line.startswith("-") and not line.startswith("---"):
            console.print(f"[red]{line.rstrip()}[/red]")
        else:
            console.print(line.rstrip())


# ── Guardrails CLI group ──────────────────────────────────────────────


@main.group(name="guardrails")
def guardrails_group():
    """Manage design guardrails for projects."""
    pass


@guardrails_group.command(name="init")
@click.option(
    "--repo", type=click.Path(exists=True), default=".", help="Project root directory"
)
@click.option(
    "--language",
    type=click.Choice(["python", "node", "rust", "go"]),
    help="Override language detection",
)
@click.option(
    "--force", "-f", is_flag=True, help="Overwrite existing JACKED_GUARDRAILS.md"
)
def guardrails_init(repo: str, language: str, force: bool):
    """Create JACKED_GUARDRAILS.md in a project from templates.

    Auto-detects language from pyproject.toml, package.json, etc.

    >>> # CLI command: jacked guardrails init
    """
    from jacked.guardrails import create_guardrails

    result = create_guardrails(repo, language=language, force=force)
    if result["created"]:
        lang_label = (
            f" ({result.get('language', 'base')})" if result.get("language") else ""
        )
        console.print(f"[green][OK][/green] Created {result['path']}{lang_label}")
    else:
        console.print(f"[yellow][-][/yellow] {result['reason']}")


# ── Lint-Hook CLI group ──────────────────────────────────────────────


@main.group(name="lint-hook")
def lint_hook_group():
    """Manage git pre-push lint hooks for projects."""
    pass


@lint_hook_group.command(name="init")
@click.option(
    "--repo", type=click.Path(exists=True), default=".", help="Project root directory"
)
@click.option(
    "--language",
    type=click.Choice(["python", "node", "rust", "go"]),
    help="Override language detection",
)
@click.option("--force", "-f", is_flag=True, help="Overwrite existing pre-push hook")
def lint_hook_init(repo: str, language: str, force: bool):
    """Install a pre-push lint hook in a project's .git/hooks/.

    Auto-detects language and installs the appropriate linter check.

    >>> # CLI command: jacked lint-hook init
    """
    from jacked.guardrails import install_hook

    result = install_hook(repo, language=language, force=force)
    if result["installed"]:
        console.print(
            f"[green][OK][/green] Installed pre-push hook at {result['path']} ({result.get('language', '?')})"
        )
        # Store project env so the hook can find the right tool
        repo_path = str(Path(repo).resolve())
        env_path = _detect_project_env()
        if env_path and _validate_env_path(env_path) is None:
            if _write_project_env(repo_path, env_path):
                console.print(f"[green][OK][/green] Project env: {env_path}")
    else:
        console.print(f"[yellow][-][/yellow] {result['reason']}")


# ── Launch Claude Code with per-account isolation ────────────────────


@main.command(name="claude", context_settings={"ignore_unknown_options": True})
@click.argument("account", required=False)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def claude_cmd(account, claude_args):
    """Launch Claude Code with per-account credential isolation.

    ACCOUNT can be an integer ID or email address. If omitted, uses
    the currently active account (set via dashboard "Use" button).

    All additional arguments are passed through to claude.

    Examples:
        jacked claude 2
        jacked claude alice@test.com
        jacked claude 2 -p editor

    >>> # CLI command: jacked claude [ACCOUNT] [CLAUDE_ARGS...]
    """
    from jacked.launch import launch_claude, prepare_account_dir, resolve_account
    from jacked.web.database import Database

    db_path = Path.home() / ".claude" / "jacked.db"
    if not db_path.exists():
        raise click.ClickException(
            "jacked database not found. Run 'jacked webux' first to initialize."
        )

    # If account looks like a Claude CLI flag (e.g. --resume, -p),
    # prepend it back to claude_args and resolve the active account instead.
    if account is not None and account.startswith("-"):
        claude_args = (account,) + tuple(claude_args)
        account = None

    db = Database(str(db_path))
    try:
        # Parse account ref: try int first, else string (email or None)
        account_ref = None
        if account is not None:
            try:
                account_ref = int(account)
            except ValueError:
                account_ref = account

        acct = resolve_account(account_ref, db)
        config_dir = prepare_account_dir(acct, db)
        console.print(
            f"Launching Claude Code as [bold]{acct['email']}[/bold] (account {acct['id']})..."
        )
    finally:
        db.close()

    # Strip leading "claude" if user pasted full `claude --resume ...` after the command
    if claude_args and claude_args[0] == "claude":
        claude_args = claude_args[1:]

    launch_claude(config_dir, claude_args, db_path=str(db_path))


# ── Convenience init command ─────────────────────────────────────────


@main.command(name="init")
@click.option(
    "--repo", type=click.Path(exists=True), default=".", help="Project root directory"
)
@click.option(
    "--language",
    type=click.Choice(["python", "node", "rust", "go"]),
    help="Override language detection",
)
@click.option("--force", "-f", is_flag=True, help="Overwrite existing files")
def init_project(repo: str, language: str, force: bool):
    """Set up guardrails + lint hook in a project (does both).

    Combines 'jacked guardrails init' + 'jacked lint-hook init'.

    >>> # CLI command: jacked init
    """
    from jacked.guardrails import create_guardrails, install_hook

    console.print(f"[bold]Setting up project: {repo}[/bold]\n")

    # Guardrails
    g_result = create_guardrails(repo, language=language, force=force)
    if g_result["created"]:
        lang_label = (
            f" ({g_result.get('language', 'base')})" if g_result.get("language") else ""
        )
        console.print(f"[green][OK][/green] Created JACKED_GUARDRAILS.md{lang_label}")
    else:
        console.print(f"[yellow][-][/yellow] Guardrails: {g_result['reason']}")

    # Lint hook
    h_result = install_hook(repo, language=language, force=force)
    if h_result["installed"]:
        console.print(
            f"[green][OK][/green] Installed pre-push lint hook ({h_result.get('language', '?')})"
        )
    else:
        console.print(f"[yellow][-][/yellow] Lint hook: {h_result['reason']}")

    # Store project env for hook tool discovery
    repo_path = str(Path(repo).resolve())
    env_path = _detect_project_env()
    if env_path and _validate_env_path(env_path) is None:
        if _write_project_env(repo_path, env_path):
            console.print(f"[green][OK][/green] Project env: {env_path}")

    console.print("\n[bold]Done.[/bold]")


if __name__ == "__main__":
    main()
