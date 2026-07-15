"""FastAPI application for jacked web dashboard."""

import asyncio
import json
import logging
import logging.handlers
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jacked import __version__
from jacked.api.watchers import (
    logs_watch_loop,
    process_alive_sweeper_loop,
    session_accounts_watch_loop,
)
from jacked.api.usage_monitor import (
    active_account_poll_loop,
    active_poll_watchdog_loop,
    all_accounts_refresh_loop,
    full_sweep_loop,
)
from jacked.api.log_capture import server_log_buffer
from jacked.api.security import (
    HostValidationMiddleware,
    build_allowed_origins,
    origin_allowed,
)
from jacked.api.websocket import WebSocketRegistry

logger = logging.getLogger(__name__)

# Monotonic counter of completed lifespan startups in this process.
# Bumped on each lifespan entry so on-call can distinguish "first
# startup" from "N-th tray restart" in logs that share the same PID
# (the tray restarts uvicorn in a new thread without forking).
_lifespan_startup_count: int = 0

# Static web files shipped with the package
WEB_DIR = Path(__file__).parent.parent / "data" / "web"

TOKEN_REFRESH_INTERVAL = 1800  # 30 minutes
WS_KEEPALIVE_INTERVAL = 30  # seconds between WebSocket pings
HEAL_SWEEP_INTERVAL = 300  # 5 minutes between heal sweeps
SWEEP_PASS_TIMEOUT = 600  # hard cap on a single refresh/heal pass
LOG_FILE_MAX_BYTES = 5_000_000  # 5 MB
LOG_FILE_BACKUP_COUNT = 3


async def _token_refresh_loop():
    """Background task to refresh tokens every 30 minutes."""
    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
        try:
            from jacked.web.auth import refresh_all_expiring_tokens

            result = await asyncio.wait_for(
                refresh_all_expiring_tokens(buffer_seconds=14400),
                timeout=SWEEP_PASS_TIMEOUT,
            )
            if result["refreshed"] > 0 or result["failed"] > 0:
                logger.info(
                    "Token refresh: checked=%d, refreshed=%d, failed=%d",
                    result["checked"],
                    result["refreshed"],
                    result["failed"],
                )
        except asyncio.TimeoutError:
            # wait_for cancels the pass; an in-flight token exchange is
            # shielded inside _refresh_token_flow and finishes in the
            # background (persisting any rotated token and releasing its
            # per-account lock) — the loop self-heals from a wedged pass.
            logger.error(
                "Token refresh pass exceeded %ds and was cancelled",
                SWEEP_PASS_TIMEOUT,
            )
        except Exception as e:
            logger.warning("Token refresh loop error: %s", e)


async def _heal_sweep_loop():
    """Background task to heal stuck accounts every 5 minutes."""
    while True:
        await asyncio.sleep(HEAL_SWEEP_INTERVAL)
        try:
            from jacked.web.auth import heal_invalid_accounts

            await asyncio.wait_for(heal_invalid_accounts(), timeout=SWEEP_PASS_TIMEOUT)
        except asyncio.TimeoutError:
            # wait_for cancels the pass; an in-flight token exchange is
            # shielded inside _refresh_token_flow and finishes in the
            # background (persisting any rotated token and releasing its
            # per-account lock) — the loop self-heals from a wedged pass.
            logger.error(
                "Heal sweep exceeded %ds and was cancelled", SWEEP_PASS_TIMEOUT,
            )
        except Exception as e:
            logger.warning("Heal sweep error: %s", e)


async def _stuck_checking_watchdog_loop(app):
    """Reset stuck validation_status='checking' rows every 60s.

    See 0.41.23 spec (docs/superpowers/specs/2026-04-19-stuck-checking-
    watchdog-design.md) for the failure mode this defends against.
    """
    from jacked.web.auth import reset_stale_checking_accounts

    interval = 60
    while True:
        try:
            db = getattr(app.state, "db", None)
            if db is not None:
                count = await reset_stale_checking_accounts(db, threshold_seconds=120)
                if count > 0:
                    logger.info(
                        "Stuck-checking watchdog reset %d account(s)", count,
                    )
        except asyncio.CancelledError:
            logger.info("Stuck-checking watchdog cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Stuck-checking watchdog error", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # Rebind every module-level asyncio.Lock / Event to this event loop.
    # The tray restarts uvicorn in a fresh thread (new loop) while this
    # process keeps the old modules imported, so primitives from the
    # previous loop would otherwise raise "bound to a different event
    # loop" on the next acquire and callers would accumulate as phantom
    # waiters. Each module is reset independently — one failure should
    # not skip the rest, and the traceback must be preserved so on-call
    # can identify which module went sideways.
    #
    # Contract for any new module that owns a module-level asyncio.Lock
    # / Event / Semaphore / Condition: expose ``reset_locks()`` and add
    # the module to this tuple. ``tests/unit/api/test_reset_locks.py``
    # enforces this at CI time.
    global _lifespan_startup_count
    _lifespan_startup_count += 1
    startup_n = _lifespan_startup_count

    # Per-module import: one broken module (e.g. syntax error in a fresh
    # refactor) should not wedge the reset for the other seven.
    _module_specs: tuple[str, ...] = (
        "jacked.api.routes.auth",
        "jacked.web.auth",
        "jacked.api.routes.features",
        "jacked.api.routes.packs",
        "jacked.api.routes.permissions",
        "jacked.api.routes.system",
        "jacked.api.usage_monitor",
        "jacked.web.oauth",
    )
    modules_to_reset: list = []
    import_failed: list[str] = []
    import importlib
    for mod_path in _module_specs:
        try:
            modules_to_reset.append(importlib.import_module(mod_path))
        except Exception:
            import_failed.append(mod_path)
            logger.exception("Failed to import %s for lock reset", mod_path)

    reset_ok: list[str] = []
    reset_failed: list[str] = []
    for mod in modules_to_reset:
        name = getattr(mod, "__name__", repr(mod))
        reset = getattr(mod, "reset_locks", None)
        if not callable(reset):
            continue
        try:
            reset()
            reset_ok.append(name)
        except Exception:
            reset_failed.append(name)
            logger.exception("reset_locks failed for %s", name)

    if not modules_to_reset:
        logger.error(
            "Lifespan lock reset #%d SKIPPED (pid=%d) — every lock-owning "
            "module failed to import: %s",
            startup_n, os.getpid(), import_failed,
        )
    else:
        logger.info(
            "Lifespan lock reset #%d (pid=%d): ok=%d failed=%d imports_failed=%d modules=%s",
            startup_n, os.getpid(), len(reset_ok), len(reset_failed),
            len(import_failed), reset_ok,
        )
    if reset_failed:
        logger.error("Lock reset failures in: %s", reset_failed)

    try:
        from jacked.web.database import Database

        app.state.db = Database()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning("Database init failed: %s", e)
        app.state.db = None

    app.state.ws_registry = WebSocketRegistry()

    host = os.environ.get("JACKED_HOST", "127.0.0.1")
    port = int(os.environ.get("JACKED_PORT", "8321"))
    app.state.host = host
    app.state.port = port
    app.state.allowed_origins = build_allowed_origins(host, port)
    # Keep the CORS layer consistent with the live allowlist: CORSMiddleware
    # holds a reference to _cors_origins (an import-time env read), while the
    # WS gate and CSRF guard evaluate at request time. Refresh in place so an
    # env change between import and startup (e.g. the tray's in-process
    # uvicorn restart) cannot make CORS drift from enforcement. Pinned by
    # test_cors_middleware_honors_in_place_refresh, which fails loudly if a
    # future Starlette stops honoring in-place mutation.
    _cors_origins[:] = app.state.allowed_origins

    # Wire server log capture (buffer-only until loop/registry set)
    _log_handler = server_log_buffer.handler
    server_log_buffer.set_loop(asyncio.get_running_loop())
    server_log_buffer.set_registry(app.state.ws_registry)
    logging.getLogger().addHandler(_log_handler)

    # Persistent file logging with rotation
    _file_handler = None
    log_path = Path.home() / ".claude" / "jacked-server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUP_COUNT,
        )
        _fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        _fmt.converter = time.gmtime
        _fh.setFormatter(_fmt)
        os.chmod(str(log_path), 0o600)
        # Attach only after chmod succeeds — prevents handler leak if chmod fails
        logging.getLogger().addHandler(_fh)
        _file_handler = _fh
    except Exception as e:
        logger.warning("File logging unavailable: %s", e)
        _file_handler = None

    # Lower jacked namespace to INFO so messages reach the capture handler.
    # basicConfig() in cli.py sets root to WARNING which would suppress them.
    _jacked_logger = logging.getLogger("jacked")
    if _jacked_logger.getEffectiveLevel() > logging.INFO:
        _jacked_logger.setLevel(logging.INFO)

    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Dashboard bound to %s: reachable beyond this machine. Same-origin "
            "enforcement and Host validation are active, but there is no "
            "authentication layer. Restrict reachability at the network layer "
            "(VPN such as Tailscale, firewall, or tailnet ACLs).",
            host,
        )

    # Start background tasks
    refresh_task = asyncio.create_task(_token_refresh_loop())
    session_watch_task = asyncio.create_task(session_accounts_watch_loop(app))
    logs_watch_task = asyncio.create_task(logs_watch_loop(app))
    sweeper_task = asyncio.create_task(process_alive_sweeper_loop(app))
    heal_task = asyncio.create_task(_heal_sweep_loop())
    active_poll_task = asyncio.create_task(active_account_poll_loop(app))
    # Expose poll-loop liveness state for the watchdog. The watchdog
    # respawns the task if heartbeat goes stale — see the 2026-05-10
    # silent-task-death incident for why this exists.
    app.state.active_poll_task = active_poll_task
    app.state.active_poll_last_tick_at = time.monotonic()
    app.state.active_poll_last_respawn_at = 0.0
    active_poll_watchdog_task = asyncio.create_task(active_poll_watchdog_loop(app))
    full_sweep_task = asyncio.create_task(full_sweep_loop(app))
    all_accounts_refresh_task = asyncio.create_task(all_accounts_refresh_loop(app))
    stuck_checking_task = asyncio.create_task(_stuck_checking_watchdog_loop(app))
    logger.info("Started background token refresh (every 30min)")
    logger.info("Started session-accounts watcher (every 3s)")
    logger.info("Started logs watcher (every 3s)")
    logger.info("Started process-alive sweeper (every 60s)")
    logger.info("Started heal sweep (every 5min)")
    logger.info("Started active account poll loop (60s)")
    logger.info("Started active poll watchdog (every 60s)")
    logger.info("Started full sweep loop")
    logger.info("Started all-accounts refresh loop (every 10min)")
    logger.info("Started stuck-checking watchdog (every 60s)")

    # Start analytics scanner + live monitor
    analytics_scan_task = None
    analytics_monitor_task = None
    try:
        from jacked.web.analytics_monitor import initial_scan_loop, live_monitor_loop
        analytics_scan_task = asyncio.create_task(initial_scan_loop(app))
        analytics_monitor_task = asyncio.create_task(live_monitor_loop(app))
        logger.info("Started analytics scanner + live monitor")
    except Exception as e:
        logger.warning("Analytics module unavailable: %s", e)

    yield

    # Shutdown: cancel background tasks. Use the CURRENT active_poll_task
    # from app.state — the watchdog may have respawned it during the run,
    # so the local `active_poll_task` reference can be stale.
    tasks_to_cancel = [
        refresh_task, session_watch_task, logs_watch_task, sweeper_task,
        heal_task,
        getattr(app.state, "active_poll_task", active_poll_task),
        active_poll_watchdog_task,
        full_sweep_task,
        all_accounts_refresh_task,
        stuck_checking_task,
    ]
    if analytics_scan_task is not None:
        tasks_to_cancel.append(analytics_scan_task)
    if analytics_monitor_task is not None:
        tasks_to_cancel.append(analytics_monitor_task)
    for task in tasks_to_cancel:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Detach server log capture
    logging.getLogger().removeHandler(_log_handler)
    server_log_buffer.detach()

    if _file_handler:
        logging.getLogger().removeHandler(_file_handler)
        _file_handler.close()

    db = getattr(app.state, "db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


app = FastAPI(
    title="jacked dashboard",
    description="Local web dashboard for Claude Code account management and analytics.",
    version=__version__,
    lifespan=lifespan,
)

_cors_origins = build_allowed_origins(
    os.environ.get("JACKED_HOST", "127.0.0.1"),
    int(os.environ.get("JACKED_PORT", "8321")),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost: covers HTTP and WebSocket handshakes (DNS-rebinding guard).
app.add_middleware(HostValidationMiddleware)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": {"message": str(exc), "code": "VALIDATION_ERROR"}},
    )


@app.exception_handler(FileNotFoundError)
async def not_found_handler(request: Request, exc: FileNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": {"message": str(exc), "code": "NOT_FOUND"}},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {"message": "An internal error occurred", "code": "INTERNAL_ERROR"}
        },
    )


@app.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket):
    """General-purpose WebSocket event bus."""
    # Browsers do not apply CORS to WebSockets; enforce Origin here.
    # Same-origin with the requested Host (loopback, MagicDNS, LAN IP)
    # or an explicit allowlist entry; everything else is a cross-site
    # page trying to ride an allowed machine's network access.
    allowed = getattr(app.state, "allowed_origins", None)
    if allowed is None:
        allowed = build_allowed_origins(
            os.environ.get("JACKED_HOST", "127.0.0.1"),
            int(os.environ.get("JACKED_PORT", "8321")),
        )
    if not origin_allowed(
        ws.headers.get("origin", ""), ws.headers.get("host", ""), allowed
    ):
        await ws.close(code=4003, reason="Origin not allowed")
        return

    await ws.accept()

    raw_topics = ws.query_params.get("topics", "*")
    topics = [t.strip() for t in raw_topics.split(",") if t.strip()]
    if not topics:
        topics = ["*"]

    registry: WebSocketRegistry = app.state.ws_registry
    await registry.connect(ws, topics)
    logger.debug(
        "WebSocket client connected (topics=%s, total=%d)",
        topics,
        registry.client_count,
    )

    try:
        async def _keepalive():
            while True:
                await asyncio.sleep(WS_KEEPALIVE_INTERVAL)
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

        keepalive_task = asyncio.create_task(_keepalive())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
    finally:
        registry.disconnect(ws)
        logger.debug("WebSocket client disconnected (total=%d)", registry.client_count)


# --- Include route modules ---

from jacked.api.routes import system, analytics, features, logs, permissions, menubar, packs  # noqa: E402
from jacked.api.routes.settings_swap import router as swap_settings_router  # noqa: E402
from jacked.api.routes.settings_remote import router as remote_access_router  # noqa: E402

# Dedicated /api/settings routers MUST be registered before the system router —
# system.py has a catch-all PUT /settings/{key} that would steal these routes
# otherwise (e.g. PUT /api/settings/remote-access).
app.include_router(swap_settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(remote_access_router, prefix="/api/settings", tags=["settings"])
app.include_router(system.router, prefix="/api", tags=["system"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(features.router, prefix="/api", tags=["features"])
app.include_router(packs.router, prefix="/api", tags=["packs"])
app.include_router(logs.router, prefix="/api", tags=["logs"])
app.include_router(permissions.router, prefix="/api", tags=["permissions"])
app.include_router(menubar.router, prefix="/api", tags=["menubar"])

# Auth routes (includes credential switching + session queries)
try:
    from jacked.api.routes import auth  # noqa: E402

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
except ImportError:
    logger.debug("Auth routes not loaded (web backend modules not yet available)")


# --- Static files + SPA catch-all ---

if WEB_DIR.exists():
    _css_dir = WEB_DIR / "css"
    _js_dir = WEB_DIR / "js"
    _assets_dir = WEB_DIR / "assets"

    # Middleware: prevent browser caching of static assets so code changes
    # take effect without hard refresh after jacked install + server restart.
    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/js/", "/css/")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    if _css_dir.exists():
        app.mount("/css", StaticFiles(directory=_css_dir), name="css")
    if _js_dir.exists():
        app.mount("/js", StaticFiles(directory=_js_dir), name="js")
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/panel")
    async def serve_panel():
        """Serve the compact usage panel hosted in the macOS popover + side panel.

        Registered before the SPA catch-all so GET /panel returns panel.html
        (the file has a .html extension, so the catch-all would otherwise miss
        it and fall back to index.html).
        """
        panel_path = WEB_DIR / "panel.html"
        if panel_path.exists():
            return FileResponse(
                panel_path,
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": "Panel not found", "code": "NOT_FOUND"}},
        )

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve static files or fall back to index.html for SPA routing."""
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": {"message": "Not found", "code": "NOT_FOUND"}},
            )

        file_path = (WEB_DIR / full_path).resolve()
        web_resolved = WEB_DIR.resolve()

        try:
            file_path.relative_to(web_resolved)
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": {"message": "Invalid path", "code": "BAD_REQUEST"}},
            )

        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

        index_path = WEB_DIR / "index.html"
        if index_path.exists():
            return FileResponse(
                index_path,
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": "Frontend not found", "code": "NOT_FOUND"}},
        )
