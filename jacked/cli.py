"""
CLI for Jacked.

Provides command-line interface for indexing, searching, and
retrieving Claude Code sessions.
"""

import os
import shutil
import subprocess
import re
import sys
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

# The only eager jacked imports in this file: click.Choice needs these values at
# decoration time (see the `jacked dcr engine set` command), and
# _wait_owned_service_ready's default budget is evaluated at import time.
# jacked.dcr_settings is stdlib-only at import time, and jacked.service is
# stdlib-only at import time too, so this costs nothing at startup.
from jacked.dcr_settings import EFFORT_CHOICES as _DCR_EFFORT_CHOICES
from jacked.dcr_settings import ENGINE_CHOICES as _DCR_ENGINE_CHOICES
from jacked.service import REPLACEMENT_READY_TIMEOUT


# Windows legacy consoles (cp1252 / cp437 OEM) can't encode glyphs like → or −;
# without this, ANY jacked subcommand that prints one dies with
# UnicodeEncodeError. That crash silently aborted the tray-update batch (a
# `jacked _update_status`/`jacked install --force` step exits non-zero, the
# batch's DRIFT_GUARD bails, and the service is never restarted) and made
# `jacked install` look failed. Degrade unencodable chars to a placeholder
# instead of raising — ASCII output is unaffected. Belt-and-suspenders with the
# ASCII-only install summary; this also covers any stray glyph elsewhere.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

console = Console()

# Extras names are package extras: a bare identifier list, never shell text.
_EXTRAS_NAME_RE = re.compile(r"[A-Za-z0-9_.,-]{0,64}")
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Configure logging based on verbosity."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


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


@click.group(invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx, verbose: bool):
    """Jacked - Cross-machine context for Claude Code sessions."""
    # Idempotent, and already done by `python -m jacked`. Repeated here because
    # the `jacked.exe` console-script entry point skips __main__.py entirely,
    # and a GUI-spawned exe can also land here with None std streams.
    from jacked.headless import ensure_std_streams

    ensure_std_streams()
    setup_logging(verbose)
    # First-run nudge: `pip`/`uv tool install` run no code, so this is the
    # earliest point we can tell a user the install isn't finished. Loud banner
    # + offer to run `jacked install` when it hasn't been wired up yet.
    _maybe_prompt_first_run(ctx)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit()


def _already_installed() -> bool:
    """True once `jacked install` has run at least once (manifest present)."""
    return (_jacked_home() / ".claude" / "jacked-manifest.json").exists()


def _is_headless() -> bool:
    """True when there's no GUI display to draw a tray icon on.

    macOS/Windows always have a window server. On Linux/BSD a tray needs X11 or
    Wayland; absent both (CI, servers, Docker, SSH) we skip the icon and just
    run the service."""
    import os

    if sys.platform in ("darwin", "win32"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _is_interactive_tty() -> bool:
    """True only when a real human terminal is on both stdin and stdout.

    Must never raise: this runs in the top-level ``main()`` callback, so an
    exception here kills EVERY subcommand before it dispatches.

    Under Windows' console-less ``pythonw.exe`` — which is exactly what the
    login autostart VBS uses to launch ``-m jacked service start`` — both
    ``sys.stdin`` and ``sys.stdout`` are ``None``, so the old
    ``sys.stdin.isatty()`` raised AttributeError and took the tray icon down
    with it, silently, on every boot (there is no console to print the
    traceback to). Streams can also be closed or replaced by objects with no
    ``isatty`` under test runners and hook shims, hence the broad guard.
    """
    for stream in (sys.stdin, sys.stdout):
        if stream is None:
            return False
        try:
            if not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            # ValueError: I/O operation on closed file.
            return False
    return True


def _maybe_prompt_first_run(ctx) -> None:
    """When jacked is on disk but `jacked install` has never run, show a loud
    banner and (interactively) offer to run it now.

    Fires only for a human at an interactive terminal — never for the hook /
    update-status shims (names starting with ``_``), for ``install`` itself, or
    for any non-TTY caller (scripts, CI, Claude Code hooks, the test runner), so
    it can't corrupt automated stdout/stderr."""
    sub = ctx.invoked_subcommand
    if sub and (sub.startswith("_") or sub == "install"):
        return
    if not _is_interactive_tty():
        return
    if _already_installed():
        return

    from rich.text import Text

    msg = Text()
    msg.append("Jacked is installed but NOT wired into Claude Code yet.\n\n", style="bold yellow")
    msg.append("Run this ONE command to deploy the skills, commands, agents,\n", style="white")
    msg.append("hooks, and tray icon:\n\n", style="white")
    msg.append("    jacked install\n", style="bold cyan")
    console.print(
        Panel(
            msg,
            title="[bold white on red]  ⚠  ONE MORE STEP — RUN `jacked install`  ⚠  [/]",
            border_style="bold red",
            expand=False,
        )
    )
    try:
        if click.confirm("Run `jacked install` now?", default=True):
            ctx.invoke(install)
            if sub is None:
                ctx.exit(0)
    except (click.Abort, EOFError, KeyboardInterrupt):
        console.print("[dim]No problem — run `jacked install` whenever you're ready.[/dim]")


@main.command()
@click.option("--cwd", default=None, help="Working directory to recover (default: current dir)")
@click.option("--exclude", default=None, help="Session id to exclude (the live one)")
@click.option("--session", "session_id", default=None, help="Recover this specific session id")
@click.option("--digest", "as_digest", is_flag=True, help="Emit the working-state digest for --session")
@click.option("--limit", "-n", default=3, help="How many candidates to list")
@click.option("--depth", type=click.Choice(["brief", "standard", "full"]), default="standard",
              help="Digest detail level (scales message/action/file caps + char budget)")
@click.option("--budget", default=None, type=int, help="Override the digest char budget (defaults to --depth's budget)")
@click.option("--json", "as_json", is_flag=True, help="Emit candidates as JSON")
def recover(cwd, exclude, session_id, as_digest, limit, depth, budget, as_json):
    """Recover a crashed session for this folder from its on-disk transcript.

    Works on a bare install.
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
        prof = rec.DEPTH_PROFILES.get(depth, rec.DEPTH_PROFILES["standard"])
        effective_budget = budget if budget is not None else prof["budget"]
        digest = rec.build_digest(session_path, depth=depth)
        click.echo(rec.render_digest(digest, budget_chars=effective_budget))
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


@main.command(name="webux")
@click.option("--host", default=None, help="Host to bind (one-shot override; default: the dashboard Remote access setting, else 127.0.0.1)")
@click.option("--port", default=8321, type=int, help="Port to bind to")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
@click.option("--reload", is_flag=True, help="Auto-reload on file changes (dev mode); ignores the Remote access setting and binds a single host (the reloader subprocess can't inherit pre-bound sockets)")
def webux(host: str | None, port: int, no_browser: bool, reload: bool):
    """Start the jacked web dashboard."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error:[/red] webux requires the web extra.")
        console.print("Install it with:")
        console.print(r'  [bold]uv tool install "claude-jacked\[web]" --force[/bold]')
        sys.exit(1)

    import os as _os

    # --reload stays on the single-host uvicorn.run path: uvicorn's reloader
    # re-execs a child process that cannot inherit our pre-bound sockets, so the
    # BindPlan / Remote access setting does not apply. An explicit --host still
    # overrides; otherwise dev binds loopback.
    if reload:
        dev_host = host or "127.0.0.1"
        _os.environ["JACKED_HOST"] = dev_host
        _os.environ["JACKED_PORT"] = str(port)
        url = f"http://{dev_host}:{port}"
        console.print(f"[bold]Starting jacked dashboard at {url}[/bold]")
        console.print(
            "[dim]Auto-reload enabled, watching for file changes "
            "(Remote access setting ignored)[/dim]"
        )
        if not no_browser:
            import webbrowser

            webbrowser.open(url)
        uvicorn.run(
            "jacked.api.main:app",
            host=dev_host,
            port=port,
            reload=True,
            reload_dirs=["jacked"],
        )
        return

    # Normal path: resolve the bind plan (explicit --host > DB setting >
    # loopback), pre-bind its sockets, and hand them to uvicorn. JACKED_HOST
    # (dynamic CORS / WebSocket origin / CSRF) comes from the plan's primary host.
    from jacked.service.bind import create_sockets, resolve_bind, set_active_plan

    plan = resolve_bind(host, port)
    # Publish the live plan so the settings API reports the real effective bind.
    set_active_plan(plan)
    _os.environ["JACKED_HOST"] = plan.primary_host
    _os.environ["JACKED_PORT"] = str(port)

    try:
        socks = create_sockets(plan)
    except OSError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        console.print(f"Is another process already using port {port}?")
        sys.exit(1)

    # Loopback reaches every plan except a cli-pinned specific IP; show that as
    # the primary URL, then list the other bound addresses.
    if plan.mode in ("loopback", "tailscale", "all"):
        primary_url = f"http://127.0.0.1:{port}"
    else:
        primary_url = f"http://{plan.primary_host}:{port}"
    console.print(f"[bold]Starting jacked dashboard at {primary_url}[/bold]")
    if plan.mode == "all":
        console.print(
            f"[dim]Bound on all interfaces (0.0.0.0:{port}), reachable from the LAN[/dim]"
        )
    if plan.tailscale_ip:
        console.print(f"[dim]Tailscale: http://{plan.tailscale_ip}:{port}[/dim]")
    if plan.fallback_reason:
        console.print(f"[yellow]{plan.fallback_reason}[/yellow]")

    if not no_browser:
        import webbrowser

        webbrowser.open(primary_url)

    config = uvicorn.Config(
        "jacked.api.main:app",
        host=plan.primary_host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run(sockets=socks)


def _wait_owned_service_ready(
    paths,
    timeout: float = REPLACEMENT_READY_TIMEOUT,
    *,
    expected_generation: str | None = None,
    expected_build: str | None = None,
    previous_instance: str | None = None,
) -> dict | None:
    """Wait for a manifest-authenticated running service and return its status."""
    import time as _time

    from jacked.service.ipc import ControlAction, send_native_control

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            response = send_native_control(paths.manifest, ControlAction.STATUS)
            status = response.get("result", {})
            if (
                response.get("ok")
                and isinstance(status, dict)
                and status.get("state") == "running"
                and (
                    expected_generation is None
                    or status.get("generation") == expected_generation
                )
                and (
                    expected_build is None
                    or status.get("build_version") == expected_build
                )
                and (
                    previous_instance is None
                    or status.get("instance_id") != previous_instance
                )
            ):
                return status
        except (OSError, ValueError):
            pass
        _time.sleep(0.25)
    return None


def _clear_start_failure_breaker(paths) -> None:
    """Let an explicit start or restart retry after the give-up breaker fired.

    A supervised service exits cleanly once repeated starts fail inside the
    window, and only an operator gesture is allowed to re-arm it.
    """
    from jacked.service import START_FAILURE_FILENAME
    from jacked.service.start_failures import clear_start_failures

    try:
        clear_start_failures(paths.root / START_FAILURE_FILENAME)
    except OSError:
        logger.warning("Could not clear the start-failure breaker", exc_info=True)


def _print_start_failure_breaker(paths) -> None:
    """Name the last start failure, and the give-up state, from the breaker.

    A boot refusal reaches only the tray log, which nobody reads. The breaker
    keeps the reason, so status can print it.

    The breaker also exits the supervised service cleanly once
    START_FAILURE_LIMIT starts fail inside START_FAILURE_WINDOW_SECONDS, so
    the supervisor stops relaunching and `jacked service status` would
    otherwise just say "stopped" with no reason.
    """
    import time as _t

    from jacked.service import START_FAILURE_FILENAME, START_FAILURE_LIMIT
    from jacked.service.start_failures import read_start_failures

    now = _t.time()
    # A missing or corrupt breaker file reads as no failures.
    recent = read_start_failures(paths.root / START_FAILURE_FILENAME, now)
    if not recent:
        return
    last = recent[-1]
    reason = last.reason or "did not become ready"
    when = _t.strftime("%H:%M:%S", _t.localtime(last.at))
    console.print(f"  [yellow]Last start failure: {reason} ({when})[/yellow]")
    if len(recent) < START_FAILURE_LIMIT:
        return
    console.print(
        f"  [yellow]Retries:   stopped after {len(recent)} failed starts; "
        "the service will not retry until you run `jacked service restart`[/yellow]"
    )


def _manifest_is_proven_stale(path: Path) -> bool:
    """True only when a valid manifest names a dead or identity-mismatched PID."""
    from jacked.service.instance import manifest_is_proven_stale

    return manifest_is_proven_stale(path)


def _spawn_service_detached(host: str | None, port: int):
    """Spawn `jacked service start` detached so it survives the caller exiting.

    Returns the log path the detached service writes to. The child runs the
    tray icon + uvicorn (ServiceRunner). Windows uses DETACHED_PROCESS for the
    windowless pythonw.exe path and CREATE_NO_WINDOW for the jacked.exe fallback
    (a console trampoline that would otherwise pop a window); POSIX uses
    start_new_session. Shared by `jacked start` and `jacked service restart`.

    ``host`` is passed through as ``--host`` ONLY when the caller supplied one
    (not None). Omitting it keeps argv honest so the detached service resolves
    its bind from the DB / loopback default, which is what makes upgrade and
    autostart restarts honor the GUI Remote access toggle instead of a baked host.
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

    svc_args = ["service", "start"]
    if host is not None:
        svc_args += ["--host", host]
    svc_args += ["--port", str(port)]
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
@click.option("--host", default=None, help="Host to bind for this launch (ignored once Remote access is configured in the dashboard, which is then authoritative; default 127.0.0.1). Pass 0.0.0.0 to expose on all interfaces.")
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
    from jacked.service import DEFAULT_PORT
    from jacked.service.ipc import ControlAction, send_native_control
    from jacked.service.lifecycle import (
        default_service_paths,
        handoff_owned_service,
        provision_service_contract,
    )
    from jacked.service.process import (
        is_port_available,
    )

    the_port = port or DEFAULT_PORT
    paths = default_service_paths()

    # A v2 manifest is the only authority for service control. Its native
    # control channel proves possession of the manifest nonce and the restart
    # handoff additionally waits for the owned lease to transfer.
    if paths.manifest.exists():
        if restart:
            try:
                stale = _manifest_is_proven_stale(paths.manifest)
            except (OSError, ValueError):
                console.print(
                    "[red]The service manifest is invalid.[/red] No process was "
                    "signalled. Run `jacked service recover`."
                )
                sys.exit(1)
            if not stale:
                spec, environment = provision_service_contract(paths=paths)
                handoff = handoff_owned_service(
                    spec, environment=environment, paths=paths
                )
                if handoff.ok:
                    console.print(
                        f"[green][OK][/green] Owned service handoff: {handoff.reason}"
                    )
                    return
                console.print(
                    "[red]Owned service restart did not complete.[/red] "
                    f"{handoff.reason}."
                )
                console.print(
                    "[dim]The shutdown request may already have been accepted. "
                    "Check `jacked service status` in a minute; if the service "
                    "is still down, run `jacked service restart`, then "
                    "`jacked service recover` to inspect ownership.[/dim]"
                )
                sys.exit(1)
            console.print(
                "[yellow]The prior owned service is stale; starting a new "
                "instance.[/yellow]"
            )
        try:
            response = send_native_control(paths.manifest, ControlAction.STATUS)
        except (OSError, ValueError) as exc:
            try:
                stale = _manifest_is_proven_stale(paths.manifest)
            except (OSError, ValueError):
                console.print(
                    "[red]The service manifest is invalid.[/red] No process was "
                    "signalled. Run `jacked service recover`."
                )
                sys.exit(1)
            if stale:
                console.print(
                    "[yellow]The prior owned service is stale; starting a new "
                    "instance.[/yellow]"
                )
            else:
                console.print(
                    "[red]Could not authenticate the existing service.[/red] "
                    f"{type(exc).__name__}. No process was signalled."
                )
                console.print(
                    "[dim]Run `jacked service recover` to inspect ownership.[/dim]"
                )
                sys.exit(1)
        else:
            status = response.get("result", {})
            if not response.get("ok") or status.get("state") != "running":
                console.print(
                    "[red]The owned service is present but degraded.[/red] "
                    "No process was signalled."
                )
                console.print(
                    "[dim]Run `jacked service recover` to inspect ownership.[/dim]"
                )
                sys.exit(1)
            running_port = status.get("port") or the_port
            console.print(
                "[green][OK][/green] jacked already running "
                f"(authenticated v2 service on :{running_port})"
            )
            console.print(f"[dim]http://127.0.0.1:{running_port}[/dim]")
            return

    # The old PID file is refusal-only evidence. PID reuse means it can never
    # authorize signalling or file removal, even when the recorded PID exists.
    from jacked.service.legacy import resolve_active_legacy_service

    legacy = resolve_active_legacy_service(paths.legacy_pid)
    if legacy is not None:
        if not restart:
            console.print(
                "[yellow]A legacy jacked service is already running "
                f"(PID {legacy.pid}, dashboard on :{legacy.port}).[/yellow]"
            )
            console.print(f"[dim]http://127.0.0.1:{legacy.port}[/dim]")
            return
        console.print(
            "[red]Refusing to control a legacy PID-only service.[/red] "
            "No process was signalled."
        )
        console.print(
            "[dim]Quit the old tray, then retry `jacked start"
            + (" --restart" if restart else "")
            + "`.[/dim]"
        )
        sys.exit(1)

    # Port held by something that isn't us?
    if not is_port_available("127.0.0.1", the_port):
        console.print(
            f"[red]Port {the_port} is in use by another process.[/red] "
            "Free it or pass --port."
        )
        sys.exit(1)

    console.print(f"[dim]Starting jacked service (detached) on :{the_port}...[/dim]")
    _clear_start_failure_breaker(paths)
    # Pass the raw host (possibly None): None means "no --host in argv", so the
    # detached service resolves its bind from the DB / loopback default.
    log_path = _spawn_service_detached(host, the_port)

    from jacked import __version__ as current_build

    status = _wait_owned_service_ready(paths, expected_build=current_build)
    if status is not None:
        running_port = status.get("port") or the_port
        console.print(
            f"[green][OK][/green] jacked running - tray icon up, "
            f"dashboard at http://127.0.0.1:{running_port}"
        )
    else:
        console.print(
            f"[yellow]Started, but :{the_port} didn't answer within "
            f"{int(REPLACEMENT_READY_TIMEOUT)}s.[/yellow]"
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
    help="Optional extras to install. All former extras are retired/empty aliases; default: tray (empty alias).",
)
@click.option(
    "--skip-service",
    is_flag=True,
    help="Don't touch the running service — just upgrade the package + migrate settings.",
)
@click.option(
    "--no-rollback",
    is_flag=True,
    help="Keep the new version installed even when its service preflight or restart fails.",
)
def upgrade(extras: str, skip_service: bool, no_rollback: bool):
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
    # The extras name reaches a shell (uv) and, on Windows, a generated batch
    # file. Refuse anything but a plain extras identifier before building either.
    if not _EXTRAS_NAME_RE.fullmatch(extras or ""):
        console.print(
            "[red]Invalid --extras value.[/red] Use letters, digits, '_', '-', ',' or '.'"
        )
        sys.exit(2)

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
        _spawn_windows_upgrade_helper(
            cmd, label, extras, skip_service,
            previous_version=__version__, no_rollback=no_rollback,
        )
        return

    # POSIX path: run inline. `__version__` is read BEFORE anything is
    # installed, so it names the build a rollback must restore.
    _run_upgrade_inline(
        cmd, label, extras, skip_service, PID_FILE, DEFAULT_HOST, DEFAULT_PORT,
        previous_version=__version__, no_rollback=no_rollback,
    )


def _installed_package_version() -> str:
    """Return the claude-jacked version that is on disk right now.

    `jacked.__version__` is the version this interpreter IMPORTED, so after an
    in-place install it still names the old build. The distribution metadata is
    read from disk on each call, so it names the build that was just written.
    Returns an empty string when the metadata cannot be read.
    """
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("claude-jacked")
    except Exception:
        return ""


def _version_label(version: str) -> str:
    """Return 'v1.2.3', or 'the new build' when the version is unknown."""
    return f"v{version}" if version else "the new build"


def _write_upgrade_recovery(reason: str, commands: list[str]) -> None:
    """Record why an upgrade failed, and the exact commands that repair it.

    Writes the same file the tray updater uses, so one place answers "what
    broke my install" no matter which flow ran.
    """
    from jacked.service.updater import RECOVERY_FILE

    body = f"Jacked upgrade failed: {reason}\n\nRecovery:\n"
    body += "".join(f"  {line}\n" for line in commands)
    try:
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(body, encoding="utf-8")
    except OSError as exc:
        console.print(f"[dim]Could not write {RECOVERY_FILE}: {exc}[/dim]")


@dataclass(frozen=True)
class _RollbackPlan:
    """Everything the rollback of one failed upgrade needs to know."""

    jacked: str
    previous_version: str
    extras: str
    skip_service: bool
    reason: str


# Rollback step names. A partial restoration must say WHICH step failed, so
# the names double as recovery-file text and as the return value of
# _run_rollback_steps.
_ROLLBACK_STEP_PACKAGE = "package rollback"
_ROLLBACK_STEP_INSTALL = "settings migration"
_ROLLBACK_STEP_RESTART = "service restart"


def _rollback_step_command(step: str, rollback_label: str) -> str:
    """Return the exact command that repairs a failed rollback step."""
    if step == _ROLLBACK_STEP_PACKAGE:
        return rollback_label
    if step == _ROLLBACK_STEP_INSTALL:
        return "jacked install --force"
    return "jacked service restart"


def _run_rollback_steps(
    plan: "_RollbackPlan", rollback_cmd: list[str], rollback_label: str
) -> "str | None":
    """Reinstall the previous build, migrate its settings, restart its service.

    Returns the name of the FIRST step that failed, or None when every step
    succeeded. Every step is checked: a rollback that reinstalled the package
    but never migrated settings or restarted the service has not restored the
    machine, and must never be reported as if it had.
    """
    from jacked.findbin import find_bin

    cmd = list(rollback_cmd)
    if cmd and cmd[0] == "uv":
        resolved_uv = find_bin("uv")
        if resolved_uv:
            cmd[0] = resolved_uv

    console.print(f"\n[yellow]Rolling back to v{plan.previous_version}[/yellow]")
    console.print(f"[dim]$ {rollback_label}[/dim]")
    if _rollback_step_failed(cmd, "Rollback command"):
        return _ROLLBACK_STEP_PACKAGE

    restored = find_bin("jacked") or plan.jacked
    console.print(f"[dim]$ {restored} install --force[/dim]")
    if _rollback_step_failed(
        [restored, "install", "--force"], "`jacked install --force`"
    ):
        return _ROLLBACK_STEP_INSTALL

    if plan.skip_service:
        return None

    console.print(f"[dim]$ {restored} service restart[/dim]")
    if _rollback_step_failed(
        [restored, "service", "restart"], "`jacked service restart`"
    ):
        return _ROLLBACK_STEP_RESTART
    return None


def _rollback_step_failed(argv: list[str], what: str) -> bool:
    """Run one rollback step. True when it failed to run or exited non-zero.

    A step whose binary is missing or not executable raises OSError. Letting
    that escape abandoned the rollback before the failed step was recorded, so
    the user got no recovery file naming what to repair.
    """
    import subprocess

    try:
        code = subprocess.run(argv).returncode
    except OSError as exc:
        console.print(f"[red]{what} could not run:[/red] {exc}")
        return True
    if code != 0:
        console.print(f"[red]{what} failed (exit {code}).[/red]")
        return True
    return False


def _upgrade_rollback_and_exit(
    *,
    plan: "_RollbackPlan",
    new_version: str,
    detail: str,
    no_rollback: bool,
) -> None:
    """Undo a failed upgrade: reinstall the previous version, then exit 1.

    An upgrade is a transaction. When the new build cannot provision its
    service contract, or its service never restarts, the machine must go back
    to the build that was running. "Rolled back" is claimed only when every
    restoration step succeeded.
    """
    from jacked.install_method import rollback_command, rollback_command_label

    new_label = _version_label(new_version)
    console.print(f"\n[red]Upgrade to {new_label} failed:[/red] {plan.reason}")
    if detail:
        console.print(f"[red]{detail}[/red]")

    if no_rollback:
        console.print(
            f"[yellow]Keeping {new_label} installed (--no-rollback). "
            "The service may be down.[/yellow]"
        )
        _finish_failed_upgrade(
            error=f"{new_label} {plan.reason}; rollback was disabled with --no-rollback",
            commands=[
                f"jacked service preflight    # {detail or 'shows the refusal'}",
                "jacked service status",
            ],
        )

    try:
        rb_cmd = rollback_command(plan.extras, plan.previous_version)
        rb_label = rollback_command_label(plan.extras, plan.previous_version)
    except ValueError as exc:
        console.print(f"[red]Cannot roll back automatically:[/red] {exc}")
        _finish_failed_upgrade(
            error=(
                f"{new_label} {plan.reason}; automatic rollback is not "
                f"available ({exc})"
            ),
            commands=["jacked service status"],
        )

    failed_step = _run_rollback_steps(plan, rb_cmd, rb_label)
    _report_rollback_outcome(plan, new_label, failed_step, rb_label)


def _report_rollback_outcome(
    plan: "_RollbackPlan", new_label: str, failed_step: "str | None", rb_label: str
) -> None:
    """Report the rollback result, write the recovery file, and exit 1.

    A partial restoration is never reported as a restoration: the recovery
    file names the step that failed and the exact command that repairs it.
    """
    from jacked.service.updater import RECOVERY_FILE

    if failed_step is None:
        commands = [rb_label, "jacked install --force"]
        if not plan.skip_service:
            commands.append("jacked service restart")
        error = (
            f"{new_label} {plan.reason}; rolled back to v{plan.previous_version}"
        )
        _write_upgrade_recovery(error, commands)
        _mark_upgrade_status_failed(
            error=error,
            recovery=f"v{plan.previous_version} is restored. Run: jacked service status",
        )
        console.print(
            f"[yellow]Restored v{plan.previous_version}. "
            f"Details in {RECOVERY_FILE}[/yellow]"
        )
        sys.exit(1)

    repair = _rollback_step_command(failed_step, rb_label)
    console.print(
        f"[red]Rollback incomplete:[/red] the {failed_step} step failed, so "
        f"v{plan.previous_version} is NOT fully restored."
    )
    _finish_failed_upgrade(
        error=(
            f"{new_label} {plan.reason}; the rollback to "
            f"v{plan.previous_version} stopped at the {failed_step} step"
        ),
        commands=[
            f"{repair}    # this is the {failed_step} step that failed",
            "jacked install --force",
            "jacked service restart",
            "jacked service status",
        ],
    )


def _mark_upgrade_status_failed(*, error: str, recovery: str) -> None:
    """Record the failure in the shared update-status file. Never raises."""
    try:
        from jacked.service import update_status as _us

        _us.mark_failed(_us.UPDATE_STATUS_FILE, error=error, recovery=recovery)
    except Exception:
        logger.debug("Could not mark the update status failed", exc_info=True)


def _finish_failed_upgrade(*, error: str, commands: list[str]) -> None:
    """Write the recovery file, mark the status failed, and exit 1."""
    from jacked.service.updater import RECOVERY_FILE

    _write_upgrade_recovery(error, commands)
    _mark_upgrade_status_failed(error=error, recovery="; ".join(commands))
    console.print(f"[dim]Wrote {RECOVERY_FILE}[/dim]")
    sys.exit(1)


def _acquire_upgrade_lock(previous_version: str, extras: str):
    """Claim the shared update lock, or refuse when one is in flight.

    The tray updater and this command drive the same machine. Two of them at
    once would race a package install against a service restart. The exclusive
    OS lock is taken FIRST, because `init_or_adopt_status` is a read-check-
    write on mtime and two upgrades starting inside the same second both read
    "no updater in flight". The status file then records the phases.

    Returns the lock handle. The caller must close it when the upgrade ends.
    """
    from jacked.install_method import detect_install_method
    from jacked.service import update_status as _us

    try:
        handle = _us.acquire_update_lock(_us.UPDATE_STATUS_FILE)
    except Exception as exc:
        # A machine that cannot take the lock must not start a package
        # install: nothing would stop a second upgrade from racing it.
        console.print(
            "[red]Cannot take the update lock:[/red] "
            f"{exc}\nFix the permissions on ~/.claude, then run "
            "`jacked upgrade` again."
        )
        sys.exit(1)
    if handle is None:
        console.print(
            "[red]Another jacked update is already running.[/red] "
            "Wait for it to finish, then run `jacked upgrade` again."
        )
        sys.exit(1)

    try:
        method = detect_install_method()
    except Exception:
        method = "unknown"
    try:
        _us.init_or_adopt_status(
            _us.UPDATE_STATUS_FILE,
            from_version=previous_version,
            to_version="next",
            method=method,
        )
    except _us.LockBusy as exc:
        _release_upgrade_lock(handle)
        console.print(
            "[red]Another jacked update is already running.[/red] "
            f"{exc}\nWait for it to finish, then run `jacked upgrade` again."
        )
        sys.exit(1)
    except Exception as exc:
        # The status file is where every later phase, the failure reason and
        # the recovery text are recorded. A machine that cannot write it (bad
        # permissions, a full disk) must not begin a package install: the
        # upgrade would proceed with no way to report what it broke.
        _release_upgrade_lock(handle)
        console.print(
            "[red]Cannot record the update status:[/red] "
            f"{exc}\nFix the permissions on ~/.claude, then run "
            "`jacked upgrade` again."
        )
        sys.exit(1)
    del extras  # kept for signature symmetry with the tray updater
    return handle


def _release_upgrade_lock(handle) -> None:
    """Release the exclusive update lock. Never raises."""
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        logger.debug("Could not release the update lock", exc_info=True)


def _upgrade_phase(phase: str, status: str, **kwargs) -> None:
    """Record one upgrade phase. Never raises: observability is not the work."""
    from jacked.service import update_status as _us

    try:
        if status == "begin":
            _us.begin_phase(_us.UPDATE_STATUS_FILE, phase)
        else:
            _us.end_phase(_us.UPDATE_STATUS_FILE, phase, status=status, **kwargs)
    except Exception:
        logger.debug("Update phase %s (%s) not recorded", phase, status, exc_info=True)


def _upgrade_install_package(cmd: list[str], label: str) -> str:
    """Run the package upgrade, then re-resolve the `jacked` binary."""
    import subprocess
    from jacked.findbin import find_bin

    _upgrade_phase("installing_package", "begin")
    console.print(f"[dim]$ {label}[/dim]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _upgrade_phase(
            "installing_package", "failed",
            error=f"upgrade command exit {result.returncode}", recovery=label,
        )
        console.print(
            f"[red]Package upgrade failed (exit {result.returncode}). Aborting.[/red]"
        )
        sys.exit(result.returncode)

    # Re-resolve jacked - the path may have changed after --force.
    jacked = find_bin("jacked")
    if not jacked:
        _upgrade_phase(
            "installing_package", "failed",
            error="jacked binary missing after install",
            recovery="jacked install --force",
        )
        console.print(
            "[red]`jacked` not found after install.[/red] "
            "Check your PATH includes `~/.local/bin`."
        )
        sys.exit(1)
    _upgrade_phase("installing_package", "ok")
    return jacked


def _upgrade_run_preflight(jacked: str) -> "tuple[int, str]":
    """Run `jacked service preflight`. Returns (returncode, output).

    A preflight that hangs or cannot be launched is a REFUSAL, not a crash.
    Without the bound the command would wait forever with the transaction
    half-applied; without the OSError guard a missing or non-executable
    `jacked` would raise straight past the rollback path.
    """
    import subprocess

    from jacked.service.updater import PREFLIGHT_TIMEOUT_SECONDS

    _upgrade_phase("preflight", "begin")
    console.print(f"\n[dim]$ {jacked} service preflight[/dim]")
    try:
        preflight = subprocess.run(
            [jacked, "service", "preflight"],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
        code = preflight.returncode
        output = ((preflight.stdout or "") + (preflight.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        code = -1
        output = (
            "preflight did not answer within "
            f"{PREFLIGHT_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        code = -1
        output = f"preflight could not run: {exc}"
    if output:
        console.print(f"[dim]{output}[/dim]")
    return code, output


def _upgrade_restart_service(jacked: str, pid_file) -> int:
    """Restart the service for the new build. Returns its exit code.

    Returns 0 when no restart was owed (a legacy tray that cannot be
    authenticated safely is left alone, exactly as before).
    """
    import subprocess
    from jacked.service.legacy import resolve_active_legacy_service
    from jacked.service.lifecycle import default_service_paths

    paths = default_service_paths()
    legacy = resolve_active_legacy_service(pid_file)
    if not paths.manifest.exists() and legacy is not None:
        console.print(
            "\n[yellow]Package upgraded, but the running legacy tray cannot "
            "be authenticated safely.[/yellow] No process was signalled."
        )
        console.print(
            f"[dim]Quit the old tray, then run `{jacked} service start`.[/dim]"
        )
        return 0

    console.print(f"\n[dim]$ {jacked} service restart[/dim]")
    return subprocess.run([jacked, "service", "restart"]).returncode


def _clear_upgrade_recovery_file() -> None:
    """Remove a recovery file left by an earlier failed upgrade."""
    from jacked.service.updater import RECOVERY_FILE

    try:
        RECOVERY_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not remove %s", RECOVERY_FILE, exc_info=True)


def _upgrade_migrate_settings(
    plan: "_RollbackPlan", new_version: str, no_rollback: bool
) -> None:
    """Migrate settings.json for the new build, or roll the upgrade back.

    A non-zero `jacked install --force` used to print a warning and let the
    upgrade finish. That shipped a build whose settings never migrated and
    still said "Upgrade complete", so it is a transaction failure now.
    """
    import subprocess

    _upgrade_phase("migrating_settings", "begin")
    console.print(f"\n[dim]$ {plan.jacked} install --force[/dim]")
    code = subprocess.run([plan.jacked, "install", "--force"]).returncode
    if code == 0:
        _upgrade_phase("migrating_settings", "ok")
        return
    _upgrade_phase(
        "migrating_settings", "failed",
        error=f"jacked install exit {code}",
        recovery="jacked install --force",
    )
    _upgrade_rollback_and_exit(
        plan=replace(plan, reason=f"the settings migration failed (exit {code})"),
        new_version=new_version,
        detail=(
            "settings.json may be in a partial state. Backups are at "
            "~/.claude/settings.json.bak-*"
        ),
        no_rollback=no_rollback,
    )


def _upgrade_restart_or_rollback(
    plan: "_RollbackPlan", new_version: str, no_rollback: bool, pid_file
) -> None:
    """Restart the new service, or roll the upgrade back when it refuses."""
    _upgrade_phase("restarting_service", "begin")
    code = _upgrade_restart_service(plan.jacked, pid_file)
    if code == 0:
        _upgrade_phase("restarting_service", "ok")
        return
    _upgrade_phase(
        "restarting_service", "failed",
        error=f"service restart exit {code}",
        recovery="jacked service restart",
    )
    _upgrade_rollback_and_exit(
        plan=replace(plan, reason=f"the new service did not restart (exit {code})"),
        new_version=new_version,
        detail=f"Run manually: {plan.jacked} service restart",
        no_rollback=no_rollback,
    )


def _run_upgrade_inline(
    cmd: list[str], label: str, extras: str, skip_service: bool,
    pid_file, host: str, port: int,
    previous_version: str = "", no_rollback: bool = False,
):
    """Inline upgrade for POSIX. Running binary gets replaced safely via inode.

    The upgrade is a transaction. Between the package install and the service
    restart, `jacked service preflight` proves the NEW build can provision its
    service contract. Any failure after that gate rolls the machine back to
    `previous_version`.
    """
    request = _UpgradeRequest(
        cmd=list(cmd),
        label=label,
        extras=extras,
        skip_service=skip_service,
        pid_file=pid_file,
        previous_version=previous_version,
        no_rollback=no_rollback,
    )
    lock = _acquire_upgrade_lock(previous_version, extras)
    try:
        _run_upgrade_inline_locked(request)
    finally:
        # Held for the whole transaction, released on success, on a rollback
        # (which exits through SystemExit) and on any unexpected raise.
        _release_upgrade_lock(lock)


@dataclass(frozen=True)
class _UpgradeRequest:
    """Everything the inline upgrade transaction needs, decided up front."""

    cmd: list[str]
    label: str
    extras: str
    skip_service: bool
    pid_file: object
    previous_version: str = ""
    no_rollback: bool = False


def _run_upgrade_inline_locked(request: "_UpgradeRequest") -> None:
    """The upgrade transaction itself. Runs while the update lock is held."""
    from jacked.service import update_status as _us

    # Step 1: package upgrade (uv / pipx, auto-detected).
    jacked = _upgrade_install_package(request.cmd, request.label)
    new_version = _installed_package_version()
    plan = _RollbackPlan(
        jacked=jacked,
        previous_version=request.previous_version,
        extras=request.extras,
        skip_service=request.skip_service,
        reason="",
    )
    # Step 2: the transaction gate. Runs even with --skip-service.
    _upgrade_preflight_or_rollback(plan, new_version, request.no_rollback)
    # Step 3: settings migration; a failure rolls back to the running build.
    _upgrade_migrate_settings(plan, new_version, request.no_rollback)
    # Step 4: restart through the newly installed CLI (authenticated handoff).
    if request.skip_service:
        console.print("\n[dim]Skipping service restart (--skip-service)[/dim]")
    else:
        _upgrade_restart_or_rollback(
            plan, new_version, request.no_rollback, request.pid_file
        )
    # A clean upgrade retires the recovery file an earlier failure left behind.
    _clear_upgrade_recovery_file()
    try:
        _us.mark_succeeded(_us.UPDATE_STATUS_FILE)
    except Exception:
        logger.debug("Could not mark the update status succeeded", exc_info=True)
    console.print("\n[green][OK][/green] Upgrade complete.")


def _upgrade_preflight_or_rollback(plan, new_version: str, no_rollback: bool) -> None:
    """Prove the NEW build can provision its service contract, or roll back.

    This runs before the old service is replaced, and with --skip-service too:
    it is cheap, and it is the whole point of the transaction.
    """
    preflight_code, preflight_output = _upgrade_run_preflight(plan.jacked)
    if preflight_code == 0:
        _upgrade_phase("preflight", "ok")
        return
    reason = "the new build cannot provision its service contract"
    _upgrade_phase(
        "preflight", "failed", error=reason, recovery="jacked service preflight"
    )
    _upgrade_rollback_and_exit(
        plan=replace(plan, reason=reason),
        new_version=new_version,
        detail=preflight_output,
        no_rollback=no_rollback,
    )


# The Windows helpers write their failure breadcrumb here. It is a DIFFERENT
# path from the update log, which cmd.exe already holds open for writing.
_WIN_FAILED_FILE = '"%USERPROFILE%\\.claude\\jacked-update-failed.txt"'


def _rollback_failed_label(
    label: str, step: str, repair: str, previous: str
) -> str:
    """Return the cmd.exe label that reports ONE failed rollback step.

    The batch reaches a label like this only when a rollback step returned a
    non-zero errorlevel. It names the step, gives the exact command that
    repairs it, and exits 1 - it never writes "rolled back".
    """
    return (
        ':' + label + '\r\n'
        'echo [%date% %time%] ERROR: the ' + step + ' step of the rollback failed\r\n'
        'echo Jacked upgrade failed, and the rollback to v' + previous
        + ' stopped at the ' + step + ' step. See %LOGFILE%. > '
        + _WIN_FAILED_FILE + '\r\n'
        'echo Run this first: ' + repair + ' >> ' + _WIN_FAILED_FILE + '\r\n'
        'echo Then: jacked install --force ^&^& jacked service restart >> '
        + _WIN_FAILED_FILE + '\r\n'
        'exit /b 1\r\n'
    )


def _spawn_windows_upgrade_helper(
    cmd: list[str], label: str, extras: str, skip_service: bool,
    previous_version: str = "", no_rollback: bool = False,
):
    """Windows: spawn a detached cmd.exe helper and exit this process.

    Running jacked.exe can't be overwritten while we're holding it open.
    The helper is cmd.exe (a system binary we don't own), which stays
    valid no matter what the upgrade command does to the jacked venv
    or user site-packages.

    Helper steps:
      1. Wait for our PID to exit (avoids racing against the .exe lock).
      2. Run the detected upgrade command (uv / pipx / pip).
      3. `jacked service preflight` (prove the new build can provision its
         service contract). A refusal rolls back to `previous_version`.
      4. `jacked install --force` (migrate settings.json).
      5. `jacked service restart` (unless --skip-service).
      6. Append progress to ~/.claude/jacked-update.log.
    """
    import os
    import subprocess
    import tempfile
    from jacked.findbin import find_bin
    from jacked.install_method import (
        rollback_command,
        rollback_command_label,
        safe_version_label,
    )
    from jacked.service import CLAUDE_DIR
    from jacked.service.updater import UPDATE_LOG, wait_for_parent_block

    my_pid = os.getpid()
    # Validate the version string ONCE, here, and use the result in every batch
    # line. A version that fails the check must never reach a shell line, not
    # even inside an echo.
    safe_previous = safe_version_label(previous_version)
    from jacked.install_method import detect_install_method

    method = detect_install_method()
    log_path = CLAUDE_DIR / "jacked-update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Re-quote the upgrade command for cmd.exe. Paths may contain spaces
    # (uv from AppData, user site-packages python.exe, etc.); every argv
    # element must be individually quoted to survive cmd's tokenization.
    upgrade_line = " ".join(f'"{arg}"' for arg in cmd)

    # Rollback command for the version that is running right now. Built here,
    # in Python, so the batch never has to compose a requirement specifier.
    rollback_line = ""
    rollback_label = ""
    if previous_version and not no_rollback:
        try:
            rb_cmd = rollback_command(extras, previous_version)
            rollback_label = rollback_command_label(extras, previous_version)
        except ValueError:
            rb_cmd = []
            rollback_label = ""
        if rb_cmd:
            if rb_cmd[0] == "uv":
                _resolved_uv = find_bin("uv")
                if _resolved_uv:
                    rb_cmd[0] = _resolved_uv
            rollback_line = " ".join(f'"{arg}"' for arg in rb_cmd)

    # Everything below writes to plain stdout, which the Popen at the bottom
    # binds to the update log. Do NOT reintroduce `>> "%LOGFILE%"` on any step:
    # cmd.exe cannot open a redirection target that another handle already holds
    # for writing, and it SKIPS the command while leaving ERRORLEVEL at 0 — so
    # the step silently no-ops and every `if errorlevel 1` guard sails past it.
    # That exact mistake shipped in the tray updater and cost a user their whole
    # install (no upgrade, no settings migration, no service, no tray icon).
    # %LOGFILE% stays defined only for the recovery-file message text, and the
    # recovery file is a different path nothing else holds open.
    # Flat labels, not a parenthesised block: a failed restart has to `goto`
    # the rollback, and `goto` out of a paren block is exactly the cmd.exe
    # parsing trap this batch avoids everywhere else.
    restart_line = (
        ''
        if skip_service
        else (
            'echo [%date% %time%] service restart\r\n'
            'jacked service restart 2>&1\r\n'
            'if errorlevel 1 goto restart_failed\r\n'
        )
    )
    # Rollback path. Reached only by `goto`, so the success path never touches
    # it. Flat lines and one single-level `if` block: nested parentheses inside
    # a parenthesised block is the classic cmd.exe parsing trap.
    _failed_file = _WIN_FAILED_FILE
    # Every rollback step is checked. A rollback that reinstalled the package
    # but never migrated settings or restarted the service has NOT restored the
    # machine, so it must never write "rolled back". Each failure jumps to a
    # flat label that names its own step: `goto` out of a parenthesised block is
    # exactly the cmd.exe parsing trap this batch avoids everywhere else.
    # Three entry labels, one shared rollback body. Every failure after the
    # package install has to undo the upgrade, and each one names its own step
    # in the failed file rather than borrowing the preflight's wording.
    preflight_failed_block = (
        ':preflight_failed\r\n'
        'echo [%date% %time%] ERROR: jacked service preflight failed for the new build\r\n'
        'set FAILREASON=its service preflight failed\r\n'
        'goto do_rollback\r\n'
    )
    if not skip_service:
        preflight_failed_block += (
            ':restart_failed\r\n'
            'echo [%date% %time%] ERROR: the new service did not restart\r\n'
            'set FAILREASON=the new service did not restart\r\n'
            'goto do_rollback\r\n'
        )
    preflight_failed_block += (
        ':migrate_failed\r\n'
        'echo [%date% %time%] ERROR: the settings migration failed for the new build\r\n'
        'set FAILREASON=the settings migration failed\r\n'
        ':do_rollback\r\n'
    )
    if rollback_line:
        _rb_restart = (
            'goto rollback_done\r\n'
            if skip_service
            else (
                'echo [%date% %time%] service restart\r\n'
                'jacked service restart 2>&1\r\n'
                'if errorlevel 1 goto rollback_restart_failed\r\n'
            )
        )
        preflight_failed_block += (
            'echo [%date% %time%] rolling back to v' + safe_previous + '\r\n'
            + rollback_line + ' 2>&1\r\n'
            'if errorlevel 1 goto rollback_package_failed\r\n'
            'echo [%date% %time%] running jacked install --force\r\n'
            'jacked install --force 2>&1\r\n'
            'if errorlevel 1 goto rollback_install_failed\r\n'
            + _rb_restart +
            ':rollback_done\r\n'
            'echo Jacked upgrade failed (%FAILREASON%); rolled back to v'
            + safe_previous + '. See %LOGFILE%. > ' + _failed_file + '\r\n'
            'echo Recovery: ' + rollback_label
            + ' ^&^& jacked install --force ^&^& jacked service restart >> '
            + _failed_file + '\r\n'
            'exit /b 1\r\n'
            + _rollback_failed_label(
                'rollback_package_failed', 'package rollback', rollback_label,
                safe_previous,
            )
            + _rollback_failed_label(
                'rollback_install_failed', 'settings migration',
                'jacked install --force', safe_previous,
            )
            + _rollback_failed_label(
                'rollback_restart_failed', 'service restart',
                'jacked service restart', safe_previous,
            )
        )
    else:
        preflight_failed_block += (
            'echo [%date% %time%] rollback disabled; keeping the new version\r\n'
            'echo Jacked upgrade failed (%FAILREASON%) and was NOT rolled back. '
            'See %LOGFILE%. > ' + _failed_file + '\r\n'
            'echo Recovery: jacked service preflight ^&^& jacked service status >> '
            + _failed_file + '\r\n'
            'exit /b 1\r\n'
        )

    batch_body = (
        '@echo off\r\n'
        'set LOGFILE=' + str(log_path) + '\r\n'
        'set SKIP_SERVICE=' + ("1" if skip_service else "") + '\r\n'
        # Default reason for the breadcrumb; every rollback entry label
        # overwrites it with the step that actually failed.
        'set FAILREASON=the new build could not be brought up\r\n'
        'echo [%date% %time%] jacked upgrade helper starting (parent PID ' + str(my_pid) + ')\r\n'
        'echo [%date% %time%] upgrade command: ' + label + '\r\n'
        # The restart step is decided in Python when the batch is written, so
        # SKIP_SERVICE is a record of what this batch was generated for. Echo
        # it: a support reader needs to know why no restart line ran.
        'echo [%date% %time%] skip service: "%SKIP_SERVICE%"\r\n'
        # In-flight guard: the same status-record claim the tray batch makes.
        # A batch cannot hold an OS lock across steps, so this is the Windows
        # equivalent of the CLI's exclusive update lock.
        'jacked _update_status_init "' + safe_previous + '" "next" ' + method
        + ' --log-path "' + str(log_path) + '"\r\n'
        'if errorlevel 2 (\r\n'
        '    echo [%date% %time%] REFUSED: another jacked updater is in progress\r\n'
        '    echo Another jacked updater is already in progress. Aborting. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 2\r\n'
        ')\r\n'
        + wait_for_parent_block(my_pid) +
        'echo [%date% %time%] parent exited, running upgrade command\r\n'
        + upgrade_line + ' 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    echo [%date% %time%] ERROR: upgrade command failed\r\n'
        '    echo Jacked upgrade failed. See %LOGFILE% for details. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    echo Recovery: ' + label + ' ^&^& jacked install --force >> "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        # Transaction gate: the new build must prove it can provision its
        # service contract before the old service is replaced.
        'echo [%date% %time%] running jacked service preflight\r\n'
        'jacked service preflight --timeout 120 2>&1\r\n'
        'if errorlevel 1 goto preflight_failed\r\n'
        'echo [%date% %time%] running jacked install --force\r\n'
        'jacked install --force 2>&1\r\n'
        'if errorlevel 1 goto migrate_failed\r\n'
        + restart_line +
        # A clean upgrade retires the breadcrumb an earlier failure left
        # behind, so nothing warns about a failure already repaired.
        'if exist ' + _WIN_FAILED_FILE + ' del ' + _WIN_FAILED_FILE + '\r\n'
        'echo [%date% %time%] upgrade complete\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
        + preflight_failed_block
    )

    # Write the batch file to %TEMP% — it deletes itself at the end.
    fd, batch_path = tempfile.mkstemp(suffix=".bat", prefix="jacked-upgrade-")
    try:
        # newline="" — batch_body already carries explicit \r\n. Translating on
        # top of that wrote \r\r\n; cmd.exe tolerates the stray CR but nothing
        # here should depend on that.
        with os.fdopen(fd, "w", newline="") as f:
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
    # Capture the batch's stdout/stderr into the update log by handing cmd.exe
    # an inherited handle on it — the same pattern the tray updater uses. This
    # is what makes the batch's bare `echo`/command output land in the log, and
    # it is precisely why no step inside the batch may redirect to %LOGFILE%.
    try:
        _lf = open(UPDATE_LOG, "a", encoding="utf-8", errors="replace")
    except OSError:
        _lf = subprocess.DEVNULL
    _helper_kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=_lf,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    try:
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
    finally:
        # cmd.exe holds its own inherited copy of the handle; close ours.
        if _lf is not subprocess.DEVNULL:
            try:
                _lf.close()
            except OSError:
                pass

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

    Exit 0 on success OR when adopting the tray's pre-init file (metadata
    written, no phases opened yet — the tray creates it moments before
    spawning us). Exit 2 only when a REAL updater is in flight (has a phase
    open). The Windows batch checks errorlevel and aborts on 2.
    """
    from jacked.service import update_status as us_mod
    try:
        # This shim exits immediately while the batch that called it keeps
        # running, so its own pid would read as "updater dead" within seconds.
        # Record no pid: the batch's liveness then follows the mtime rule.
        outcome = us_mod.init_or_adopt_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version=from_version,
            to_version=to_version,
            method=method,
            log_path=log_path,
            updater_pid=us_mod.NO_UPDATER_PID,
        )
    except us_mod.LockBusy as exc:
        click.echo(f"[update-status] lock busy: {exc}", err=True)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 - the batch only aborts on exit 2
        click.echo(
            f"[update-status] cannot claim the status record: {type(exc).__name__}",
            err=True,
        )
        sys.exit(2)
    if outcome == "adopted":
        click.echo("[update-status] adopted tray pre-init")


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


@main.command(
    name="_hook",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("name")
@click.argument("hook_args", nargs=-1, type=click.UNPROCESSED)
def _hook_shim(name: str, hook_args: tuple):
    """Internal: dispatch to a hook handler by name.

    Called by Claude Code / Codex hooks via `jacked _hook <name> [args...]`.
    The handler's main() reads hook input from stdin as usual; any extra argv
    after the name (e.g. `--runtime codex`) is forwarded to main() so
    runtime-portable hooks (qa_suggest) can adapt their output.

    Indirection keeps settings.json paths stable across `uv tool upgrade`.
    """
    if name not in _valid_hook_names():
        # Retired/unknown hooks FAIL OPEN (exit 0): a stale settings.json entry
        # (e.g. the removed security_gatekeeper wired as PreToolUse) must never
        # block the user's tool calls between a package upgrade and the
        # `jacked install` that prunes the entry. Exit 2 would mean "deny".
        click.echo(f"Unknown hook: {name} (retired? run `jacked install` to prune)", err=True)
        sys.exit(0)

    import importlib
    try:
        module = importlib.import_module(f"jacked.data.hooks.{name}")
    except ImportError as e:
        click.echo(f"Hook import failed: {name} ({e})", err=True)
        sys.exit(2)

    if not hasattr(module, "main"):
        click.echo(f"Hook has no main(): {name}", err=True)
        sys.exit(2)

    # Forward extra argv only when present, so no-parameter hook mains keep
    # working (they're always invoked without extra args).
    if hook_args:
        module.main(list(hook_args))
    else:
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
    # Package-relative hooks dir — location-INDEPENDENT so it matches the
    # bare-path command form ({python} <...>/jacked/data/hooks/<hook>.py) the
    # web API writes, wherever the package lives: site-packages, an editable
    # clone, a git worktree, or a renamed checkout. (Subsumes the older
    # /site-packages/... and /claude-jacked/... anchors, which missed any
    # checkout dir not named `claude-jacked` — e.g. a worktree — and let the
    # API-then-CLI install path append a DUPLICATE gatekeeper hook.) Still
    # anchored to our package layout, so it won't match a user's own
    # security_gatekeeper.py living outside a jacked/data/hooks/ directory.
    "/jacked/data/hooks/",
    "jacked\" _hook ",                      # shim form we write: "<path>/jacked" _hook <name>
    "jacked.exe\" _hook ",                  # same shim on Windows, where find_bin returns jacked.EXE
    "-m jacked _hook ",                     # fallback form (dev without PATH shim)
)


def _is_jacked_managed_hook_path(command: str) -> bool:
    """True if this settings.json command value was installed by jacked.

    Anchored to path substrings we write — won't falsely match a user's
    own script named security_gatekeeper.py in an unrelated directory.
    Case-insensitive: the Windows shim is written as ``jacked.EXE``, and a
    case-sensitive match let every ``jacked install`` append one more
    SessionStart entry instead of collapsing the ones it already owned.

    >>> _is_jacked_managed_hook_path('"/home/u/.local/bin/jacked" _hook session_start')
    True
    >>> _is_jacked_managed_hook_path('"C:\\\\Users\\\\u\\\\.local\\\\bin\\\\jacked.EXE" _hook session_start')
    True
    >>> _is_jacked_managed_hook_path("/usr/local/bin/my-own-startup.sh")
    False
    """
    if not command:
        return False
    lowered = command.lower()
    return any(marker in lowered for marker in _JACKED_HOOK_PATH_MARKERS)


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


def _copy_skill_tree(src_skill_root: Path, skill_dir: Path) -> int:
    """Install one skill: SKILL.md AND every sidecar file (scripts, references/,
    assets) — a skill that ships a measure.js etc. is broken without them.

    `_link_or_copy` symlinks in editable mode and copies otherwise (plain
    shutil.copy raises SameFileError if dst is already a symlink to src, which
    broke the tray-triggered upgrade when a dev symlink was present). Returns the
    number of files written; raises OSError, which the caller reports per skill.

    >>> callable(_copy_skill_tree)
    True
    """
    skill_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for src_file in sorted(p for p in src_skill_root.rglob("*") if p.is_file()):
        dst = skill_dir / src_file.relative_to(src_skill_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(src_file, dst)
        written += 1
    return written


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


# SessionStart is deliberately ABSENT: the tracker's session-start work runs
# inside the single combined SessionStart entry (_install_session_start_hook),
# because separate SessionStart entries run concurrently and their stdout is
# concatenated in completion order (see _install_session_start_hook).
SESSION_TRACKER_EVENTS = [
    ("Notification", "auth_success"),
    ("SessionEnd", ""),
    ("Stop", ""),
    ("UserPromptSubmit", ""),
]


def _install_session_tracker_hook(existing: dict, settings_path: Path):
    """Install session-account tracker hooks for Notification(auth_success), SessionEnd, Stop (heartbeat), and UserPromptSubmit.

    Registers hooks that track which Anthropic account each Claude Code session
    is using by reading ~/.claude/.credentials.json on re-auth.
    The Stop hook fires a throttled heartbeat to keep sessions visible in the dashboard.
    The tracker's SessionStart leg is NOT registered here: it runs inside the
    single combined SessionStart entry (``_install_session_start_hook``).
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
    ...     "Notification": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "SessionEnd": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "Stop": [{"hooks": [{"command": "session_account_tracker"}]}],
    ...     "UserPromptSubmit": [{"hooks": [{"command": "session_account_tracker"}]}],
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


def _chain_of_command_marker() -> str:
    """Marker to identify the jacked chain-of-command SessionStart hook."""
    return "# jacked-chain-of-command"


# The hook module behind the single combined SessionStart entry.
SESSION_START_HOOK = "session_start"

# The single-purpose SessionStart hooks the combined entry replaced. An entry
# whose command names one of these (and is jacked-managed) is a legacy layout
# to migrate away — including the hand-written `bash -c` consolidation some
# machines carry, which chains two of these names in one command string.
_LEGACY_SESSION_START_HOOKS = (
    "session_account_tracker",
    "chain_of_command_context",
    "memory_recall",
)


def _is_jacked_session_start_entry(entry: dict) -> bool:
    """True if this SessionStart entry is jacked's, in any layout we ever wrote.

    Matches the current combined entry, the three legacy single-purpose entries,
    and the hand-written ``bash -c`` consolidation (which names our hooks
    through a jacked-managed command, so it matches on the same rule). Every
    match requires a jacked-managed command path or one of our markers, so a
    foreign SessionStart hook is NEVER claimed.

    >>> _is_jacked_session_start_entry(
    ...     {"hooks": [{"command": '"/b/jacked" _hook session_start'}]})
    True
    >>> _is_jacked_session_start_entry(
    ...     {"hooks": [{"command": "/usr/local/bin/my-own-startup.sh"}]})
    False
    """
    if not isinstance(entry, dict):
        return False

    entry_str = str(entry)
    if _chain_of_command_marker() in entry_str or _session_tracker_marker() in entry_str:
        return True

    names = (SESSION_START_HOOK,) + _LEGACY_SESSION_START_HOOKS
    for h in entry.get("hooks", []) or []:
        if not isinstance(h, dict):
            continue
        cmd = str(h.get("command", ""))
        if not _is_jacked_managed_hook_path(cmd):
            continue
        if any(name in cmd for name in names):
            return True
    return False


def _install_session_start_hook(existing: dict, settings_path: Path):
    """Install THE jacked SessionStart entry: one synchronous, in-order hook.

    Claude Code runs SessionStart hook ENTRIES concurrently and concatenates
    their stdout in COMPLETION order, so the old three-entry layout (session
    tracker + chain-of-command + memory recall) emitted its blocks in a random
    order per session. That randomized the prompt preamble and defeated the
    inference-side prompt prefix cache. ONE entry, running the steps
    sequentially in-process (``jacked.data.hooks.session_start``), makes the
    preamble byte-identical every session.

    The entry is SYNCHRONOUS on purpose: an async hook's stdout is not injected
    into the session context, and injection is the whole point. The tracker step
    keeps its own daemon thread inside the module, so it still does not block.

    Migration: EVERY jacked-managed SessionStart entry (the three legacy
    single-purpose entries, a hand-written ``bash -c`` consolidation, or a stale
    combined entry) is dropped and replaced by exactly one current entry.
    Foreign SessionStart entries are left exactly where they are.
    """
    script_path = _get_data_root() / "hooks" / "session_start.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] Session start script not found: {script_path}"
        )
        console.print("[yellow]Skipping session start hook installation[/yellow]")
        return

    command_str = _build_hook_command(SESSION_START_HOOK)

    # SYNCHRONOUS entry: no "async" key.
    hook_entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": command_str,
            }
        ],
    }

    existing.setdefault("hooks", {})
    entries = existing["hooks"].get("SessionStart")
    if not isinstance(entries, list):
        entries = []

    ours = [e for e in entries if _is_jacked_session_start_entry(e)]
    if len(ours) == 1 and ours[0] == hook_entry:
        existing["hooks"]["SessionStart"] = entries
        console.print("[yellow][-][/yellow] Session start hook already configured")
        return

    # Foreign entries keep their order; ours collapses to a single trailing entry.
    kept = [e for e in entries if not _is_jacked_session_start_entry(e)]
    kept.append(hook_entry)
    existing["hooks"]["SessionStart"] = kept

    _write_settings_atomic(settings_path, existing)
    console.print(
        "[green][OK][/green] Installed session start hook (SessionStart event)"
    )


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
        # The gatekeeper feature is gone — its prompt file is dead config.
        prompt_path = _jacked_home() / ".claude" / "gatekeeper-prompt.txt"
        if prompt_path.exists():
            try:
                prompt_path.unlink()
                console.print("[dim][-][/dim] Removed gatekeeper prompt file")
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

    >>> # Smoke test: function exists and is callable
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

    >>> # Smoke test: function exists and is callable
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


def _remove_session_start_hook(settings_path: Path) -> bool:
    """Remove the jacked SessionStart entry, any layout. Returns True if removed.

    The combined entry serves several features at once (chain-of-command policy,
    memory recall, session tracking), so it is removed only when the LAST of
    them goes: a single-feature teardown leaves it in place and the module's own
    step no-ops while that feature is off. Full uninstall is that last-one-out
    case, which is why this is called there and not from a per-feature remover.

    Foreign SessionStart hooks are never touched.

    >>> # Smoke test: function exists and is callable
    >>> callable(_remove_session_start_hook)
    True
    """
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    entries = settings.get("hooks", {}).get("SessionStart")
    if not isinstance(entries, list):
        return False

    kept = [e for e in entries if not _is_jacked_session_start_entry(e)]
    if len(kept) == len(entries):
        return False

    settings["hooks"]["SessionStart"] = kept
    settings_path.write_text(json.dumps(settings, indent=2))
    console.print("[green][OK][/green] Removed session start hook")
    return True


def _remove_chain_of_command_hook(settings_path: Path) -> bool:
    """Remove a LEGACY chain-of-command SessionStart hook. Returns True if removed.

    Matches only entries whose command is a jacked-managed path AND names the
    chain_of_command_context hook, so foreign SessionStart hooks are left
    intact. The current combined entry (``_hook session_start``) is NOT matched
    here: it also carries other features, so only the full-uninstall path
    (``_remove_session_start_hook``) may drop it.

    >>> # Smoke test: function exists and is callable
    >>> callable(_remove_chain_of_command_hook)
    True
    """
    import json

    if not settings_path.exists():
        return False

    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    if "SessionStart" not in settings.get("hooks", {}):
        return False

    def _is_ours(entry: dict) -> bool:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if "chain_of_command_context" in cmd and _is_jacked_managed_hook_path(cmd):
                return True
        return False

    before = len(settings["hooks"]["SessionStart"])
    settings["hooks"]["SessionStart"] = [
        h for h in settings["hooks"]["SessionStart"] if not _is_ours(h)
    ]

    if len(settings["hooks"]["SessionStart"]) < before:
        settings_path.write_text(json.dumps(settings, indent=2))
        console.print("[green][OK][/green] Removed chain-of-command hook")
        return True

    return False


def _install_memory_capture_hook(existing: dict, settings_path: Path):
    """Install the memory-vault capture hooks (SessionEnd + PreCompact(auto)).

    Both entries are async: the SessionEnd/PreCompact triage runs without
    blocking Claude Code. Entry math is delegated to
    ``jacked.memory.hooks_config`` so the CLI and the dashboard Features route
    (M7) share one implementation. Not wired into ``_run_install`` yet (M7
    handles install parity); directly testable in the meantime.
    """
    script_path = _get_data_root() / "hooks" / "memory_capture.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] Memory capture script not found: {script_path}"
        )
        console.print("[yellow]Skipping memory capture hook installation[/yellow]")
        return

    from jacked.memory import hooks_config

    existing.setdefault("hooks", {})
    command_str = _build_hook_command("memory_capture")

    if not hooks_config.ensure_capture_entries(existing, command_str):
        console.print("[yellow][-][/yellow] Memory capture hooks already configured")
        return

    _write_settings_atomic(settings_path, existing)
    console.print(
        "[green][OK][/green] Installed memory capture hooks (SessionEnd, PreCompact)"
    )


def _install_memory_recall_hook(existing: dict, settings_path: Path):
    """Install the memory-vault recall hook (synchronous SessionStart).

    Registers a SINGLE synchronous SessionStart hook whose stdout is injected
    into the session context: the group-scoped memory brief. The entry is
    SYNCHRONOUS on purpose (``hooks_config.ensure_recall_entry`` omits the
    ``async`` key) because async SessionStart hooks do not inject stdout, and
    injecting the brief is the whole point. It is a NO-OP once the combined
    ``session_start`` entry is installed: that entry already runs the brief, in
    a fixed order with the other session-start steps. Entry math is delegated to
    ``jacked.memory.hooks_config`` so the CLI and the dashboard Features route
    (M7) share one implementation. Not wired into ``_run_install`` yet (M7
    handles install parity); directly testable in the meantime.
    """
    script_path = _get_data_root() / "hooks" / "memory_recall.py"

    if not script_path.exists():
        console.print(
            f"[red][FAIL][/red] Memory recall script not found: {script_path}"
        )
        console.print("[yellow]Skipping memory recall hook installation[/yellow]")
        return

    from jacked.memory import hooks_config

    existing.setdefault("hooks", {})
    command_str = _build_hook_command("memory_recall")

    if not hooks_config.ensure_recall_entry(existing, command_str):
        console.print("[yellow][-][/yellow] Memory recall hook already configured")
        return

    _write_settings_atomic(settings_path, existing)
    console.print(
        "[green][OK][/green] Installed memory recall hook (SessionStart event)"
    )


def _rewire_memory_hooks_on_install(home: Path, existing: dict, settings_path: Path) -> None:
    """Re-wire the memory-vault hooks during ``jacked install`` ONLY when the
    feature is already enabled.

    An upgrade re-installs an enabled feature so its hook commands stay current,
    but install NEVER turns the feature on -- enabling is the job of the
    dashboard Features toggle / ``jacked memory init``. ``load_state`` is
    tolerant, so a missing or corrupt state file reads as disabled and this is a
    silent no-op.
    """
    from jacked.memory import vault as _mem_vault

    if not _mem_vault.load_state(home).get("enabled"):
        return
    _install_memory_capture_hook(existing, settings_path)
    _install_memory_recall_hook(existing, settings_path)


def _remove_memory_hooks(settings_path: Path) -> bool:
    """Remove the jacked memory capture + recall hooks. Returns True if removed.

    Marker-scoped via ``jacked.memory.hooks_config`` (anchored on the
    ``memory_capture`` / ``memory_recall`` command substrings), so foreign
    hooks and the sibling jacked SessionStart/SessionEnd hooks are untouched.
    """
    import json

    if not settings_path.exists():
        return False

    # Tolerant read + atomic write (the repo-wide settings-writer pattern): a
    # corrupt settings.json must not abort the wider uninstall flow, and a
    # partial write must never be able to clobber the user's settings.
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        console.print(
            "[yellow]settings.json is unreadable; skipping memory hook removal[/yellow]"
        )
        return False
    if not isinstance(settings, dict):
        return False

    from jacked.memory import hooks_config

    changed = hooks_config.remove_capture_entries(settings)
    changed = hooks_config.remove_recall_entries(settings) or changed

    if changed:
        _write_settings_atomic(settings_path, settings)
        console.print("[green][OK][/green] Removed memory hooks")
        return True

    return False


def _install_statusline(home: Path, existing: dict, settings_path: Path) -> None:
    """Register the jacked statusline during ``jacked install``.

    Mutates the SHARED in-memory settings dict (the installer discipline:
    a file-only write would be clobbered by a later installer's stale
    copy) and writes atomically. The engine decides what is safe:
    a foreign statusLine is never replaced (the report says how to adopt
    it), and an explicit disable is never overridden.
    """
    from jacked import statusline_setup

    outcome = statusline_setup.sync_on_install(home, existing)
    if outcome in ("installed", "migrated"):
        _write_settings_atomic(settings_path, existing)
        verb = "Registered" if outcome == "installed" else "Updated"
        console.print(f"[green][OK][/green] {verb} Claude Code statusline")
    elif outcome == "unchanged":
        console.print("[dim][-][/dim] Statusline already registered")
    elif outcome == "skipped_foreign":
        console.print(
            "[dim][-][/dim] Kept your existing statusline "
            "(adopt jacked's with `jacked statusline enable`)"
        )
    elif outcome == "skipped_disabled":
        console.print(
            "[dim][-][/dim] Statusline disabled (enable with `jacked statusline enable`)"
        )


def _remove_statusline(settings_path: Path) -> bool:
    """Remove the jacked statusline entry. Returns True if removed.

    Only a jacked-owned command (marker: ``-m jacked.statusline``) is
    removed; a foreign statusLine is untouched. A foreign entry that
    jacked's enable took over and backed up is restored.
    """
    import json

    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        console.print(
            "[yellow]settings.json is unreadable; skipping statusline removal[/yellow]"
        )
        return False
    if not isinstance(settings, dict):
        return False

    from jacked import statusline_setup

    changed = statusline_setup.remove_on_uninstall(_jacked_home(), settings)
    if changed:
        _write_settings_atomic(settings_path, settings)
        console.print("[green][OK][/green] Removed statusline registration")
        return True
    return False


# Single source of truth for the chrome-devtools MCP npx package spec. The Codex
# installer (jacked/codex/installer._mcp_block_body) imports this + the autoConnect
# args below so the two CLIs never drift on the version/flags they register.
CHROME_DEVTOOLS_NPX_PACKAGE = "chrome-devtools-mcp@latest"

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
        "npx", CHROME_DEVTOOLS_NPX_PACKAGE, *CHROME_DEVTOOLS_MODES["autoConnect"],
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
                "npx", CHROME_DEVTOOLS_NPX_PACKAGE] + CHROME_DEVTOOLS_MODES[mode]
    add = _run_claude_mcp(*add_args, timeout=30)

    if add is not None and add.returncode == 0:
        return True, f"Chrome DevTools MCP set to {mode}"

    # Rollback: restore previous config if add failed
    if had_existing:
        _run_claude_mcp(
            "add", "-s", "user", "chrome-devtools", "--",
            "npx", CHROME_DEVTOOLS_NPX_PACKAGE, *prev_mode_args,
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
@click.option("--no-rules", is_flag=True, help="Skip behavioral rules in CLAUDE.md")
@click.option(
    "--no-tray",
    is_flag=True,
    help="Skip registering/starting the tray icon (the tray is on by default)",
)
@click.option(
    "--no-statusline",
    is_flag=True,
    help="Skip registering the Claude Code statusline (on by default; never replaces a statusline jacked does not own)",
)
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
@click.option(
    "--no-codex",
    is_flag=True,
    help="Skip installing skills/commands/rules/gatekeeper into Codex (auto-detected by default)",
)
@click.option(
    "--packs",
    "packs_csv",
    default=None,
    help="Comma-separated skill packs to explicitly enable and install (e.g. --packs marketing). Default packs install anyway; use this to add a non-default pack or re-enable one you disabled. See `jacked packs list`.",
)
@click.option(
    "--no-packs",
    is_flag=True,
    help="Skip skill packs entirely for this install (does not change which packs are enabled).",
)
def install(
    sounds: bool,
    no_rules: bool,
    no_tray: bool,
    no_statusline: bool,
    force: bool,
    as_json: bool,
    no_codex: bool,
    packs_csv: str | None,
    no_packs: bool,
):
    """Auto-install skills, agents, commands, rules, and hooks.

    Installs the skills/commands/agents suite, behavioral rules, the
    session-account tracker + QA hooks, and the tray service. Also prunes
    artifacts from retired features (the security gatekeeper hook and the
    session-indexing Stop hook) left behind by older versions.

    Default skill packs install too (opt out with --no-packs, or durably
    remove one with `jacked packs disable NAME`).
    """
    import json

    home = _jacked_home()
    pkg_root = _get_data_root()

    if no_packs and packs_csv:
        console.print(
            "[red][FAIL][/red] --no-packs and --packs are contradictory; pass one."
        )
        raise SystemExit(1)

    # Validate --packs at the VERY top, before any install work. A typo must
    # fail fast with the list of valid packs instead of running a full install
    # and only then exiting 1 with no summary. Duplicates collapse to a single
    # install. The validated list is handed to _run_packs_phase (which keeps its
    # own re-validation as a backstop but should never hit it from here).
    validated_packs: list[str] = []
    if packs_csv:
        from jacked import packs as _packs_val

        _registry = _packs_val.load_registry(pkg_root)
        _requested = [p.strip() for p in packs_csv.split(",") if p.strip()]
        _seen: set[str] = set()
        _requested = [p for p in _requested if not (p in _seen or _seen.add(p))]
        _unknown = [p for p in _requested if p not in _registry]
        if _unknown:
            _packs_unknown_name(_unknown, _registry)  # prints + raises SystemExit(1)
        validated_packs = _requested

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

    # In --json mode, suppress the per-step "[OK] ..." chatter (and the same
    # chatter emitted by helper functions) so stdout carries only the JSON
    # record. The try/finally guarantees the module-level console is restored
    # even if install raises — otherwise a later in-process command (tray)
    # would silently inherit quiet=True.
    _prev_quiet = console.quiet
    if as_json:
        console.quiet = True
    try:
        _installed_skill_names = _run_install(
            home=home,
            pkg_root=pkg_root,
            sounds=sounds,
            no_rules=no_rules,
            no_tray=no_tray,
            no_statusline=no_statusline,
            force=force,
            as_json=as_json,
        )
    finally:
        console.quiet = _prev_quiet

    # --- Change summary (manifest-driven) ---
    # Hash source-now, diff against the prior manifest, prune artifacts that
    # jacked installed before but no longer ships, then persist the new
    # manifest + the dashboard-readable last-install record.
    _current_hashes = _mani.hash_source(pkg_root)
    # Full-dir hashes of the skill dirs we just wrote, so a later run can tell a
    # user-edited SIDECAR from jacked's own content (SKILL.md alone can't).
    # Hash ONLY the skills _run_install actually wrote: a skill skipped on
    # OSError leaves the USER's dir at that path, and recording its hash would
    # mark their dir jacked-owned — a later uninstall would then delete it.
    _current_hashes[_mani.SKILLS_DIRS_KEY] = _mani.hash_installed_skill_dirs(
        home,
        {
            n: h
            for n, h in _current_hashes.get("skills", {}).items()
            if n in _installed_skill_names
        },
    )
    _d = _mani.diff(_prior_manifest, _current_hashes)
    # Pass the prior manifest so a skill dir the user modified/recreated is never
    # deleted by the prune (upgrade runs this automatically — high exposure).
    _pruned, _preserved = _mani.prune_removed(_d, home, _prior_manifest)
    # Belt-and-braces: with no usable prior manifest the diff prunes nothing, and
    # writing the new manifest below would strand a stale command copy of a
    # migrated name forever (no later diff or uninstall can see it). The sweep
    # deletes only manifest-proven jacked copies; anything unproven is left in
    # place and reported as stale (the skill shadows it, so it is inert).
    _swept, _stale_cmds = _mani.sweep_migrated_commands(
        home, _mani.migrated_skill_names(pkg_root), _prior_manifest,
    )
    _pruned += _swept
    _now = datetime.now(timezone.utc).isoformat()
    _mani.write(_manifest_path, _ver, _current_hashes, _now)
    _record = _isum.build_record(_d, _prior_version, _ver, _now)
    # Outcome, not intent: the diff says what SHOULD go; record what actually
    # did, and keep the rendered "removed" lines honest for preserved files.
    _record["pruned"] = _pruned
    _record["preserved"] = _preserved
    _record["stale_commands"] = _stale_cmds
    # Strip every intended removal that did not actually happen (preserved to
    # backups, kept-in-place modified skill dir, or a locked file the prune
    # could not touch) so neither the render nor the dashboard claims it's gone.
    _kept_in_place: list[str] = []
    for _cat, _ch in _record["changes"].items():
        _rm = _ch.get("removed")
        if not _rm:
            continue
        for _nm in list(_rm):
            _id = f"{_cat}/{_nm}"
            if _id not in _pruned:
                _rm.remove(_nm)
                if _id not in _preserved:
                    _kept_in_place.append(_id)
    _record["kept_in_place"] = _kept_in_place
    _isum.write_last_install(_record, home / ".claude" / "jacked-last-install.json")

    # --- Codex pass (auto-detected) ---
    # Deploy the same skills/commands/rules into Codex's native locations
    # (~/.agents/skills, ~/.codex/prompts, ~/.codex/AGENTS.md) with its own
    # manifest. Best-effort: a Codex failure never fails the Claude install.
    _codex_summary = None
    _codex_failed = False
    _codex_detected = False
    if not no_codex:
        try:
            from jacked.codex import installer as _cdx

            if _cdx.codex_present():
                _codex_detected = True
                _codex_summary = _cdx.install_codex(
                    pkg_root,
                    version=_ver,
                    now_iso=_now,
                )
        except Exception:
            logger.exception("Codex install pass failed (Claude install unaffected)")
            _codex_failed = True

    # --- Skill packs pass ---
    # Enable + install/refresh any requested (and any already-enabled) skill
    # packs. include_codex mirrors the Codex pass condition above (reuse the
    # detection result, never re-detect). Unknown --packs names were already
    # rejected up top, so the phase never exits here. Wrap it like the sibling
    # Codex pass: any failure prints loud but the main install stays unaffected.
    _packs_record: dict = {}
    try:
        _packs_record = _run_packs_phase(
            home, pkg_root, validated_packs, _codex_detected, as_json,
            no_packs=no_packs,
        )
    except Exception:
        logger.exception("Skill packs phase failed (install unaffected)")
        console.print(
            "[yellow][-][/yellow] Skill packs phase failed; see logs. "
            "The main install is unaffected."
        )

    if as_json:
        if _codex_summary is not None:
            _record["codex"] = {
                "skills": _codex_summary.skills,
                "prompts": _codex_summary.prompts,
                "agents": _codex_summary.agents,
                "rules": _codex_summary.rules,
                "hooks": _codex_summary.hooks,
                "mcp": _codex_summary.mcp,
                "removed": _codex_summary.removed,
                "preserved": _codex_summary.preserved,
                "changed": _codex_summary.changed,
            }
        elif _codex_failed:
            _record["codex"] = {"failed": True}
        if _packs_record:
            _record["packs"] = _packs_record
        click.echo(json.dumps(_record))
    else:
        console.print("")
        console.print(_isum.render_terminal(_record))
        # render_terminal already lists diff-driven removals; only the sweep's
        # extra deletions and the not-deleted outcomes need their own lines.
        if _swept:
            console.print(
                f"[green][OK][/green] Removed {len(_swept)} stale command "
                "cop(ies) of names that ship as skills now: " + ", ".join(_swept)
            )
        for _item in _preserved:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified {_item} under "
                "~/.claude/jacked-backups/ (no longer matches what jacked installed)"
            )
        for _item in _kept_in_place:
            console.print(
                f"[yellow][!][/yellow] Left {_item} in place (modified or "
                "locked; see the log for the exact reason)"
            )
        for _item in _stale_cmds:
            console.print(
                f"[yellow][!][/yellow] ~/.claude/{_item} collides with a shipped "
                "skill of the same name (the skill wins in Claude Code). jacked "
                "can't confirm it's jacked's old copy, so it was left alone. "
                "Delete it yourself if it's a pre-0.96 leftover."
            )
        if _codex_summary is not None:
            _mcp = _codex_summary.mcp
            if _mcp in ("added", "updated"):
                _mcp_suffix = ", chrome-devtools MCP → config.toml"
            elif _mcp == "preexisting":
                _mcp_suffix = ", chrome-devtools MCP (kept your existing entry)"
            elif _mcp == "skipped-unparseable":
                _mcp_suffix = ", chrome-devtools MCP skipped (config.toml unparseable)"
            else:
                _mcp_suffix = ""
            console.print(
                f"[green][OK][/green] Codex: {len(_codex_summary.skills)} skills "
                f"→ ~/.agents/skills, {len(_codex_summary.prompts)} prompts "
                f"→ ~/.codex/prompts, {len(_codex_summary.agents)} agents "
                f"→ ~/.codex/agents, rules → AGENTS.md{_mcp_suffix}"
            )
            if _codex_summary.removed:
                console.print(
                    f"[green][OK][/green] Codex: removed "
                    f"{len(_codex_summary.removed)} no-longer-shipped: "
                    + ", ".join(_codex_summary.removed)
                )
            for _item in _codex_summary.preserved:
                console.print(
                    f"[yellow][!][/yellow] Codex: preserved your existing "
                    f"~/.agents/{_item} under ~/.agents/jacked-backups/skills/"
                )
            if _codex_summary.hooks_added:
                console.print(
                    "[yellow][!][/yellow] Codex requires one-time hook trust - "
                    "run /hooks inside Codex to approve the jacked QA hook."
                )
        elif _codex_failed:
            console.print(
                "[yellow][WARN][/yellow] Codex pass failed (see logs); "
                "Claude install unaffected."
            )
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
    no_tray: bool = False,
    no_statusline: bool = False,
) -> set:
    """Run the artifact/hook/rules installation (no manifest, no summary).

    Split out of `install` so the change-summary orchestration can wrap it in
    a try/finally that always restores console state.

    Returns the names of the skills actually WRITTEN this run. A skill that
    hit the per-skill OSError skip is absent — the caller must not record a
    dir hash for it, because the dir at that path is the USER's (recording it
    would mark their dir jacked-owned and a later uninstall would delete it).
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

    # Retired-feature prune (0.70.0) — operate on the SHARED `existing` dict
    # (later hook installers write it back, so a file-only prune would be
    # clobbered by their stale in-memory copy).
    _pruned_legacy = []
    # (a) `jacked index` Stop hook — the retired command would error on
    # every session stop.
    _legacy_stop = [
        e for e in existing["hooks"]["Stop"]
        if any("jacked index" in h.get("command", "") for h in e.get("hooks", []))
    ]
    if _legacy_stop:
        existing["hooks"]["Stop"] = [
            e for e in existing["hooks"]["Stop"] if e not in _legacy_stop
        ]
        _pruned_legacy.append("session-indexing Stop hook")
    # (b) security gatekeeper PreToolUse/PermissionRequest entries — a stale
    # entry would fire `jacked _hook security_gatekeeper` on every tool call.
    for _ev in ("PreToolUse", "PermissionRequest"):
        _entries = existing["hooks"].get(_ev, [])
        _kept = [e for e in _entries if "security_gatekeeper" not in str(e)]
        if len(_kept) != len(_entries):
            existing["hooks"][_ev] = _kept
            _pruned_legacy.append(f"gatekeeper {_ev} hook")
    if _pruned_legacy:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(existing, indent=2))
        console.print(
            "[green][OK][/green] Removed legacy hooks retired in 0.70.0: "
            + ", ".join(_pruned_legacy)
        )
    # The gatekeeper's prompt file is dead config for a removed feature.
    _gk_prompt = home / ".claude" / "gatekeeper-prompt.txt"
    if _gk_prompt.exists():
        try:
            _gk_prompt.unlink()
            console.print("[dim][-][/dim] Removed gatekeeper prompt file (feature retired)")
        except Exception:
            pass

    # Install skills — iterate all skills/*/SKILL.md in data root
    # Claude Code expects skills in subdirectories with SKILL.md
    skills_src_dir = pkg_root / "skills"
    skill_count = 0
    installed_skill_names: set = set()
    if skills_src_dir.exists():
        # Prior manifest identifies which colliding dirs are jacked's OWN copies.
        # It is still the pre-install manifest here: `install` rewrites it only
        # after _run_install returns.
        from jacked import install_manifest as _skill_mani

        _prior_skills = _skill_mani.load(home / ".claude" / "jacked-manifest.json")
        for skill_md in skills_src_dir.glob("*/SKILL.md"):
            skill_name = skill_md.parent.name
            skill_dir = home / ".claude" / "skills" / skill_name
            # Never destroy a dir that isn't ours: a user's own same-named skill
            # (or one they edited) is moved aside first, and the move is REPORTED
            # immediately — a Ctrl-C later in the loop must not leave the user's
            # dir relocated without them ever seeing where it went. One
            # unwritable skill is skipped, not fatal to the whole install.
            try:
                _backup = _skill_mani.preserve_user_skill_dir(
                    skill_dir, skill_name, skill_md.parent, _prior_skills,
                )
                if _backup:
                    console.print(
                        f"[yellow][!][/yellow] Preserved your existing skill "
                        f"{skill_name} (not installed by jacked) at {_backup}"
                    )
                _copy_skill_tree(skill_md.parent, skill_dir)
            except OSError as _skill_err:
                console.print(
                    f"[yellow][!][/yellow] Skipped skill {skill_name}: {_skill_err}"
                )
                continue
            skill_count += 1
            installed_skill_names.add(skill_name)
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

    # Static permission audit (independent of the retired gatekeeper).
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
                "[dim]Run 'jacked permissions audit' for full details, "
                "or 'jacked permissions audit --fix' to prune them interactively.[/dim]"
            )
        else:
            console.print("[green][AUDIT] Permission rules look clean[/green]")

    # Install session-account tracker hooks (always — lightweight, no deps)
    _install_session_tracker_hook(existing, settings_path)

    # Install THE SessionStart hook: one synchronous entry that runs the tracker,
    # the chain-of-command auto-load, and the memory recall brief IN ORDER
    # (concurrent entries would randomize the injected preamble).
    _install_session_start_hook(existing, settings_path)

    # Install QA suggestion hook (always — lightweight, no deps)
    _install_qa_hook(existing, settings_path)

    # Re-wire the memory-vault hooks when the feature is already enabled
    # (upgrade parity; install never enables the feature itself).
    _rewire_memory_hooks_on_install(home, existing, settings_path)

    # Statusline: ON by default. Registers `"<abs-python>" -m jacked.statusline`
    # under statusLine, migrating a stale jacked-owned command in place. Never
    # replaces a foreign statusLine and never overrides an explicit disable
    # (dashboard toggle / `jacked statusline disable`).
    if not no_statusline:
        _install_statusline(home, existing, settings_path)

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

    # Tray: ON by default (pystray/Pillow are core deps) — register login
    # autostart and start the tray now. `--no-tray` opts out.
    if not no_tray:
        _setup_tray_autostart()

    return installed_skill_names


def _detect_codex_for_packs() -> bool:
    """Auto-detect Codex for a standalone skill-pack op (enable/update).

    Mirrors the guard the install path uses: best-effort, and a Codex probe
    failure just means "don't target Codex" rather than an error.
    """
    try:
        from jacked.codex import installer as _cdx

        return _cdx.codex_present()
    except Exception:
        logger.debug(
            "Codex detection for skill packs failed; targeting claude-code only",
            exc_info=True,
        )
        return False


def _rich_escape(text: str) -> str:
    """Escape Rich markup in untrusted text (npx stderr tails, lockfile-derived
    skill names, user argv) before console.print interpolation. npm error
    output routinely contains bracketed tokens that otherwise raise
    rich.errors.MarkupError or render live [link] markup."""
    from rich.markup import escape

    return escape(text or "")


def _packs_unknown_name(names, registry: dict) -> None:
    """Print an actionable unknown-pack error and exit 1.

    Accepts a single name or a list of names — one message shape everywhere.
    Lists the valid pack names (or a clear "none available" when the registry
    is empty). Never returns - always raises SystemExit(1).
    """
    if isinstance(names, str):
        names = [names]
    valid = ", ".join(sorted(registry)) or "(none available)"
    console.print(
        f"[red][FAIL][/red] Unknown skill pack(s): {_rich_escape(', '.join(names))}. "
        f"Valid packs: {valid}."
    )
    raise SystemExit(1)


def _pack_trust_line(pack) -> str:
    """One-line provenance/consent notice shown before installing a pack.

    Plain text (no em-dashes): skills are instructions the user's agents will
    follow, so we name the count, the upstream source, and the homepage to
    review before trusting them.
    """
    n = len(pack.skills)
    return (
        f"Installing {n} skill{'s' if n != 1 else ''} from {pack.source} "
        f"(upstream main branch). Skills are instructions your agents will "
        f"follow; review the source at {pack.homepage}."
    )


def _emit_pack_result(name: str, res, total: int, as_json: bool, results: dict) -> None:
    """Record and print the outcome for ONE pack.

    ``res`` is a PackOpResult-like carrying ``.ok``, ``.installed`` (list),
    ``.missing`` (list), ``.message``, and optional ``.skipped`` (list). Builds
    ``results[name]`` (installed as a count) and prints the [OK]/[FAIL] line plus
    a loud [!] line for any skipped (pre-existing user-owned) skill dirs.
    """
    installed = list(getattr(res, "installed", []) or [])
    missing = list(getattr(res, "missing", []) or [])
    skipped = list(getattr(res, "skipped", []) or [])
    results[name] = {
        "ok": bool(res.ok),
        "installed": len(installed),
        "missing": missing,
        "skipped": skipped,
        "message": getattr(res, "message", ""),
    }
    if as_json:
        return
    if res.ok:
        console.print(
            f"[green][OK][/green] Pack '{name}': "
            f"{len(installed)}/{total} skills installed"
        )
    else:
        console.print(f"[red][FAIL][/red] {_rich_escape(getattr(res, 'message', ''))}")
    if skipped:
        console.print(
            f"[yellow][!][/yellow] Pack '{name}': left "
            f"{_rich_escape(', '.join(skipped))} untouched (a skill dir you already own)"
        )


def _run_packs_phase(
    home: Path,
    pkg_root: Path,
    requested_packs: list[str],
    include_codex: bool,
    as_json: bool,
    no_packs: bool = False,
) -> dict:
    """Install/refresh the effectively-enabled skill packs as part of `jacked install`.

    Effectively-enabled = every registry pack marked ``default: true`` that the
    user has not explicitly disabled, plus any pack explicitly enabled (via a
    prior toggle or ``--packs`` this run). Returns
    ``{name: {"ok", "installed", "missing", "skipped", "message"}}`` for the JSON
    record (empty when nothing is enabled, ``--no-packs`` was passed, or npx is
    missing). Never fails the overall install: a pack error prints loud but the
    caller's exit code stays 0.
    """
    from types import SimpleNamespace

    from jacked import packs as _packs

    registry = _packs.load_registry(pkg_root)

    if no_packs:
        if not as_json:
            console.print("[dim][-] Skill packs skipped (--no-packs).[/dim]")
        return {}

    # --packs explicitly enables (durably) + installs the named packs. This is
    # additive to the default-on set; install() already rejected unknown names,
    # so the registry re-check here is a backstop.
    if requested_packs:
        unknown = [p for p in requested_packs if p not in registry]
        if unknown:
            _packs_unknown_name(unknown, registry)
        for name in requested_packs:
            _packs.set_enabled(home, name, True)

    # Deregistered packs: an explicit enabled decision for a pack unknown to this
    # build. Never a silent skip — say the skills were left as-is.
    if not as_json:
        for name in [n for n in _packs.enabled_pack_names(home) if n not in registry]:
            console.print(
                f"[yellow][!][/yellow] Pack '{name}' is enabled but unknown to "
                "this jacked version; its skills were left untouched."
            )

    targets = _packs.effective_enabled_pack_names(home, registry)
    results: dict = {}
    if not targets:
        return results

    if _packs.find_npx() is None:
        if not as_json:
            console.print(
                "[yellow][-][/yellow] Skill packs skipped: Node.js/npx not found "
                "(install Node 18+)"
            )
        return results

    # Discriminate install vs refresh, not by "was it enabled this run" but by
    # whether every skill is already on disk AND tracked as OURS (own-source in
    # the lockfile). A pack that's fully present and ours is refreshed via one
    # batched update; anything else -- missing skills, or skills shadowed by a
    # same-named dir from another source or a user's own hand-made skill -- goes
    # to install, where the collision guard surfaces the shadow loudly instead
    # of the refresh path silently reporting "up to date" (it can only update
    # own-source skills, so a foreign shadow would be a silent no-op).
    to_install: list[str] = []
    to_update: list[str] = []
    for name in targets:
        st = _packs.pack_status(registry[name], home)
        skills = st.get("skills", [])
        fully_ours = bool(skills) and all(
            s.get("installed") and s.get("source_ok") is True for s in skills
        )
        if fully_ours:
            to_update.append(name)
        else:
            to_install.append(name)

    for name in to_install:
        pack = registry[name]
        if not as_json:
            console.print(_pack_trust_line(pack))
        res = _packs.install_pack(pack, home, include_codex=include_codex)
        _emit_pack_result(name, res, len(pack.skills), as_json, results)

    if to_update:
        update_targets = [registry[n] for n in to_update]
        upd = _packs.update_packs(update_targets, home, include_codex=include_codex)
        # Per-pack attribution comes from upd.per_pack[name] — the aggregate
        # upd.ok is False whenever ANY pack in the batch failed, so keying the
        # per-pack line off it would print [FAIL] on a healthy sibling.
        per_pack = getattr(upd, "per_pack", None) or {}
        for name in to_update:
            pack = registry[name]
            res = per_pack.get(name)
            if res is None:
                # Fallback for a packs.py without per_pack: derive this pack's
                # truth from on-disk status, never from the batch aggregate, so
                # a sibling's failure still can't mark this one FAIL.
                st = _packs.pack_status(pack, home)
                skills = st.get("skills", [])
                res = SimpleNamespace(
                    installed=[s["name"] for s in skills if s.get("installed")],
                    missing=[s["name"] for s in skills if not s.get("installed")],
                    skipped=[],
                    message=getattr(upd, "message", ""),
                )
                res.ok = not res.missing
            _emit_pack_result(name, res, len(pack.skills), as_json, results)

    return results


def _tray_extra_installed() -> bool:
    """True if pystray is importable. pystray/Pillow are CORE deps now (tray on
    by default), so this is False only on a broken/incomplete install."""
    try:
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def _live_manifest(paths):
    """Return the instance manifest only when it is valid and names a live PID.

    Returns ``None`` for a missing, unreadable, invalid, or proven-stale
    manifest, so callers can fall through to quarantine and reprovisioning.
    """
    from jacked.service.instance import read_manifest

    try:
        manifest = read_manifest(paths.manifest)
    except (OSError, ValueError):
        return None
    if _manifest_is_proven_stale(paths.manifest):
        return None
    return manifest


def _already_owned_and_running(manifest, paths) -> bool:
    """True when a live supervised instance is exactly what recovery installs.

    Two independent pieces of evidence must agree: the manifest generation
    equals the generation this build provisions, and the native login item is
    owned and enabled. A drifted generation or a disabled or foreign login
    item still falls through to a real recovery.
    """
    from jacked.service.autostart import AutostartState, inspect_autostart
    from jacked.service.lifecycle import provision_service_contract

    try:
        spec, _environment = provision_service_contract(paths=paths)
    except (OSError, ValueError):
        return False
    if manifest.generation != spec.generation:
        return False
    return inspect_autostart().state is AutostartState.OWNED_ENABLED


def _release_manual_owner(paths, *, timeout: float = 10.0) -> tuple[bool, str]:
    """Hand ownership back from a manually started instance.

    Sends an authenticated SHUTDOWN over the control channel, then waits for
    the instance to remove its manifest. Returns ``(released, detail)`` where
    ``detail`` is a short user-facing sentence describing the outcome. Shared
    by `service install` and `service recover` so both hand off identically.
    """
    import time

    from jacked.service.ipc import ControlAction, send_native_control

    try:
        response = send_native_control(paths.manifest, ControlAction.SHUTDOWN)
    except (OSError, ValueError) as exc:
        return False, f"manual service refused handoff ({type(exc).__name__})"
    if not response.get("ok"):
        return False, "manual service refused handoff"
    deadline = time.monotonic() + timeout
    while paths.manifest.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if paths.manifest.exists():
        return False, "manual service did not release ownership"
    return True, "manual service released ownership"


def _ensure_autostart_and_running(
    port: int, *, one_shot_host: str | None = None, label: str = "Service"
) -> bool:
    """Register login autostart and make sure the service is running now.

    Returns True when the exact service generation is running afterwards.

    macOS: ``install_autostart`` bootstraps it via launchd (starts immediately)
    unless the service is already running. Windows/Linux: ``install_autostart``
    only registers the login entry, so we spawn the detached service ourselves
    when nothing is already listening. Idempotent and non-fatal - never aborts
    the caller.

    The autostart artifact is always host-free (the bind host lives in the
    settings DB and is resolved at every boot). ``one_shot_host`` applies ONLY
    to the immediate detached spawn on Windows/Linux - a caller-supplied
    unmapped ``--host`` that is deliberately not persisted; the launchd path
    always boots host-free.
    """
    from jacked.service.lifecycle import (
        default_service_paths,
        discover_service,
        inspect_native_artifact,
        install_native_owned,
        native_artifact_path,
        provision_service_contract,
        spawn_exact_service,
    )
    from jacked.service.instance import read_manifest
    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors import (
        ArtifactDisposition,
        is_known_legacy_artifact,
    )

    paths = default_service_paths()
    try:
        spec, environment = provision_service_contract(paths=paths)
        endpoint = discover_service(paths)
        if endpoint.source == "manifest":
            existing = read_manifest(paths.manifest)
            if existing.supervisor == SupervisorKind.MANUAL.value:
                artifact_path = native_artifact_path(spec, paths=paths)
                inspected = inspect_native_artifact(
                    spec,
                    artifact_path,
                    environment=environment,
                )
                # A pre-v2 definition jacked itself wrote is NOT foreign: the
                # supervisor install backs it up and replaces it. Only refuse
                # an artifact nobody here recognises.
                if (
                    inspected.artifact.disposition is ArtifactDisposition.FOREIGN
                    and not is_known_legacy_artifact(
                        artifact_path, spec.service_id, spec.supervisor
                    )
                ):
                    console.print(
                        "[red]Error:[/red] supervisor artifact is foreign; "
                        "run `jacked service recover`"
                    )
                    return False
                released, detail = _release_manual_owner(paths)
                if not released:
                    console.print(f"[red]Error:[/red] {detail}")
                    return False
        result = (
            spawn_exact_service(spec, environment=environment)
            if spec.supervisor is SupervisorKind.MANUAL
            else install_native_owned(spec, environment=environment, paths=paths)
        )
    except (OSError, ValueError) as exc:
        console.print(
            f"[red]Error:[/red] safe service install failed ({type(exc).__name__})"
        )
        return False
    if not result.ok:
        console.print(
            f"[red]Error:[/red] safe service install refused: {result.reason}"
        )
        return False
    ready = _wait_owned_service_ready(
        paths,
        expected_generation=spec.generation,
    )
    if ready is None:
        console.print(
            "[red]Error:[/red] native manager accepted the definition, but the "
            "exact service generation did not become ready"
        )
        return False
    port_label = ready.get("port") or port
    console.print("[green][OK][/green] Autostart registered from exact ServiceSpec")
    console.print(
        f"[green][OK][/green] {label} activation requested -> "
        f"http://127.0.0.1:{port_label}"
    )
    return True

def _setup_tray_autostart() -> None:
    """Register login autostart and start the tray now so `jacked install` makes
    the icon appear. The tray is ON by default (pystray/Pillow are core deps);
    `jacked install --no-tray` skips this. No-ops when pystray can't import, or
    on a headless box (no display) so CI/servers run the service without trying
    to draw a broken icon."""
    if not _tray_extra_installed():
        return
    if _is_headless():
        console.print(
            "[dim][-][/dim] Headless environment (no display) — skipping tray icon. "
            "Run `jacked service start` to run the service without a tray."
        )
        return
    from jacked.service import DEFAULT_PORT

    _ensure_autostart_and_running(DEFAULT_PORT, label="Tray")


# Plugins that jacked's behaviors and skills genuinely depend on. Missing
# any of these means key workflows are broken, so install surfaces them as a
# blocker; the full (optional/recommended) list lives in `jacked doctor`.
_REQUIRED_PLUGINS = {
    "superpowers@claude-plugins-official": "brainstorming, planning, TDD, subagent workflows",
    "playwright@claude-plugins-official": "/qa and /ux browser testing",
    "commit-commands@claude-plugins-official": "/commit, /commit-push-pr",
    "code-review@claude-plugins-official": "/code-review multi-agent PR review",
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


def _port_owner_hint(port: int | None = None) -> str:
    """Return the platform's command for "who is listening on this port".

    The old hardcoded ``lsof`` line was macOS/Linux-only, so the one message
    a stuck Windows user most needed handed them a command that doesn't exist.
    """
    from jacked.service import DEFAULT_PORT

    port = DEFAULT_PORT if port is None else port
    if sys.platform == "win32":
        return f"netstat -ano | findstr :{port}"
    return f"lsof -iTCP:{port} -sTCP:LISTEN"


@main.command()
def doctor():
    """Diagnose a broken jacked install and print recovery commands.

    Checks version, install method, launchd/systemd plist/unit, and
    service running state (via PID + HTTP probe, not just port).
    Prints exact commands to paste for any detected issue.

    A 200 on the port is NOT sufficient to call the service healthy: a
    foreground ``jacked webux`` serves the same dashboard but runs no tray
    icon and writes no PID file, so the HTTP probe is cross-checked against
    a live PID file before reporting HEALTHY.

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
            if resp.status_code == 200 and pid_alive:
                console.print(
                    f"[bold]Service:[/bold] [green]HEALTHY[/green] "
                    f"(port {DEFAULT_PORT}, HTTP 200, pid {pid_info['pid']})"
                )
            elif resp.status_code == 200:
                # Port answers, but nothing we manage owns it. The usual
                # culprit is a hand-run `jacked webux`: it serves this exact
                # dashboard, so the HTTP probe passes, but it has no tray icon
                # and writes no PID file — and while it squats the port every
                # `service start` dies with "port already in use".  Reporting
                # HEALTHY here is what let that hide for days.
                console.print(
                    f"[bold]Service:[/bold] [yellow]DASHBOARD UP, "
                    f"NO TRAY SERVICE[/yellow] (port {DEFAULT_PORT}, HTTP 200)"
                )
                if pid_info is None:
                    console.print(f"  No PID file at {PID_FILE}.")
                else:
                    console.print(
                        f"  Stale PID file at {PID_FILE} "
                        f"(pid {pid_info['pid']} is dead)."
                    )
                console.print(
                    "  Something is serving the dashboard, but it is not the "
                    "managed service — so there is no tray icon."
                )
                console.print(
                    "  Most likely a foreground [cyan]jacked webux[/cyan] "
                    "holding the port."
                )
                console.print(f"  Find the owner: [cyan]{_port_owner_hint()}[/cyan]")
                console.print(
                    "  Recovery: stop that process, then "
                    "[cyan]jacked service start[/cyan]"
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
                    f"Run [cyan]{_port_owner_hint()}[/cyan] "
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

    from jacked import install_manifest as _mani

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
            "Remove jacked from Claude Code? (Your local database is kept)"
        ):
            console.print("Cancelled")
            return

    console.print("[bold]Uninstalling Jacked...[/bold]\n")

    # Also remove sound, security, session tracker hooks, and behavioral rules during full uninstall
    _remove_sound_hooks(settings_path)
    _remove_security_hook(settings_path)
    _remove_session_tracker_hooks(settings_path)
    _remove_qa_hook(settings_path)
    _remove_chain_of_command_hook(settings_path)
    # Every feature the combined SessionStart entry serves is going away with
    # this uninstall, so this is the last-one-out case that may drop it.
    _remove_session_start_hook(settings_path)
    # Remove the memory-vault hook entries unconditionally — stripping entries
    # for a tool being uninstalled is always correct (the vault files stay put).
    _remove_memory_hooks(settings_path)
    _remove_statusline(settings_path)
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
    _manifest_path = home / ".claude" / "jacked-manifest.json"
    _uninstall_manifest, _manifest_status = _mani.load_with_status(_manifest_path)
    if _manifest_status == "corrupt":
        console.print(
            f"[yellow][!][/yellow] The install manifest at {_manifest_path} is "
            "unreadable. Skill removal falls back to a content comparison "
            "against the packaged skills."
        )
    if skills_src_dir.exists():
        for skill_md in skills_src_dir.glob("*/SKILL.md"):
            skill_name = skill_md.parent.name
            skill_dir = home / ".claude" / "skills" / skill_name
            if not skill_dir.exists():
                continue
            # Delete only a dir jacked owns: one that still matches the manifest,
            # or (no/unreadable manifest) one whose every file still matches the
            # packaged source. Anything else stays, with an honest reason.
            _remove, _keep_why = _mani.skill_removal_decision(
                skill_dir, skill_name, _uninstall_manifest, skill_md.parent,
            )
            if _remove:
                # A symlink gets unlinked, never rmtree'd: rmtree raises on a
                # symlink argument, which pre-guard aborted the whole uninstall
                # on an editable-install skill dir. One unremovable skill is
                # reported and skipped, not fatal to the uninstall.
                try:
                    if skill_dir.is_symlink():
                        skill_dir.unlink()
                    else:
                        shutil.rmtree(skill_dir)
                    skill_count += 1
                except OSError as _rm_err:
                    console.print(
                        f"[yellow][!][/yellow] Could not remove skill "
                        f"{skill_name}: {_rm_err}"
                    )
            else:
                console.print(
                    f"[yellow][!][/yellow] Kept skill {skill_name}: {_keep_why}"
                )
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
        _agents_kept: list[str] = []
        for agent_file in agents_src.glob("*.md"):
            _act = _mani.remove_or_preserve_flat(
                agents_dst / agent_file.name, "agents", agent_file.name,
                _uninstall_manifest, src=agent_file,
            )
            if _act == "removed":
                agent_count += 1
            elif _act == "preserved":
                _agents_kept.append(agent_file.name)
        if agent_count > 0:
            console.print(f"[green][OK][/green] Removed {agent_count} agents")
        elif not _agents_kept:
            console.print("[yellow][-][/yellow] No jacked agents found")
        for _n in _agents_kept:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified agents/{_n} under "
                "~/.claude/jacked-backups/"
            )
    else:
        console.print("[yellow][-][/yellow] Agents directory not found")

    # Remove only jacked-installed commands (not the whole directory!)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    if commands_src.exists() and commands_dst.exists():
        cmd_count = 0
        _cmds_kept: list[str] = []
        for cmd_file in commands_src.glob("*.md"):
            _act = _mani.remove_or_preserve_flat(
                commands_dst / cmd_file.name, "commands", cmd_file.name,
                _uninstall_manifest, src=cmd_file,
            )
            if _act == "removed":
                cmd_count += 1
            elif _act == "preserved":
                _cmds_kept.append(cmd_file.name)
        if cmd_count > 0:
            console.print(f"[green][OK][/green] Removed {cmd_count} commands")
        elif not _cmds_kept:
            console.print("[yellow][-][/yellow] No jacked commands found")
        for _n in _cmds_kept:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified commands/{_n} under "
                "~/.claude/jacked-backups/"
            )
    else:
        console.print("[yellow][-][/yellow] Commands directory not found")

    # Remove only jacked-installed lenses (not the whole directory!)
    lenses_src = pkg_root / "lenses"
    lenses_dst = home / ".claude" / "lenses"
    if lenses_src.exists() and lenses_dst.exists():
        lens_count = 0
        _lenses_kept: list[str] = []
        for lens_file in lenses_src.glob("*.md"):
            _act = _mani.remove_or_preserve_flat(
                lenses_dst / lens_file.name, "lenses", lens_file.name,
                _uninstall_manifest, src=lens_file,
            )
            if _act == "removed":
                lens_count += 1
            elif _act == "preserved":
                _lenses_kept.append(lens_file.name)
        if lens_count > 0:
            console.print(f"[green][OK][/green] Removed {lens_count} lenses")
        elif not _lenses_kept:
            console.print("[yellow][-][/yellow] No jacked lenses found")
        for _n in _lenses_kept:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified lenses/{_n} under "
                "~/.claude/jacked-backups/"
            )
    else:
        console.print("[yellow][-][/yellow] Lenses directory not found")

    # Remove only jacked-installed HTML templates (preserve any user-added files)
    templates_src = pkg_root / "templates"
    templates_dst = home / ".claude" / "jacked-templates"
    if templates_src.exists() and templates_dst.exists():
        tpl_count = 0
        _tpls_kept: list[str] = []
        for tpl_file in templates_src.glob("*.html"):
            _act = _mani.remove_or_preserve_flat(
                templates_dst / tpl_file.name, "templates", tpl_file.name,
                _uninstall_manifest, src=tpl_file,
            )
            if _act == "removed":
                tpl_count += 1
            elif _act == "preserved":
                _tpls_kept.append(tpl_file.name)
        if tpl_count > 0:
            console.print(f"[green][OK][/green] Removed {tpl_count} HTML templates")
        for _n in _tpls_kept:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified templates/{_n} under "
                "~/.claude/jacked-backups/"
            )
        # Drop the dir only if it's now empty so user-added templates survive.
        try:
            templates_dst.rmdir()
        except OSError:
            pass

    # Manifest-aware cleanup: remove any artifact jacked recorded but that the
    # current source no longer ships (covers pruned-then-reinstalled history,
    # which the source-glob loops above would miss), then drop the bookkeeping
    # files so a fresh install starts clean.
    _prior_manifest = _uninstall_manifest
    if _prior_manifest:
        # Treat current source as empty so every recorded artifact counts as
        # "removed" and gets pruned from ~/.claude.
        _empty = {cat.key: {} for cat in _mani.CATEGORIES}
        _d = _mani.diff(_prior_manifest, _empty)
        # Same hash-gate as the skills loop: a modified skill dir is left alone,
        # and a modified flat file is moved into ~/.claude/jacked-backups/.
        _pruned, _kept = _mani.prune_removed(_d, home, _prior_manifest)
        if _pruned:
            console.print(
                f"[green][OK][/green] Removed {len(_pruned)} manifest-tracked artifacts"
            )
        for _item in _kept:
            console.print(
                f"[yellow][!][/yellow] Preserved your modified {_item} under "
                "~/.claude/jacked-backups/ (no longer matches what jacked installed)"
            )
    if _manifest_path.exists():
        _manifest_path.unlink()
    _last_install_path = home / ".claude" / "jacked-last-install.json"
    if _last_install_path.exists():
        _last_install_path.unlink()

    # Remove the Codex artifacts too (best-effort; per the Codex manifest).
    try:
        from jacked.codex import installer as _cdx

        _cdx_out = _cdx.uninstall_codex()
        _cdx_removed = _cdx_out.get("removed", [])
        _cdx_kept = _cdx_out.get("skipped", [])
        if _cdx_removed:
            console.print(
                f"[green][OK][/green] Removed {len(_cdx_removed)} Codex artifacts"
            )
        if _cdx_kept:
            console.print(
                f"[yellow][!][/yellow] Kept {len(_cdx_kept)} Codex skill dir(s) you "
                f"modified: {', '.join(_cdx_kept)}"
            )
    except Exception:
        logger.exception("Codex uninstall pass failed")

    # Remove enabled skill packs' skills (best-effort), then always drop the
    # pack state file so a fresh install starts clean. npx missing: we can't
    # drive the skills CLI to remove anything, so warn and still clear state.
    try:
        from jacked import packs as _packs

        _registry = _packs.load_registry(pkg_root)
        # Remove every pack that was effectively installed (registry defaults the
        # user didn't disable, plus explicit-enabled), not just explicit ones.
        _effective = _packs.effective_enabled_pack_names(home, _registry)
        _deregistered = [
            n for n in _packs.enabled_pack_names(home) if n not in _registry
        ]
        if _effective and _packs.find_npx() is None:
            console.print(
                "[yellow][-][/yellow] Skill packs: Node.js/npx not found; "
                "skipping skill removal and clearing pack state only"
            )
        else:
            for _pk_name in _effective:
                _pk = _registry[_pk_name]
                _pk_res = _packs.remove_pack(_pk, home)
                if _pk_res.removed:
                    console.print(
                        f"[green][OK][/green] Pack '{_pk_name}': removed "
                        f"{len(_pk_res.removed)} skill(s)"
                    )
                if _pk_res.skipped:
                    console.print(
                        f"[yellow][!][/yellow] Pack '{_pk_name}': skipped "
                        f"{_rich_escape(', '.join(_pk_res.skipped))} (different source or not tracked)"
                    )
        for _pk_name in _deregistered:
            # Deregistered pack: we don't know its skills, so we can't remove
            # them. Say so before we drop the state file.
            console.print(
                f"[yellow][!][/yellow] Pack '{_pk_name}' is enabled but unknown "
                "to this jacked version; its skills (if any) were left on disk."
            )
        _state_file = home / ".claude" / _packs.STATE_PATH_NAME
        _state_file.unlink(missing_ok=True)
    except Exception:
        logger.exception("Skill packs uninstall pass failed")

    console.print("\n[bold]Uninstall complete![/bold]")
    console.print(
        "\n[dim]Run 'uv tool uninstall claude-jacked' to fully remove the package.[/dim]"
    )


@main.group(name="packs")
def packs_group():
    """Manage optional third-party skill packs."""
    pass


@packs_group.command(name="list")
def packs_list():
    """List available skill packs and their install status."""
    from rich.table import Table

    from jacked import packs as _packs

    home = _jacked_home()
    registry = _packs.load_registry(_get_data_root())
    if not registry:
        console.print("[yellow]No skill packs are available in this build.[/yellow]")
        return

    explicit = set(_packs.enabled_pack_names(home))
    table = Table(title="Skill packs")
    table.add_column("Pack", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Installed", justify="right", no_wrap=True)
    table.add_column("Source", no_wrap=True)
    table.add_column("Description")

    for name in sorted(registry):
        pack = registry[name]
        st = _packs.pack_status(pack, home)
        state = _packs.pack_state(home, name)  # explicit decision or None
        if _packs.is_effectively_enabled(pack, home):
            status = "[green]on[/green]" + (" [dim](default)[/dim]" if state is None else "")
        else:
            # Off: distinguish a user's explicit disable from an opt-in pack the
            # user simply never turned on.
            status = "[dim]off[/dim]" if state == "disabled" else "[dim]off (opt-in)[/dim]"
        installed = f"{st.get('installed_count', 0)}/{st.get('total', len(pack.skills))}"
        desc = pack.description or ""
        if len(desc) > 60:
            desc = desc[:57] + "..."
        table.add_row(name, status, installed, pack.source, desc)

    console.print(table)

    # Explicitly-enabled-but-deregistered packs never show up in the
    # registry-keyed table above; surface them loudly instead of dropping them.
    for name in sorted(explicit - set(registry)):
        console.print(
            f"[yellow][!][/yellow] Pack '{name}' is enabled but unknown to this "
            "jacked version; its skills were left untouched."
        )


@packs_group.command(name="enable")
@click.argument("name")
def packs_enable(name: str):
    """Enable a skill pack and install its skills."""
    from jacked import packs as _packs

    home = _jacked_home()
    registry = _packs.load_registry(_get_data_root())
    pack = registry.get(name)
    if pack is None:
        _packs_unknown_name(name, registry)

    # Consent/provenance line before we pull instructions the agents will run.
    console.print(_pack_trust_line(pack))

    # Persist intent first so a failed install still leaves the pack enabled
    # (a later `jacked packs update` can then repair it).
    _packs.set_enabled(home, name, True)
    include_codex = _detect_codex_for_packs()
    res = _packs.install_pack(pack, home, include_codex=include_codex)
    if res.ok:
        console.print(f"[green][OK][/green] {_rich_escape(res.message)}")
    else:
        console.print(f"[red][FAIL][/red] {_rich_escape(res.message)}")
        raise SystemExit(1)


@packs_group.command(name="disable")
@click.argument("name")
def packs_disable(name: str):
    """Disable a skill pack and remove its skills."""
    from jacked import packs as _packs

    home = _jacked_home()
    registry = _packs.load_registry(_get_data_root())
    pack = registry.get(name)
    if pack is None:
        # A pack this build no longer knows about can still be stuck enabled in
        # state — let the user turn it off. We can't remove skills we can't
        # enumerate, so just clear the state entry and say so.
        if name in set(_packs.enabled_pack_names(home)):
            console.print(
                f"[yellow][!][/yellow] Pack '{name}' is unknown to this jacked "
                "version but was enabled; disabling it. Any skills it installed "
                "were left on disk."
            )
            _packs.set_enabled(home, name, False)
            return
        _packs_unknown_name(name, registry)

    # Clear intent FIRST (durable), then remove skills. If a crash lands between
    # the two, the pack stays disabled rather than resurrecting as enabled with
    # its skills half-removed.
    _packs.set_enabled(home, name, False)
    res = _packs.remove_pack(pack, home)
    if res.ok:
        console.print(f"[green][OK][/green] {_rich_escape(res.message)}")
    else:
        console.print(
            f"[red][FAIL][/red] {_rich_escape(res.message)} The pack is disabled; some skills "
            "may remain on disk. Enable and disable again to retry removal."
        )
        raise SystemExit(1)


@packs_group.command(name="update")
@click.argument("name", required=False)
def packs_update(name: str | None):
    """Refresh the skills of enabled packs from their upstream repos.

    Scoped to packs that are effectively enabled (default-on packs you didn't
    disable, plus any you explicitly enabled) -- all of them, or a single NAME.
    Disabled packs are left alone; enable a pack first to install its skills.
    """
    from jacked import packs as _packs

    home = _jacked_home()
    registry = _packs.load_registry(_get_data_root())
    effective = _packs.effective_enabled_pack_names(home, registry)
    include_codex = _detect_codex_for_packs()

    if name:
        pack = registry.get(name)
        if pack is None:
            _packs_unknown_name(name, registry)
        if name not in effective:
            console.print(
                f"[yellow]Pack '{name}' is not enabled. "
                f"Run `jacked packs enable {name}` first.[/yellow]"
            )
            raise SystemExit(1)
        targets = [pack]
    else:
        # Explicit-enabled-but-deregistered packs can't be updated (unknown to
        # this build); warn rather than silently skip.
        for unknown_name in [
            n for n in _packs.enabled_pack_names(home) if n not in registry
        ]:
            console.print(
                f"[yellow][!][/yellow] Pack '{unknown_name}' is enabled but "
                "unknown to this jacked version; its skills were left untouched."
            )
        targets = [registry[n] for n in effective]
        if not targets:
            console.print("No skill packs are enabled. Nothing to update.")
            return

    res = _packs.update_packs(targets, home, include_codex=include_codex)
    if res.ok:
        console.print(f"[green][OK][/green] {_rich_escape(res.message)}")
    else:
        console.print(f"[red][FAIL][/red] {_rich_escape(res.message)}")
        raise SystemExit(1)


@main.group(name="statusline")
def statusline_group():
    """Claude Code statusline: model, effort, context, rate limits, account."""


@statusline_group.command(name="enable")
def statusline_enable():
    """Enable the statusline. Takes over a foreign statusline with a backup."""
    from jacked import statusline_setup
    from jacked.memory.settings_io import SettingsUnreadableError

    home = _jacked_home()
    try:
        result = statusline_setup.enable(home)
    except SettingsUnreadableError as exc:
        console.print(f"[red][FAIL][/red] {exc}")
        raise SystemExit(1)
    if result["took_over_foreign"]:
        console.print(
            "[green][OK][/green] Statusline enabled "
            "(your previous statusline was saved; `jacked statusline disable` restores it)"
        )
    elif result["changed"]:
        console.print("[green][OK][/green] Statusline enabled")
    else:
        console.print("[dim][-][/dim] Statusline already enabled")


@statusline_group.command(name="disable")
def statusline_disable():
    """Disable the statusline and restore a saved previous one, if any."""
    from jacked import statusline_setup
    from jacked.memory.settings_io import SettingsUnreadableError

    home = _jacked_home()
    try:
        result = statusline_setup.disable(home)
    except SettingsUnreadableError as exc:
        console.print(f"[red][FAIL][/red] {exc}")
        raise SystemExit(1)
    if result["restored_previous"]:
        console.print("[green][OK][/green] Statusline disabled; previous statusline restored")
    elif result["changed"]:
        console.print("[green][OK][/green] Statusline disabled")
    else:
        console.print("[dim][-][/dim] Statusline was not registered")


@statusline_group.command(name="status")
def statusline_status():
    """Show the statusline registration state."""
    import json as _json

    from jacked import statusline_setup

    home = _jacked_home()
    settings_file = home / ".claude" / "settings.json"
    try:
        settings = _json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    where = statusline_setup.entry_state(settings)
    state = statusline_setup.load_state(home)["state"] or "default (on)"
    console.print(f"Registration: {where}")
    console.print(f"Preference:   {state}")
    if where == "ours":
        console.print(f"Command:      {settings['statusLine'].get('command', '')}")


@main.group(name="dcr")
def dcr_group():
    """Configure how /dcr runs its review agents."""


def _dcr_print_status(resolved: dict) -> None:
    """Print the friendly engine block shared by `dcr engine` and `dcr engine set`.

    Takes the resolved contract (jacked.dcr_settings.resolve) so the human block
    and the --json output can never describe different states.
    """
    if resolved.get("engine") == "codex":
        console.print("DCR review engine: [cyan]Codex (OpenAI)[/cyan]")
        console.print(f"  Model:  {_rich_escape(str(resolved.get('model') or ''))}")
        console.print(f"  Effort: {_rich_escape(str(resolved.get('effort') or ''))}")
        kept = resolved.get("keep_on_claude") or []
        kept_text = ", ".join(_rich_escape(str(k)) for k in kept) if kept else "(none)"
        console.print(f"  Kept on Claude: {kept_text}")
        if resolved.get("usable"):
            version = resolved.get("codex_version")
            cli_label = f"codex CLI {version}" if version else "codex CLI"
            console.print(f"  Status: [green]ready[/green]  ({_rich_escape(cli_label)} installed, signed in)")
            # Usable with a reason means something was adjusted on the way out
            # (a retired default migrated, a model the CLI cannot serve swapped
            # for the fallback). Hiding it would make the printed Model line
            # look chosen when it was substituted.
            if resolved.get("reason"):
                console.print(f"[yellow][!][/yellow] {_rich_escape(str(resolved['reason']))}")
        else:
            reason = _rich_escape(str(resolved.get("reason") or "the Codex CLI is not usable"))
            console.print(f"  Status: [yellow]not usable - {reason}[/yellow]")
            console.print(
                "[yellow]Reviews will fall back to Claude until this is fixed.[/yellow]"
            )
        return

    console.print("DCR review engine: [cyan]Claude (default)[/cyan]")
    # A corrupt config resolves to Claude with a reason attached — surface it, or
    # the user never learns why their codex setting stopped taking effect.
    if resolved.get("reason"):
        console.print(f"[yellow][!][/yellow] {_rich_escape(str(resolved['reason']))}")
    console.print(
        "Switch with: jacked dcr engine set codex --model gpt-6-astra --effort xhigh"
    )


@dcr_group.group(name="engine", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True,
              help="Print the resolved engine config as JSON (machine readable).")
@click.pass_context
def dcr_engine_group(ctx, as_json: bool):
    """Show which engine /dcr reviewers run on."""
    if ctx.invoked_subcommand is not None:
        return

    import json as _json

    from jacked import dcr_settings as _dcr

    resolved = _dcr.resolve(_jacked_home())
    if as_json:
        # The /dcr command parses this: JSON on stdout and nothing else.
        click.echo(_json.dumps(resolved))
        return
    _dcr_print_status(resolved)


@dcr_engine_group.command(name="set")
@click.argument("engine", type=click.Choice(_DCR_ENGINE_CHOICES))
@click.option("--model", default=None,
              help="Model the codex reviewers run (for example gpt-6-astra, or gpt-5.6-sol on an older Codex CLI).")
@click.option("--effort", type=click.Choice(_DCR_EFFORT_CHOICES), default=None,
              help="Reasoning effort the codex reviewers run at.")
@click.option("--keep-on-claude", default=None,
              help="Comma-separated lens names that always stay on Claude.")
def dcr_engine_set(engine: str, model, effort, keep_on_claude):
    """Set the review engine (claude or codex) and its model, effort, and lenses."""
    from jacked import dcr_settings as _dcr

    home = _jacked_home()
    lenses = None
    if keep_on_claude is not None:
        lenses = [part.strip() for part in keep_on_claude.split(",") if part.strip()]

    try:
        # One shared read-merge-write (the dashboard calls the same function), so
        # the two surfaces cannot drift on merge semantics.
        config = _dcr.update_config(
            home, engine=engine, model=model, effort=effort, keep_on_claude=lenses,
        )
    except _dcr.DcrSettingsUnreadableError as exc:
        # NEVER overwrite a config we could not parse: the user's real settings
        # would be gone. Say where the file is and stop. (Also catches the
        # post-write verification failure, which must not surface as a traceback.)
        console.print(f"[red][FAIL][/red] {_rich_escape(str(exc))}")
        console.print(
            f"Nothing was written. Fix or delete {_rich_escape(str(_dcr.config_path(home)))}, "
            "then run this command again."
        )
        raise SystemExit(1)
    except _dcr.DcrSettingsAccessError as exc:
        # The filesystem refused the write (permissions, read-only, full disk).
        console.print(f"[red][FAIL][/red] {_rich_escape(str(exc))}")
        raise SystemExit(1)
    except ValueError as exc:
        console.print(f"[red][FAIL][/red] {_rich_escape(str(exc))}")
        raise SystemExit(1)

    # Saving codex while the preflight fails is allowed: the /dcr runtime falls
    # back to Claude, so the setting is kept and the warning is printed.
    _dcr_print_status(_dcr.resolve(home))

    # Emptying the carve-out list is allowed (an explicit user override), but it
    # is the one setting that quietly moves the Security lens off Claude, so say
    # so out loud instead of letting the user discover it in a review.
    if config.get("engine") == "codex" and not config.get("keep_on_claude"):
        console.print(
            "[bold yellow]Warning: the keep-on-Claude list is empty. "
            "EVERY lens, including Security, will run on Codex.[/bold yellow]"
        )


@dcr_engine_group.command(name="clear")
def dcr_engine_clear():
    """Delete the DCR engine config so reviews use Claude."""
    from jacked import dcr_settings as _dcr

    try:
        existed = _dcr.clear_config(_jacked_home())
    except _dcr.DcrSettingsAccessError as exc:
        # Never claim the config was cleared when the filesystem refused.
        console.print(f"[red][FAIL][/red] {_rich_escape(str(exc))}")
        raise SystemExit(1)
    console.print("DCR engine config cleared. Reviews use Claude (default).")
    if not existed:
        console.print("[dim](No config file was present.)[/dim]")


@main.group(name="memory")
def memory_group():
    """Cross-repo memory vault: capture and recall durable facts across repos."""
    # Attach the durable rotating failure log so any memory-command warning lands
    # in <home>/.claude/jacked-memory.log (covers capture-merge run from the hook).
    try:
        from jacked.memory import vault as _vault

        _vault.ensure_memory_file_logging()
    except Exception:  # noqa: BLE001 -- logging setup must never break a command
        logger.debug("memory: file logging setup failed", exc_info=True)


def _memory_csv(value: str | None) -> list[str]:
    """Split a comma-separated option into a clean list (drops empties)."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _memory_default_roots() -> list:
    """Default init roots: <jacked_home>/Github if it exists, else the cwd parent."""
    from pathlib import Path as _Path

    gh = _jacked_home() / "Github"
    if gh.is_dir():
        return [gh]
    return [_Path.cwd().parent]


@memory_group.command(name="init")
@click.option("--root", "roots", multiple=True, type=click.Path(),
              help="Root dir to scan for repos (repeatable). Defaults to ~/Github or the cwd parent.")
@click.option("--yes", is_flag=True, help="Accept all suggested groups without prompting.")
def memory_init(roots: tuple, yes: bool):
    """Initialize the memory vault: scan repos, suggest groups, write the vault."""
    from collections import OrderedDict
    from pathlib import Path as _Path

    from jacked import memory as _memory

    home = _jacked_home()
    vault = _memory.vault_dir()

    # Re-running init on an existing vault is safe: report, never clobber. Still
    # (re)enable so the capture/recall hooks + per-repo git hooks are present and
    # surfaced -- enable() is idempotent and never touches vault content.
    if _memory.is_initialized(vault):
        st = _memory.status(vault, home)
        console.print(
            f"[yellow]Vault already initialized[/yellow] at {_rich_escape(st['vault_dir'])}. "
            "Nothing changed."
        )
        _memory_print_groups(st)
        _memory_finish_enable(home)
        return

    root_paths = [_Path(r) for r in roots] if roots else _memory_default_roots()
    console.print(
        "Scanning for repos under: "
        + ", ".join(_rich_escape(str(p)) for p in root_paths)
    )
    discovered = _memory.scan_roots(root_paths)
    if not discovered:
        console.print(
            "[yellow]No git repos found under those roots.[/yellow] "
            "Creating an empty vault; groups are created on the fly as you add notes."
        )
    suggestions = _memory.suggest_groups(discovered)

    interactive = sys.stdin.isatty() and not yes
    final: "OrderedDict[str, list]" = OrderedDict()
    for name, idents in suggestions.items():
        if interactive:
            console.print(
                f"\nSuggested group [cyan]{_rich_escape(name)}[/cyan]: "
                + ", ".join(_rich_escape(i) for i in idents)
            )
            chosen = (click.prompt("  Group name", default=name) or name).strip()
            chosen = _memory.slugify(chosen) if chosen else name
        else:
            chosen = name
        bucket = final.setdefault(chosen, [])
        for ident in idents:
            if ident not in bucket:
                bucket.append(ident)

    summary = _memory.init_vault(vault, [str(p) for p in root_paths], final)
    _memory.set_enabled(home, True, vault=vault)

    console.print(
        f"\n[green][OK][/green] Vault initialized at {_rich_escape(summary['vault_dir'])} "
        f"({len(summary['groups'])} group(s))."
    )
    for group, repos in summary["groups"].items():
        console.print(f"  [cyan]{_rich_escape(group)}[/cyan]: {len(repos)} repo(s)")

    # Turn the feature ON end-to-end: install the capture + recall settings.json
    # hooks and the per-repo post-merge git hooks (this is what makes the README's
    # "init installs hooks" claim true).
    _memory_finish_enable(home)


def _memory_finish_enable(home) -> None:
    """Enable the memory vault end-to-end via the shared setup engine and report.

    Installs the capture + recall settings.json entries and the post-merge git
    hooks into every mapped repo (idempotent; never touches vault content), then
    prints what happened -- hooks installed, per-repo git-hook results with any
    skips surfaced, and a migration nudge when legacy ``.remember`` dirs remain.
    A corrupt settings.json aborts LOUDLY (exit 2) rather than clobbering the
    user's hooks with a fresh file.
    """
    from jacked.memory import setup as _setup
    from jacked.memory.settings_io import SettingsUnreadableError

    try:
        result = _setup.enable(home)
    except SettingsUnreadableError:
        console.print(
            "[red][FAIL][/red] settings.json is unreadable; vault created but hooks "
            "NOT installed. Fix the JSON and re-run `jacked memory init`."
        )
        raise SystemExit(2)

    hooks = result.get("hooks_installed") or []
    if hooks:
        console.print(
            f"[green][OK][/green] Capture + recall hooks installed "
            f"({', '.join(_rich_escape(h) for h in hooks)})."
        )

    git_hooks = result.get("git_hooks") or {}
    if git_hooks:
        # "current" = already installed and up to date (healthy idempotent
        # re-run); only genuine refusals print as warnings.
        ok = [
            ident for ident, res in git_hooks.items()
            if res.get("installed") or res.get("current")
        ]
        if ok:
            console.print(f"  post-merge git hook active in {len(ok)} repo(s).")
        for ident, res in git_hooks.items():
            if res.get("skipped"):
                console.print(
                    f"  [yellow]post-merge git hook skipped for {_rich_escape(ident)}: "
                    f"{_rich_escape(str(res.get('reason', '')))}[/yellow]"
                )

    migration = int(result.get("migration_available") or 0)
    if migration:
        console.print(
            f"  migration: {migration} legacy .remember dir(s) found "
            "(run `jacked memory migrate` to import)."
        )


def _memory_print_groups(st: dict) -> None:
    if not st.get("groups"):
        return
    for group, info in st["groups"].items():
        total = sum(info.get("counts", {}).values())
        console.print(
            f"  [cyan]{_rich_escape(group)}[/cyan]: "
            f"{len(info.get('repos', []))} repo(s), {total} note(s)"
        )


@memory_group.command(name="status")
@click.option("--quiet", is_flag=True,
              help="No output. Exit 0 if the vault is initialized and enabled, else 1.")
def memory_status(quiet: bool):
    """Show vault path, groups, note counts, drift, and sync state."""
    from jacked import memory as _memory

    home = _jacked_home()
    vault = _memory.vault_dir()
    st = _memory.status(vault, home)

    if quiet:
        raise SystemExit(0 if (st["initialized"] and st["enabled"]) else 1)

    from rich.table import Table

    state_word = "enabled" if st["enabled"] else "disabled"
    init_word = "initialized" if st["initialized"] else "not initialized"
    console.print(
        f"[bold]Memory vault[/bold] ({_rich_escape(init_word)}, {state_word})"
    )
    console.print(f"  path: {_rich_escape(st['vault_dir'])}")
    console.print(f"  triage model: {_rich_escape(str(st['triage_model']))}")
    console.print(
        f"  drift: {st['drift_added']}/{st['drift_threshold']} added since last groom"
    )
    console.print(f"  pending retries: {st['retry_pending']}")
    console.print(f"  last rollup: {_rich_escape(str(st['last_rollup']))}")
    console.print(f"  last recall: {_rich_escape(str(st.get('last_recall')))}")
    console.print(f"  last capture: {_rich_escape(str(st.get('last_capture')))}")
    if st.get("capture_failures"):
        console.print(f"  capture failures: {st['capture_failures']}")
    if st.get("last_capture_error"):
        console.print(
            f"  [yellow]last capture error: {_rich_escape(str(st['last_capture_error']))}[/yellow]"
        )
    console.print(f"  last sync: {_rich_escape(str(st['last_sync']))}")
    if st.get("last_sync_error"):
        console.print(
            f"  [yellow]last sync error: {_rich_escape(str(st['last_sync_error']))}[/yellow]"
        )

    # Honesty cross-check: state says enabled but the capture hook is not actually
    # installed in settings.json (a half-applied enable, or a settings edit that
    # dropped it). Surface it so a user isn't fooled into thinking capture runs.
    if st["enabled"]:
        try:
            from jacked.memory import hooks_config, settings_io

            settings = settings_io.read_settings(settings_io.settings_path(home))
            if not hooks_config.has_capture_entry(settings):
                console.print(
                    "  [yellow]enabled in state but the capture hook is not installed in "
                    "settings.json (run jacked install or re-enable from the dashboard)[/yellow]"
                )
            elif not hooks_config.has_recall_entry(settings):
                console.print(
                    "  [yellow]capture hook installed but the recall hook is missing; "
                    "the SessionStart brief will not inject (re-enable from the "
                    "dashboard or run jacked memory init)[/yellow]"
                )
        except settings_io.SettingsUnreadableError:
            console.print(
                "  [yellow]settings.json is unreadable; cannot confirm the capture hook is "
                "installed[/yellow]"
            )

    # Cheap scan for legacy .remember dirs that can still be imported.
    if st["initialized"]:
        try:
            from pathlib import Path as _Path

            from jacked.memory import migrate as _migrate

            cfg = _memory.load_vault_config(vault)
            root_paths = [_Path(r) for r in cfg.get("roots", [])]
            remember_dirs = _migrate.discover_remember_dirs(root_paths)
            if remember_dirs:
                console.print(
                    f"  migration: {len(remember_dirs)} legacy .remember dir(s) "
                    "found (run `jacked memory migrate` to import)"
                )
        except Exception:  # noqa: BLE001 -- a status readout must never crash
            logger.debug("memory status: .remember discovery failed", exc_info=True)

    if not st["initialized"]:
        console.print(
            "\n[yellow]Run `jacked memory init` to create the vault.[/yellow]"
        )
        return

    if not st["groups"]:
        console.print("\n[dim]No groups yet.[/dim]")
        return

    table = Table(title="Groups")
    table.add_column("Group", style="cyan", no_wrap=True)
    table.add_column("Repos", justify="right", no_wrap=True)
    for t in _memory.VALID_TYPES:
        table.add_column(t, justify="right", no_wrap=True)
    for group in sorted(st["groups"]):
        info = st["groups"][group]
        counts = info.get("counts", {})
        table.add_row(
            group,
            str(len(info.get("repos", []))),
            *[str(counts.get(t, 0)) for t in _memory.VALID_TYPES],
        )
    console.print(table)


@memory_group.command(name="add")
@click.option("--type", "note_type", required=True,
              type=click.Choice(["decision", "convention", "vision", "reference", "progress"]),
              help="Note type.")
@click.option("--title", required=True, help="Short note title (slugified for the filename).")
@click.option("--group", default=None, help="Target group (default: auto from the current repo).")
@click.option("--repos", default=None, help="Comma-separated repo identities (default: current repo).")
@click.option("--tags", default=None, help="Comma-separated tags (optional).")
@click.option("--body", required=True, help="Note body. Use '-' to read from stdin.")
def memory_add(note_type: str, title: str, group: str | None, repos: str | None,
               tags: str | None, body: str):
    """Add a typed atomic note, update the index, and commit the vault."""
    from jacked import memory as _memory

    home = _jacked_home()
    vault = _memory.vault_dir()
    if not _memory.is_initialized(vault):
        console.print(
            "[red][FAIL][/red] Vault is not initialized. Run `jacked memory init` first."
        )
        raise SystemExit(2)

    if body == "-":
        body = sys.stdin.read()
    if not (body or "").strip():
        console.print("[red][FAIL][/red] Note body is empty.")
        raise SystemExit(2)

    # Resolve the current repo identity once (used for auto group + auto repos).
    from pathlib import Path as _Path

    identity = _memory.repo_identity(_Path.cwd())

    if group:
        group = _memory.slugify(group)
        _memory.ensure_group(vault, group)
    else:
        group = _memory.resolve_group(vault, identity)
        if not group:
            # Unmapped repo -> a solo group named after it, created on the fly.
            # The vault.json read-modify-write joins the vault-write lock contract
            # so a concurrent capture/merge that also registers a solo group can't
            # lose this mapping (or vice versa).
            group = _memory.group_for_identity(identity)
            with _memory.vault_write_lock(vault):
                cfg = _memory.load_vault_config(vault)
                _memory.register_repo(cfg, identity, group)
                _memory.save_vault_config(vault, cfg)
            _memory.ensure_group(vault, group)

    repo_list = _memory_csv(repos) or [identity]
    tag_list = _memory_csv(tags)

    try:
        res = _memory.add_note(
            vault, home,
            note_type=note_type, title=title, group=group,
            repos=repo_list, tags=tag_list, body=body,
        )
    except (ValueError, RuntimeError) as exc:
        # ValueError = schema violation; RuntimeError = vault git op failure.
        # Both are user-fixable conditions, not tracebacks.
        console.print(f"[red][FAIL][/red] {_rich_escape(str(exc))}")
        raise SystemExit(2)
    except subprocess.SubprocessError as exc:
        # A vault git op that timed out or was killed (TimeoutExpired is a
        # SubprocessError) surfaces as a clean failure, never a raw traceback.
        console.print(
            f"[red][FAIL][/red] vault git operation timed out or failed: {_rich_escape(str(exc))}"
        )
        raise SystemExit(2)

    console.print(
        f"[green][OK][/green] Added {note_type} note to group "
        f"'{_rich_escape(res['group'])}': {_rich_escape(res['path'])}"
    )


@memory_group.command(name="search")
@click.argument("query")
@click.option("--group", default=None, help="Limit search to one group.")
@click.option("--type", "note_type", default=None,
              type=click.Choice(["decision", "convention", "vision", "reference", "progress"]),
              help="Limit the note-body tier to one type.")
@click.option("--limit", default=20, type=int, help="Max results (default 20).")
def memory_search(query: str, group: str | None, note_type: str | None, limit: int):
    """Search the vault (hot -> index -> note bodies -> episodic)."""
    from jacked import memory as _memory

    vault = _memory.vault_dir()
    if not _memory.is_initialized(vault):
        console.print("[yellow]Vault is not initialized.[/yellow] Run `jacked memory init`.")
        return

    try:
        results = _memory.search(
            vault, query, group=group, note_type=note_type, limit=limit
        )
    except subprocess.SubprocessError as exc:
        # Search is file-only today, but the CLI presents the SAME clean failure
        # as `add` for any vault git op that times out or is killed, so the vault
        # failure contract reads uniformly across the memory commands.
        console.print(
            f"[red][FAIL][/red] vault git operation timed out or failed: {_rich_escape(str(exc))}"
        )
        raise SystemExit(2)
    if not results:
        console.print(f"[dim]No matches for {_rich_escape(query)}.[/dim]")
        return

    for rel, lineno, line in results:
        console.print(
            f"[cyan]{_rich_escape(rel)}[/cyan]:{lineno}: {_rich_escape(line)}"
        )
    console.print(f"[dim]{len(results)} match(es).[/dim]")


@memory_group.command(name="capture-merge", hidden=True)
@click.option("--repo", default=".", type=click.Path(),
              help="Repo whose just-landed merge to distill (default: cwd).")
def memory_capture_merge(repo: str):
    """Distill a just-landed merge into a candidate note (git post-merge hook).

    Invoked (backgrounded) by the installed post-merge hook. Fail-open: it always
    exits 0 so a git merge is never blocked by a memory-capture failure.
    """
    try:
        from jacked.memory import merge_capture as _merge_capture

        _merge_capture.capture_merge(repo)
    except Exception:  # noqa: BLE001 -- a git hook must never propagate failure
        logger.debug("memory capture-merge failed", exc_info=True)
    raise SystemExit(0)


@memory_group.command(name="rollup")
def memory_rollup():
    """Roll episodic history forward: past-day files to recent, aged recent to archive."""
    from jacked import memory as _memory
    from jacked.memory import rollup as _rollup

    home = _jacked_home()
    vault = _memory.vault_dir()
    if not _memory.is_initialized(vault):
        # Exit 0 even when uninitialized: rollup is a maintenance no-op, not a
        # failure the caller (or the SessionEnd tail-call) should trip on.
        console.print(
            "[yellow]Vault is not initialized.[/yellow] Run `jacked memory init` first."
        )
        return

    summary = _rollup.rollup(vault, home)
    console.print(
        f"[green][OK][/green] Rollup complete: "
        f"{summary['days_rolled']} day(s) rolled to recent, "
        f"{summary['sections_archived']} section(s) archived, "
        f"{summary['skipped_unparseable']} skipped (unparseable)."
    )
    if not summary["changed"]:
        console.print("[dim]Nothing new to roll up; vault unchanged.[/dim]")


@memory_group.command(name="migrate")
@click.option("--root", "roots", multiple=True, type=click.Path(),
              help="Root dir to scan for .remember dirs (repeatable). Defaults to the vault roots.")
@click.option("--yes", is_flag=True, help="Skip the import confirmation prompt.")
@click.option("--keep-plugin", is_flag=True,
              help="Never offer to retire the remember plugin after migrating.")
def memory_migrate(roots: tuple, yes: bool, keep_plugin: bool):
    """Import legacy .remember history into the vault, verifying counts.

    Sources are never modified or deleted. Every imported file's entry count is
    verified against the source; any mismatch fails that repo and exits nonzero.
    """
    from pathlib import Path as _Path

    from rich.table import Table

    from jacked import memory as _memory
    from jacked.memory import migrate as _migrate

    home = _jacked_home()
    vault = _memory.vault_dir()
    if not _memory.is_initialized(vault):
        console.print(
            "[red][FAIL][/red] Vault is not initialized. Run `jacked memory init` first."
        )
        raise SystemExit(2)

    if roots:
        root_paths = [_Path(r) for r in roots]
    else:
        cfg = _memory.load_vault_config(vault)
        root_paths = [_Path(r) for r in cfg.get("roots", [])]

    rows = _migrate.preview(vault, roots=root_paths)
    if not rows:
        console.print(
            "[yellow]No .remember directories found to migrate.[/yellow] "
            "Nothing changed."
        )
        return

    plan_table = Table(title="Will import")
    plan_table.add_column("Repo", style="cyan")
    plan_table.add_column("Group", style="cyan", no_wrap=True)
    plan_table.add_column("Files", justify="right", no_wrap=True)
    plan_table.add_column("Entries", justify="right", no_wrap=True)
    for row in rows:
        plan_table.add_row(
            _rich_escape(row["repo"]), _rich_escape(row["group"]),
            str(row["files"]), str(row["entries"]),
        )
    console.print(plan_table)
    console.print(
        "[dim]Sources are read-only: nothing in .remember is modified or deleted.[/dim]"
    )

    interactive = sys.stdin.isatty() and not yes
    if interactive and not click.confirm("Import these into the vault?", default=True):
        console.print("Aborted. Nothing changed.")
        return

    report = _migrate.migrate(vault, home, roots=root_paths)

    verify_table = Table(title="Verification (source vs staged entries)")
    verify_table.add_column("Repo", style="cyan")
    verify_table.add_column("File")
    verify_table.add_column("Source", justify="right", no_wrap=True)
    verify_table.add_column("Staged", justify="right", no_wrap=True)
    verify_table.add_column("Result", no_wrap=True)
    for repo_path, rep in report["repos"].items():
        short = rep.get("repo_short", repo_path)
        if not rep["files"]:
            verify_table.add_row(
                _rich_escape(short), "[dim](no files)[/dim]", "-", "-",
                _memory_migrate_status_cell(rep["status"]),
            )
            continue
        for name, counts in rep["files"].items():
            src = counts.get("source_entries")
            staged = counts.get("staged_entries")
            ok = staged is not None and src == staged
            result = "[green]ok[/green]" if ok else "[red]MISMATCH[/red]"
            verify_table.add_row(
                _rich_escape(short), _rich_escape(name),
                str(src if src is not None else "-"),
                str(staged if staged is not None else "-"),
                result,
            )
    console.print(verify_table)
    console.print(
        f"[bold]{report['repos_migrated']}[/bold] repo(s) migrated, "
        f"[bold]{report['repos_failed']}[/bold] failed, "
        f"[bold]{report['candidates_created']}[/bold] candidate note(s) created "
        f"from core-memories."
    )
    for repo_path, rep in report["repos"].items():
        if rep["status"] == "failed":
            console.print(
                f"[red][FAIL][/red] {_rich_escape(rep.get('repo_short', repo_path))}: "
                f"{_rich_escape(str(rep.get('reason') or 'verification mismatch'))} "
                "(nothing imported for this repo)"
            )
        skipped_links = rep.get("skipped_symlinks") or []
        if skipped_links:
            console.print(
                f"[yellow][SKIP][/yellow] {_rich_escape(rep.get('repo_short', repo_path))}: "
                f"symlinked source file(s) refused: "
                f"{_rich_escape(', '.join(skipped_links))}"
            )
        already = rep.get("already_imported") or []
        if already:
            console.print(
                f"  [dim]{len(already)} file(s) already imported, skipped[/dim]"
            )

    any_failed = report["repos_failed"] > 0
    migrated_any = report["repos_migrated"] > 0

    if migrated_any and not any_failed and not keep_plugin:
        from jacked.memory.settings_io import SettingsUnreadableError

        res_path = _migrate.settings_path(home)
        if sys.stdin.isatty():
            console.print(
                f"\nThe remember plugin '{_migrate.REMEMBER_PLUGIN_ID}' can now be retired. "
                f"This will disable it in {_rich_escape(str(res_path))}; "
                "your .remember sources stay on disk, untouched."
            )
            if click.confirm("Retire the remember plugin now?", default=False):
                try:
                    res = _migrate.retire_remember_plugin(home)
                except SettingsUnreadableError as exc:
                    console.print(
                        f"[red][FAIL][/red] settings.json is unreadable; refusing to modify it "
                        f"({_rich_escape(str(exc))})."
                    )
                    raise SystemExit(2)
                console.print(
                    f"[green][OK][/green] Disabled '{_rich_escape(res['plugin'])}' in "
                    f"{_rich_escape(res['settings_path'])}."
                )
            else:
                console.print("[dim]Left the remember plugin enabled.[/dim]")
        else:
            console.print(
                f"\n[dim]Not retiring the remember plugin (no interactive terminal). "
                f"Rerun `jacked memory migrate` in a terminal to disable "
                f"'{_rich_escape(_migrate.REMEMBER_PLUGIN_ID)}', or leave it; "
                "your sources are safe either way.[/dim]"
            )

    if any_failed:
        raise SystemExit(2)


def _memory_migrate_status_cell(status: str) -> str:
    if status == "migrated":
        return "[green]migrated[/green]"
    if status == "failed":
        return "[red]failed[/red]"
    return f"[dim]{_rich_escape(status)}[/dim]"


@memory_group.command(name="mark-groomed", hidden=True)
def memory_mark_groomed():
    """Record a completed librarian groom (reset drift, stamp last_groomed)."""
    from jacked import memory as _memory

    home = _jacked_home()
    _memory.mark_groomed(home)
    console.print("[green][OK][/green] Marked vault as groomed (drift counter reset).")


@main.group(name="permissions")
def permissions_group():
    """Audit and prune Claude Code Bash permission rules."""
    pass


@main.command(name="menubar")
@click.option("--host", default=None, help="Host to bind for this launch (ignored once Remote access is configured in the dashboard, which is then authoritative; default 127.0.0.1). Pass 0.0.0.0 to expose on all interfaces.")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
def menubar(host: str | None, port: int | None):
    """Start the macOS menu-bar agent in the foreground (manual start).

    macOS only — the live usage pill, dropdown, and pinned side panel. On other
    platforms use `jacked service start` (pystray tray). Equivalent to
    `jacked service start` on macOS, but fails fast off darwin so it's an
    explicit, debuggable entry point.
    """
    if sys.platform != "darwin":
        console.print("[red]`jacked menubar` is macOS-only.[/red] "
                      "Use `jacked service start` on this platform.")
        sys.exit(1)

    from jacked.service import DEFAULT_PORT
    from jacked.service.tray import ServiceRunner

    # Pass the raw host (possibly None): ServiceRunner resolves the bind plan
    # from the DB / loopback default when no explicit --host was given.
    ServiceRunner(host=host, port=port or DEFAULT_PORT).run()


def _persist_remote_access_from_host(host: str, *, allow_one_shot: bool = True) -> bool:
    """Map an explicit ``service install/restart --host`` onto the dashboard
    Remote access setting.

    These commands EXPRESS intent about the persistent mode, so unlike the
    artifact migration (which only fills absent keys) this OVERWRITES any
    existing setting. Returns True when the host was consumed (persisted, and
    the caller should proceed host-free so the DB decides the bind); False when
    it stays a one-shot pass-through (an unmapped specific IP, or the DB write
    failed).
    """
    from jacked.service.migrate import map_host_to_setting

    mapping = map_host_to_setting(host)
    if mapping is None:
        if allow_one_shot:
            console.print(
                f"[yellow]Host {host} is a one-shot override for this start only "
                "and is not persisted. Use the dashboard Remote access setting "
                "(Settings > Advanced) for a persistent mode.[/yellow]"
            )
        else:
            console.print(
                f"[yellow]Host {host} is not a supported persistent mode and was "
                "not applied. The exact supervisor remains host-free. Choose "
                "0.0.0.0, a Tailscale 100.x address, or 127.0.0.1.[/yellow]"
            )
        return False
    enabled, scope = mapping
    try:
        from jacked.web.database import Database

        db = Database()
        db.set_setting("remote_access_enabled", enabled)
        if scope is not None:
            db.set_setting("remote_access_scope", scope)
    except Exception as exc:
        console.print(
            f"[yellow]Could not persist the Remote access setting ({exc}); "
            f"using --host {host} for this start only.[/yellow]"
        )
        return False
    if enabled == "false":
        desc = "disabled (loopback only)"
    elif scope == "all":
        desc = "enabled, all interfaces (0.0.0.0)"
    else:
        desc = f"enabled, Tailscale only ({host})"
    console.print(f"[green][OK][/green] Remote access setting updated: {desc}")
    return True


def _resolve_service_start_host(typed_host: str | None) -> str | None:
    """Boot-time autostart-artifact migration plus argv neutralization.

    Pre-M5 autostart artifacts baked ``--host X`` into the launchd plist /
    Startup VBS. At ``service start`` time we may BE the launchd job that plist
    describes, so: capture the baked host into the settings DB (guarded - never
    clobbers an existing GUI choice), rewrite the artifact host-free FILE-ONLY
    (a bootout here would kill us mid-boot), and decide what the typed
    ``--host`` argv means:

    - typed host EXACTLY equals the artifact's baked host -> this invocation IS
      the artifact's own respawn, so treat it as None (the DB decides; the
      migration just captured the old intent).
    - they differ, or the artifact carries no ``--host`` -> the typed host is a
      deliberate one-shot; honor it verbatim.

    Never raises - a boot must never die over migration bookkeeping.
    """
    try:
        if sys.platform == "darwin":
            from jacked.service.platform import _get_launchd_plist_path

            artifact_path, kind = _get_launchd_plist_path(), "plist"
        elif sys.platform == "win32":
            from jacked.service.platform import _get_windows_startup_path

            artifact_path, kind = _get_windows_startup_path(), "vbs"
        else:
            # Linux: no artifact is generated; the DB read at boot covers it.
            return typed_host
        if not artifact_path.exists():
            return typed_host
        text = artifact_path.read_text(encoding="utf-8")

        from jacked.service.migrate import (
            extract_baked_host,
            migrate_baked_host_to_db,
            remote_access_configured,
            strip_baked_host,
        )

        baked = extract_baked_host(text, kind)
        if baked is None:
            # The on-disk artifact is host-free (this version already stripped
            # it), yet we still received a --host. It did NOT come from the
            # current artifact: it is a STALE launchd in-memory / execv replay of
            # a pre-migration argv (launchd serves its loaded definition, not the
            # rewritten plist, until the next reboot). If remote access is
            # configured in the DB, the DB is authoritative — ignore the stale
            # host, or a crash-respawn could silently re-expose (or hide) the
            # dashboard against the user's saved choice until the next reboot.
            if typed_host is not None and remote_access_configured():
                msg = (
                    f"Ignoring stale --host {typed_host} from a pre-migration "
                    "autostart replay; the bind resolves from the settings DB."
                )
                console.print(f"[dim]{msg}[/dim]")
                # Also log it: this is the one decision that flips network
                # exposure, and on a launchd crash-respawn stdout goes to
                # /dev/null. logger.warning lands in the service log once the
                # process is up, giving a durable audit trail of the ignore.
                logger.warning(msg)
                return None
            return typed_host
        status = migrate_baked_host_to_db(baked)
        try:
            artifact_path.write_text(strip_baked_host(text, kind), encoding="utf-8")
            rewrite = "artifact rewritten host-free (file only, no reload)"
        except OSError as exc:
            rewrite = f"artifact rewrite failed: {exc}"
        if typed_host == baked:
            console.print(
                f"[dim]Autostart artifact carried --host {baked}: {status}; "
                f"{rewrite}. This start is the artifact's own respawn, so the "
                "bind resolves from the settings DB.[/dim]"
            )
            return None
        console.print(
            f"[dim]Autostart artifact carried --host {baked}: {status}; "
            f"{rewrite}.[/dim]"
        )
        return typed_host
    except Exception:
        logger.exception("Boot-time autostart --host migration failed; continuing")
        return typed_host


@main.group()
def service():
    """Manage the jacked background service (tray icon + auto-start)."""
    pass


@service.command(name="start")
@click.option("--host", default=None, help="Host to bind for this launch (ignored once Remote access is configured in the dashboard, which is then authoritative; default 127.0.0.1). Pass 0.0.0.0 to expose on all interfaces.")
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
def service_start(host: str | None, port: int | None):
    """Start jacked as a background service with system tray icon."""
    from jacked.service import DEFAULT_PORT
    from jacked.service.tray import ServiceRunner

    # Boot-time migration: capture a pre-M5 artifact's baked --host into the
    # DB, rewrite the artifact host-free, and neutralize our own argv when this
    # invocation is that artifact's respawn (details in the helper docstring).
    host = _resolve_service_start_host(host)

    # Pass the raw host (possibly None): ServiceRunner resolves the bind plan
    # from the DB / loopback default when no explicit --host was given.
    runner = ServiceRunner(host=host, port=port or DEFAULT_PORT)
    runner.run()


def _provision_with_timeout(provision, paths, timeout_seconds):
    """Run provisioning, bounded when the caller asked for a timeout.

    A batch file cannot bound a command itself, so the Windows upgrade helpers
    ask this command to bound its own work. The worker is an explicit daemon
    thread: ``ThreadPoolExecutor`` workers are joined at interpreter exit, so
    a hung provisioning call would keep the process alive after the refusal.
    """
    if timeout_seconds is None:
        return provision(paths=paths)
    import queue
    import threading

    outcome: "queue.Queue" = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            outcome.put(("ok", provision(paths=paths)))
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            outcome.put(("error", exc))

    threading.Thread(target=_worker, name="jacked-preflight", daemon=True).start()
    try:
        kind, value = outcome.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise TimeoutError(
            f"provisioning did not finish within {timeout_seconds:g}s"
        ) from exc
    if kind == "error":
        raise value
    return value


def _exit_hard(code: int) -> None:
    """Exit even if a daemon thread is wedged inside a C call."""
    import os

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - exiting anyway
            pass
    os._exit(code)


@service.command(name="preflight")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print one JSON object with the result, for automation.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    default=None,
    type=float,
    help="Fail with exit code 1 if provisioning takes longer than this many seconds.",
)
def service_preflight(as_json: bool, timeout_seconds: float | None):
    """Prove this build can provision its service contract.

    The command provisions the contract only. It starts no process and it
    changes no supervisor. Provisioning writes the immutable
    content-addressed launcher file, which is safe to repeat.

    Exit code 0 means this build can boot its service. Exit code 1 means it
    cannot, and an upgrade to this build must not be trusted.
    """
    from jacked.service.lifecycle import (
        default_service_paths,
        provision_service_contract,
    )

    try:
        spec, _environment = _provision_with_timeout(
            provision_service_contract, default_service_paths(), timeout_seconds
        )
    except (OSError, ValueError, TimeoutError) as exc:
        _print_preflight_failure(exc, as_json)
        if isinstance(exc, TimeoutError):
            # The provisioning thread is still wedged; a normal exit would
            # wait for it. The batches depend on this command returning.
            _exit_hard(1)
        sys.exit(1)
    _print_preflight_ok(spec, as_json)


def _print_preflight_failure(exc: BaseException, as_json: bool) -> None:
    import json as _json

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": False,
                    "runtime_path": None,
                    "runtime_target_path": None,
                    "supervisor": None,
                    "generation": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        return
    console.print(f"[red][FAIL][/red] {type(exc).__name__}: {exc}")
    console.print("[dim]Run 'jacked service status' for details.[/dim]")


def _print_preflight_ok(spec, as_json: bool) -> None:
    import json as _json

    supervisor = getattr(spec.supervisor, "value", str(spec.supervisor))
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": True,
                    "runtime_path": spec.runtime_path,
                    "runtime_target_path": spec.runtime_target_path,
                    "supervisor": supervisor,
                    "generation": spec.generation,
                    "error": None,
                }
            )
        )
        return
    console.print("[green][OK][/green] Service contract OK")
    console.print(f"[dim]runtime:[/dim] {spec.runtime_path}")
    console.print(f"[dim]target:[/dim] {spec.runtime_target_path}")
    console.print(f"[dim]supervisor:[/dim] {supervisor}")
    console.print(f"[dim]generation:[/dim] {spec.generation[:12]}")


@service.command(name="stop")
def service_stop():
    """Stop only an instance proven by its private v2 control channel."""
    from jacked.service.lifecycle import default_service_paths

    paths = default_service_paths()
    if not paths.manifest.exists():
        from jacked.service.legacy import resolve_active_legacy_service

        if resolve_active_legacy_service(paths.legacy_pid) is not None:
            console.print(
                "[red]Refusing PID-only stop:[/red] legacy service evidence exists, "
                "but its process identity is unproven. Stop it from its tray menu, "
                "then start the upgraded service."
            )
            sys.exit(1)
        console.print("[yellow]Service is not running[/yellow]")
        return
    from jacked.service.ipc import ControlAction, send_native_control

    try:
        response = send_native_control(paths.manifest, ControlAction.SHUTDOWN)
    except (OSError, ValueError) as exc:
        console.print(
            f"[red]Could not authenticate the service control channel:[/red] "
            f"{type(exc).__name__}. No process was signalled."
        )
        sys.exit(1)
    if not response.get("ok"):
        console.print("[red]Service rejected the authenticated stop request[/red]")
        sys.exit(1)
    console.print("[green][OK][/green] Graceful stop accepted by owned service")


@service.command(name="restart")
@click.option("--host", default=None, help="Sets the dashboard Remote access setting, then restarts: 0.0.0.0 enables all interfaces, a Tailscale 100.x IP enables Tailscale only, 127.0.0.1 disables remote access. Any other IP applies to this start only and is not persisted. The GUI toggle (Settings > Advanced) is the primary interface.")
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
    from jacked.service import DEFAULT_PORT
    from jacked.service.lifecycle import default_service_paths

    _clear_start_failure_breaker(default_service_paths())
    the_port = port or DEFAULT_PORT

    # An explicit --host expresses intent about the persistent Remote access
    # mode: map it into the settings DB (unlike migration, this DOES overwrite
    # existing keys), then restart host-free so the DB decides the bind. This
    # is what makes the command work reliably on macOS: native_restart's
    # kickstart reuses launchd's in-memory argv, but the DB is re-read at every
    # boot. Unmapped specific IPs stay a one-shot pass-through.
    if host is not None and _persist_remote_access_from_host(host):
        host = None

    paths = default_service_paths()
    if not foreground and paths.manifest.exists():
        from jacked.service.lifecycle import (
            handoff_owned_service,
            provision_service_contract,
        )

        try:
            stale = _manifest_is_proven_stale(paths.manifest)
        except (OSError, ValueError):
            console.print(
                "[red]The service manifest is invalid.[/red] Run "
                "`jacked service recover`."
            )
            sys.exit(1)
        if not stale:
            spec, environment = provision_service_contract(paths=paths)
            result = handoff_owned_service(
                spec, environment=environment, paths=paths
            )
            if result.ok:
                console.print(
                    f"[green][OK][/green] Owned service handoff: {result.reason}"
                )
                return
            console.print(f"[red]Owned service handoff failed:[/red] {result.reason}")
            sys.exit(1)
        console.print(
            "[yellow]The prior owned service is stale; starting a replacement.[/yellow]"
        )

    # 2. Start the new service.
    if foreground:
        from jacked.service.tray import ServiceRunner
        # Raw host (possibly None): ServiceRunner resolves from the DB otherwise.
        ServiceRunner(host=host, port=the_port).run()
        return

    # No owned instance to hand off to. When this machine has an owned native
    # supervisor definition, re-arm it so ownership stays with the supervisor:
    # after an upgrade the old job exits cleanly and takes its manifest with
    # it, and a manual spawn here would leave the tray unsupervised until the
    # next login (2026-09-05). A machine with no definition keeps the plain
    # detached spawn.
    if host is None and _rearm_native_owner(the_port):
        return

    # Detached - the tray must survive this command returning. Raw host stays
    # out of argv when None so the child re-resolves the bind from the DB.
    log_path = _spawn_service_detached(host, the_port)
    from jacked import __version__ as current_build

    status = _wait_owned_service_ready(paths, expected_build=current_build)
    if status is None:
        console.print("[red]Replacement service did not become ready.[/red]")
        console.print(f"[dim]Logs: {log_path}[/dim]")
        sys.exit(1)
    running_port = status.get("port") or the_port
    console.print(
        f"[green][OK][/green] Started authenticated service on :{running_port}"
    )


def _rearm_native_owner(port: int) -> bool:
    """Re-activate an owned native supervisor definition, if this machine has one.

    Returns True when the exact service generation is running under the
    native supervisor afterwards. Any refusal falls back to the caller.
    """
    from jacked.service.autostart import AutostartState
    from jacked.service.lifecycle import supervisor_for_platform
    from jacked.service.platform import inspect_autostart
    from jacked.service.spec import SupervisorKind

    if supervisor_for_platform() is SupervisorKind.MANUAL:
        return False
    try:
        state = inspect_autostart().state
    except Exception:  # noqa: BLE001 - inspection failure means "no definition"
        return False
    if state not in {AutostartState.OWNED_ENABLED, AutostartState.OWNED_DISABLED}:
        return False
    console.print("[dim]Re-arming the native supervisor definition[/dim]")
    return _ensure_autostart_and_running(port, label="Service")


@service.command(name="status")
def service_status():
    """Show evidence-qualified service state and discovered endpoint."""
    from jacked.service.lifecycle import default_service_paths, discover_service
    from jacked.service.autostart import AutostartState
    from jacked.service.platform import inspect_autostart

    paths = default_service_paths()
    endpoint = discover_service(paths)
    autostart = inspect_autostart()
    stopped = False
    if autostart.state is AutostartState.OWNED_ENABLED:
        autostart_label = "[green]enabled[/green]"
    elif autostart.state in {AutostartState.ABSENT, AutostartState.OWNED_DISABLED}:
        autostart_label = "[dim]disabled[/dim]"
    else:
        detail = f" ({autostart.reason})" if autostart.reason else ""
        autostart_label = f"[yellow]{autostart.state.value}{detail}[/yellow]"
    if endpoint.source == "manifest":
        from jacked.service.ipc import ControlAction, send_native_control

        try:
            response = send_native_control(paths.manifest, ControlAction.STATUS)
        except (OSError, ValueError) as exc:
            console.print(
                "[bold yellow]Jacked Service: ownership indeterminate[/bold yellow]"
            )
            console.print(
                f"  Evidence:  manifest + failed control ({type(exc).__name__})"
            )
            console.print(f"  Autostart: {autostart_label}")
            _print_start_failure_breaker(paths)
            return
        if response.get("ok"):
            state = response["result"]
            reported = state.get("state", "unknown")
            label = (
                "quarantined"
                if reported == "running" and state.get("quarantine")
                else reported
            )
            color = "green" if reported == "running" else "yellow"
            console.print(f"[bold {color}]Jacked Service: {label}[/bold {color}]")
            console.print(f"  Build:     {state.get('build_version')}")
            console.print(f"  Protocol:  {state.get('protocol_version')}")
            console.print(f"  Generation:{state.get('generation')}")
            console.print(f"  Port:      {state.get('port')}")
            console.print(f"  Autostart: {autostart_label}")
            if reported == "degraded":
                console.print(f"  Failure:   {state.get('failure') or 'unknown'}")
                console.print(
                    "  Recovery:  run `jacked service recover`; inspect "
                    "~/.claude/jacked-tray.log"
                )
            if reported == "running":
                console.print(f"  Dashboard: http://127.0.0.1:{state.get('port')}")
            _print_start_failure_breaker(paths)
            return
    if endpoint.source == "legacy":
        console.print("[bold yellow]Jacked Service: legacy service running[/bold yellow]")
        console.print(
            f"  Evidence:  health fingerprint on 127.0.0.1:{endpoint.port}; "
            "control unavailable"
        )
    elif endpoint.source in {"manifest-invalid", "legacy-ambiguous"}:
        console.print(
            "[bold yellow]Jacked Service: ownership indeterminate[/bold yellow]"
        )
        console.print(f"  Evidence:  {endpoint.source} ({endpoint.reason})")
    else:
        console.print("[bold yellow]Jacked Service: stopped[/bold yellow]")
        stopped = True
    console.print(f"  Autostart: {autostart_label}")
    # A legacy autostart entry cannot start the owned service, so a stopped
    # service stays stopped until the operator re-installs ownership.
    if stopped and autostart.state is AutostartState.LEGACY:
        console.print("  Recovery:  run `jacked service recover`")
    _print_start_failure_breaker(paths)


@service.command(name="install")
@click.option(
    "--host",
    default=None,
    help="Sets the dashboard Remote access setting: 0.0.0.0 enables all interfaces, a Tailscale 100.x IP enables Tailscale only, 127.0.0.1 disables remote access. Other IPs are not applied to the host-free supervisor. The GUI toggle (Settings > Advanced) is the primary interface.",
)
@click.option("--port", default=None, type=int, help="Port to bind to (default: 8321)")
def service_install(host: str | None, port: int | None):
    """Configure jacked to start automatically on login, and start it now."""
    from jacked.service import DEFAULT_PORT

    # An explicit --host expresses intent about the persistent Remote access
    # mode: map it into the settings DB (overwriting existing keys), then
    # install host-free so every boot resolves the bind from the DB. An
    # unmapped IP cannot safely become an extra process beside the supervisor.
    if host is not None:
        _persist_remote_access_from_host(host, allow_one_shot=False)
    _ensure_autostart_and_running(port or DEFAULT_PORT, label="Service")


@service.command(name="recover")
def service_recover():
    """Safely reconcile owned service state without PID/port-based control."""
    from jacked.service.lifecycle import (
        default_service_paths,
        discover_service,
        install_native_owned,
        native_artifact_path,
        provision_service_contract,
        quarantine_invalid_ownership,
    )
    from jacked.service.instance import ServiceLeaseBusy
    from jacked.service.spec import SupervisorKind

    paths = default_service_paths()
    # A live instance holds the singleton lease, so quarantine would fail with
    # ServiceLeaseBusy before recovery could do anything. Resolve the live
    # owner first: hand off a manual one, or report a healthy supervised one.
    live = _live_manifest(paths)
    if live is not None and live.supervisor == SupervisorKind.MANUAL.value:
        console.print(
            "[yellow]A manually started service holds ownership.[/yellow] "
            "Recovery will hand it off to the native supervisor."
        )
        released, detail = _release_manual_owner(paths)
        if not released:
            console.print(f"[red]Recovery could not hand off ownership:[/red] {detail}")
            raise SystemExit(1)
        console.print(f"[green][OK][/green] {detail.capitalize()}")
    elif live is not None:
        if _already_owned_and_running(live, paths):
            console.print(
                "[green][OK][/green] Service already owned and running "
                f"(generation {live.generation[:12]})"
            )
            return
    try:
        quarantined = quarantine_invalid_ownership(paths)
        spec, environment = provision_service_contract(paths=paths)
        artifact_path = native_artifact_path(spec, paths=paths)
    except ServiceLeaseBusy as exc:
        console.print(
            "[red]Recovery could not take ownership:[/red] a running jacked "
            "service holds the service lease. Run `jacked service stop`, then "
            "run `jacked service recover` again. No process or supervisor was "
            "changed."
        )
        raise SystemExit(1) from exc
    except (OSError, ValueError) as exc:
        console.print(
            f"[red]Recovery could not establish ownership:[/red] {type(exc).__name__}. "
            "No process or supervisor was changed."
        )
        raise SystemExit(1) from exc
    if quarantined is not None:
        console.print(
            "[yellow]Quarantined invalid ownership evidence:[/yellow] "
            f"{quarantined}"
        )

    result = install_native_owned(spec, environment=environment, paths=paths)
    if not result.ok:
        console.print(f"[red]Recovery activation refused:[/red] {result.reason}")
        console.print(f"  Artifact: {artifact_path}")
        endpoint = discover_service(paths)
        if endpoint.source == "manifest" and endpoint.port:
            console.print(
                f"  Owned service remains at http://127.0.0.1:{endpoint.port}"
            )
        raise SystemExit(1)
    ready = _wait_owned_service_ready(
        paths,
        expected_generation=spec.generation,
    )
    if ready is None:
        console.print(
            "[red]Recovery activation failed:[/red] the exact service generation "
            "did not become ready."
        )
        raise SystemExit(1)
    console.print(
        f"[green][OK][/green] Recovered exact service generation {spec.generation[:12]}"
    )


@service.command(name="uninstall")
def service_uninstall():
    """Remove the exact owned jacked auto-start configuration."""
    from jacked.service.lifecycle import (
        default_service_paths,
        provision_service_contract,
        uninstall_native_owned,
    )

    paths = default_service_paths()
    try:
        spec, environment = provision_service_contract(paths=paths)
        result = uninstall_native_owned(spec, environment=environment, paths=paths)
    except (OSError, ValueError) as exc:
        console.print(
            f"[red]Safe service uninstall failed:[/red] {type(exc).__name__}. "
            "No PID or port owner was signalled."
        )
        raise SystemExit(1) from exc
    if not result.ok:
        console.print(f"[red]Service uninstall refused:[/red] {result.reason}")
        console.print("No PID or port owner was signalled.")
        raise SystemExit(1)
    console.print(f"[green][OK][/green] {result.reason}")


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
    import json

    def _load_permissions(settings_path: Path) -> list[str]:
        """Bash permission allow patterns from a settings JSON file."""
        try:
            if not settings_path.exists():
                return []
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            return [
                p
                for p in data.get("permissions", {}).get("allow", [])
                if isinstance(p, str) and p.startswith("Bash(")
            ]
        except Exception:
            return []

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


@permissions_group.command(name="audit")
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
def permissions_audit(fix, yes):
    """Audit permission rules for dangerous wildcards."""

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
                f"         A blanket wildcard auto-approves ANY {prefix} invocation, "
                "including inline code execution.\n"
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
            "\n[yellow]TIP: Remove dangerous wildcards so Claude Code evaluates those commands individually.[/yellow]"
        )
        console.print(
            "[dim]Run 'jacked permissions audit --fix' to prune them interactively.[/dim]"
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

    ACCOUNT can be an account ID, a full email address, or a unique
    part of an email. If omitted, uses the currently active account
    (set via dashboard "Use" button).

    All additional arguments are passed through to claude.

    Examples:
        jacked claude 2
        jacked claude alice@test.com
        jacked claude udifi
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


# ── Subscription usage snapshot ────────────────────────────────────


def _usage_row(acct: dict, now) -> dict:
    """Shape one account row for `jacked usage` output.

    Field-ALLOWLISTED on purpose: token columns must never reach stdout.
    Percents are defensively coerced (SQLite dynamic typing can hand back
    TEXT); cache_age_seconds is SIGNED — negative means clock skew, which
    should be visible, not silently clamped to "fresh".
    """
    from jacked.service.usage_pacing import cache_age_seconds, coerce_pct

    return {
        "id": acct.get("id"),
        "provider": acct.get("provider") or "claude",
        "email": acct.get("email"),
        "subscription_type": acct.get("subscription_type"),
        "is_active": bool(acct.get("is_active", 1)),
        "validation_status": acct.get("validation_status"),
        "usage_5h_pct": coerce_pct(acct.get("cached_usage_5h")),
        "usage_7d_pct": coerce_pct(acct.get("cached_usage_7d")),
        "resets_5h_at": acct.get("cached_5h_resets_at"),
        "resets_7d_at": acct.get("cached_7d_resets_at"),
        "cache_age_seconds": cache_age_seconds(acct, now),
    }


def _render_usage_table(rows: list[dict], summary: dict) -> None:
    """Human table for `jacked usage` (JSON mode is the machine contract)."""
    from rich.table import Table as _Table

    def _pct(v):
        return f"{v:.0f}%" if isinstance(v, (int, float)) else "?"

    t = _Table(title="Subscription usage (cached)")
    for col in ("ID", "Provider", "Email", "Plan", "5h %", "7d %",
                "5h resets (UTC)", "7d resets (UTC)", "Cache age"):
        t.add_column(col)
    for r in rows:
        age_s = r["cache_age_seconds"]
        # Negative age = clock skew (deliberately unclamped); label it.
        age = "?" if age_s is None else ("skew" if age_s < 0 else f"{age_s // 60}m")
        t.add_row(
            str(r["id"]), r["provider"], r["email"] or "?",
            r["subscription_type"] or "?",
            _pct(r["usage_5h_pct"]), _pct(r["usage_7d_pct"]),
            r["resets_5h_at"] or "?", r["resets_7d_at"] or "?", age,
        )
    console.print(t)
    if summary.get("pause_until"):
        # pause_until is set when ANY eligible window is constrained — it does
        # NOT mean the whole fleet is exhausted, so the copy must not say so.
        console.print(
            f"Earliest constrained-window reset: [bold]{summary['pause_until']}[/bold]"
        )


@main.command(name="usage")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option(
    "--include-inactive", is_flag=True,
    help="Include accounts marked inactive in the dashboard.",
)
def usage_cmd(as_json, include_inactive):
    """Show cached subscription usage per account (5h/7d windows + resets).

    Reads the same cached rate-limit windows the dashboard shows. Does NOT
    call any provider API — data is as fresh as the last dashboard/menubar
    refresh (see cache_age_seconds). Autonomous loops (e.g. the night-shift
    skill) use --json to decide whether to pause: summary.pause_until is the
    earliest FUTURE reset among CONSTRAINED (>=90% effective) windows of
    eligible accounts, staleness-adjusted (a past resets_at means the cached
    percent is stale headroom). The JSON contract is pinned by
    tests/unit/test_usage_cmd.py.

    >>> # CLI command: jacked usage [--json] [--include-inactive]
    """
    import json as _json
    from datetime import datetime, timezone

    from jacked.service.usage_pacing import compute_best_account_summary
    from jacked.web.database import Database

    db_path = Path.home() / ".claude" / "jacked.db"
    if not db_path.exists():
        payload = {"available": False, "reason": "jacked database not found; run 'jacked webux' once"}
        if as_json:
            click.echo(_json.dumps(payload))
        else:
            console.print("[yellow]jacked database not found. Run 'jacked webux' first.[/yellow]")
        return

    db = Database(str(db_path))
    try:
        accounts = db.list_accounts(include_inactive=include_inactive)
    finally:
        db.close()

    now = datetime.now(timezone.utc)
    rows = [_usage_row(a, now) for a in accounts]
    summary = compute_best_account_summary(accounts, now=now)

    if as_json:
        click.echo(_json.dumps({"available": True, "accounts": rows, "summary": summary}))
        return
    if not rows:
        console.print("[yellow]No accounts found.[/yellow]")
        return
    _render_usage_table(rows, summary)


# ── Codex provider commands ──────────────────────────────────────────


@main.group(name="codex")
def codex_group():
    """Manage OpenAI Codex accounts (add, list, switch, launch)."""


@codex_group.command(name="add")
@click.option(
    "--make-active", is_flag=True,
    help="Mark this as the active Codex account after adding.",
)
@click.option(
    "--no-login", is_flag=True,
    help="Don't run `codex login`; import an already-logged-in account only.",
)
def codex_add_cmd(make_active, no_login):
    """Add (or refresh) the logged-in Codex account in jacked.

    Forces file-based credential storage (so jacked can manage the account),
    runs `codex login` if you're not signed in, then imports the account's
    identity. Tokens stay in ~/.codex/auth.json — jacked stores identity only.

    >>> # CLI command: jacked codex add [--make-active] [--no-login]
    """
    from jacked.codex.accounts import CodexImportError, add_codex_account

    db = _codex_db()
    try:
        acct = add_codex_account(
            db, run_login=not no_login, make_active=make_active
        )
    except CodexImportError as exc:
        raise click.ClickException(str(exc))
    finally:
        db.close()

    plan = acct.get("subscription_type") or "?"
    console.print(
        f"Added Codex account [bold]{acct['email']}[/bold] "
        f"(plan {plan}, id {acct['id']})."
    )
    if make_active:
        console.print(
            "It's now the active Codex account — restart Codex to pick it up."
        )
    else:
        console.print(
            f"Its usage will show in the dashboard. Make it active with "
            f"[bold]jacked codex use {acct['id']}[/bold]."
        )


def _codex_db():
    from jacked.web.database import Database

    return Database(str(Path.home() / ".claude" / "jacked.db"))


@codex_group.command(name="list")
def codex_list_cmd():
    """List the Codex accounts jacked knows about.

    >>> # CLI command: jacked codex list
    """
    db = _codex_db()
    try:
        active = db.get_active_account_id("codex")
        rows = [a for a in db.list_accounts() if a.get("provider") == "codex"]
    finally:
        db.close()
    if not rows:
        console.print("No Codex accounts yet. Add one with [bold]jacked codex add[/bold].")
        return
    for a in rows:
        mark = "→ " if a["id"] == active else "  "
        plan = a.get("subscription_type") or "?"
        console.print(f"{mark}[bold]{a['id']}[/bold]  {a['email']}  ({plan})")


@codex_group.command(name="use")
@click.argument("account_id", type=int)
def codex_use_cmd(account_id):
    """Make ACCOUNT_ID the active Codex account (swaps ~/.codex/auth.json).

    Restart Codex afterwards — it caches auth at startup.

    >>> # CLI command: jacked codex use <id>
    """
    from jacked.codex.switching import CodexSwapError, swap_codex_account

    db = _codex_db()
    try:
        swap_codex_account(db, account_id)
    except CodexSwapError as exc:
        raise click.ClickException(str(exc))
    finally:
        db.close()
    console.print(
        f"Active Codex account → [bold]{account_id}[/bold]. "
        "Restart Codex (CLI/app/IDE) to pick it up — it caches auth at startup."
    )


@codex_group.command(
    name="launch", context_settings={"ignore_unknown_options": True}
)
@click.argument("account_id", type=int)
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex_launch_cmd(account_id, codex_args):
    """Launch Codex isolated on ACCOUNT_ID via its own CODEX_HOME.

    Unlike `use`, this does NOT touch the shared root account — it runs Codex in
    a per-account home, sidestepping the single-file refresh-token race. Extra
    args pass through to `codex`.

    >>> # CLI command: jacked codex launch <id> [codex args...]
    """
    from jacked.codex.switching import CodexSwapError, launch_codex

    try:
        console.print(f"Launching Codex on account [bold]{account_id}[/bold]...")
        launch_codex(account_id, codex_args)
    except CodexSwapError as exc:
        raise click.ClickException(str(exc))


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
