"""FastAPI application for jacked web dashboard."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jacked import __version__
from jacked.api.credential_sync import (
    create_missing_credentials_file,
    read_platform_credentials,
    re_stamp_jacked_account_id,
    sync_credential_tokens,
)
from jacked.api.watchers import (
    logs_watch_loop,
    process_alive_sweeper_loop,
    session_accounts_watch_loop,
)
from jacked.api.websocket import WebSocketRegistry

logger = logging.getLogger(__name__)

# Static web files shipped with the package
WEB_DIR = Path(__file__).parent.parent / "data" / "web"

TOKEN_REFRESH_INTERVAL = 1800  # 30 minutes
CRED_WATCH_INTERVAL = 3  # seconds between credential file checks
WS_KEEPALIVE_INTERVAL = 30  # seconds between WebSocket pings
HEAL_SWEEP_INTERVAL = 300  # 5 minutes between heal sweeps


def _build_allowed_origins(host: str, port: int) -> list[str]:
    """Build the CORS / WebSocket allowed origins list.

    >>> _build_allowed_origins("127.0.0.1", 8321)
    ['http://127.0.0.1:8321', 'http://localhost:8321']
    >>> _build_allowed_origins("0.0.0.0", 8321)
    ['*']
    """
    if host == "0.0.0.0":
        return ["*"]
    return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]


async def _credentials_watch_loop(app: FastAPI):
    """Watch Claude Code's credential file and keychain for external changes.

    Polls file mtime every 3 seconds.  When the mtime changes, broadcasts a
    ``credentials_changed`` event through the WebSocket registry.
    Self-writes from the ``/use`` endpoint are suppressed via mtime
    comparison.  External changes trigger a token sync back to the DB.

    On macOS, also polls the Keychain every ~30s (10 cycles) because Claude Code
    writes refreshed tokens to Keychain only — the file doesn't change.
    """
    cred_path = Path.home() / ".claude" / ".credentials.json"
    last_mtime: float | None = None
    _last_bootstrap_attempt = 0.0

    # Keychain polling state (macOS only)
    KEYCHAIN_POLL_EVERY = 10  # ~30s at 3s interval
    keychain_cycle = 0
    last_keychain_token: str | None = None

    # Seed initial mtime
    try:
        last_mtime = await asyncio.to_thread(
            lambda: cred_path.stat().st_mtime if cred_path.exists() else None
        )
    except OSError:
        pass

    # Bootstrap: if file doesn't exist at startup, try to create from keychain/DB
    if last_mtime is None:
        db = getattr(app.state, "db", None)
        if db is not None:
            try:
                created_mtime = await asyncio.to_thread(
                    create_missing_credentials_file, db
                )
                if created_mtime is not None:
                    last_mtime = created_mtime
                    app.state.cred_last_written_mtime = created_mtime
                    logger.info("Bootstrapped .credentials.json from keychain/DB")
            except Exception as exc:
                logger.debug("Startup credential bootstrap failed: %s", exc)
        _last_bootstrap_attempt = time.monotonic()

    while True:
        await asyncio.sleep(CRED_WATCH_INTERVAL)
        try:
            current_mtime = await asyncio.to_thread(
                lambda: cred_path.stat().st_mtime if cred_path.exists() else None
            )
        except OSError:
            continue

        if current_mtime is not None and current_mtime != last_mtime:
            last_mtime = current_mtime

            # Suppress notification if we just wrote the file ourselves
            self_written = getattr(app.state, "cred_last_written_mtime", None)
            if self_written is not None and self_written == current_mtime:
                app.state.cred_last_written_mtime = None
                continue

            # Sync tokens from file → DB (prevents token desync)
            db = getattr(app.state, "db", None)
            if db is not None:
                try:
                    data = await asyncio.to_thread(
                        lambda: (
                            json.loads(cred_path.read_text(encoding="utf-8"))
                            if cred_path.exists() and not cred_path.is_symlink()
                            else None
                        )
                    )
                    if data:
                        await asyncio.to_thread(sync_credential_tokens, db, data)
                        # Re-stamp _jackedAccountId if Claude Code removed it
                        if "_jackedAccountId" not in data:
                            stamp_mtime = await asyncio.to_thread(
                                re_stamp_jacked_account_id, db, data, cred_path
                            )
                            if stamp_mtime is not None:
                                app.state.cred_last_written_mtime = stamp_mtime
                except Exception as exc:
                    logger.debug("Token sync failed (non-fatal): %s", exc)

            registry: WebSocketRegistry = getattr(app.state, "ws_registry", None)
            if registry and registry.client_count > 0:
                await registry.broadcast(
                    "credentials_changed",
                    source="file_watcher",
                )
                logger.info(
                    "Credential file changed — notified %d client(s)",
                    registry.client_count,
                )
        # Keychain polling: detect token changes Claude Code writes only to Keychain
        keychain_cycle += 1
        if keychain_cycle >= KEYCHAIN_POLL_EVERY:
            keychain_cycle = 0
            try:
                kc_data = await asyncio.to_thread(read_platform_credentials)
                if kc_data:
                    kc_token = kc_data.get("claudeAiOauth", {}).get("accessToken")
                    if kc_token and kc_token != last_keychain_token:
                        last_keychain_token = kc_token
                        db = getattr(app.state, "db", None)
                        if db is not None:
                            await asyncio.to_thread(sync_credential_tokens, db, kc_data)
                            stamp_mtime = await asyncio.to_thread(
                                re_stamp_jacked_account_id, db, kc_data, cred_path
                            )
                            if stamp_mtime is not None:
                                last_mtime = stamp_mtime
                                app.state.cred_last_written_mtime = stamp_mtime
                        registry = getattr(app.state, "ws_registry", None)
                        if registry and registry.client_count > 0:
                            await registry.broadcast(
                                "credentials_changed", source="keychain_watcher"
                            )
                            logger.info(
                                "Keychain token change detected — notified %d client(s)",
                                registry.client_count,
                            )
            except Exception as exc:
                logger.debug("Keychain poll failed (non-fatal): %s", exc)

        if current_mtime is None:
            # File missing (deleted or never existed) — try to recreate
            now = time.monotonic()
            if now - _last_bootstrap_attempt >= 60:
                _last_bootstrap_attempt = now
                last_mtime = None
                db = getattr(app.state, "db", None)
                if db is not None:
                    try:
                        created_mtime = await asyncio.to_thread(
                            create_missing_credentials_file, db
                        )
                        if created_mtime is not None:
                            last_mtime = created_mtime
                            app.state.cred_last_written_mtime = created_mtime
                    except Exception as exc:
                        logger.debug(
                            "Credential file recreation failed (non-fatal): %s", exc
                        )


async def _token_refresh_loop():
    """Background task to refresh tokens every 30 minutes.

    Sleeps first (tokens were just loaded), then runs indefinitely.
    Only logs when something actually happens.
    """
    while True:
        await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
        try:
            from jacked.web.auth import refresh_all_expiring_tokens

            result = await refresh_all_expiring_tokens(buffer_seconds=14400)
            if result["refreshed"] > 0 or result["failed"] > 0:
                logger.info(
                    "Token refresh: checked=%d, refreshed=%d, failed=%d",
                    result["checked"],
                    result["refreshed"],
                    result["failed"],
                )
        except Exception as e:
            logger.warning("Token refresh loop error: %s", e)


async def _heal_sweep_loop():
    """Background task to heal stuck accounts every 5 minutes.

    Recovers accounts with validation_status 'invalid' or 'unknown'
    by attempting token refresh or profile validation.
    """
    while True:
        await asyncio.sleep(HEAL_SWEEP_INTERVAL)
        try:
            from jacked.web.auth import heal_invalid_accounts

            await heal_invalid_accounts()
        except Exception as e:
            logger.warning("Heal sweep error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # Startup: initialize database
    try:
        from jacked.web.database import Database

        app.state.db = Database()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning("Database init failed: %s", e)
        app.state.db = None

    # Startup: apply token recovery file (if DB update failed last run)
    if app.state.db is not None:
        try:
            from jacked.web.token_recovery import apply_token_recovery

            apply_token_recovery(app.state.db)
        except Exception as e:
            logger.debug("Token recovery at startup: %s", e)

    # Startup: prune old known_refresh_tokens entries
    if app.state.db is not None:
        try:
            if hasattr(app.state.db, "prune_old_refresh_tokens"):
                app.state.db.prune_old_refresh_tokens()
        except Exception as e:
            logger.debug("RT prune at startup: %s", e)

    # WebSocket registry + host/port/origin config
    app.state.ws_registry = WebSocketRegistry()
    app.state.cred_last_written_mtime = None

    host = os.environ.get("JACKED_HOST", "127.0.0.1")
    port = int(os.environ.get("JACKED_PORT", "8321"))
    app.state.host = host
    app.state.port = port
    app.state.allowed_origins = _build_allowed_origins(host, port)

    if host == "0.0.0.0":
        logger.warning(
            "Dashboard exposed to network — consider using a VPN or tunnel for security"
        )

    # Start background tasks
    refresh_task = asyncio.create_task(_token_refresh_loop())
    cred_watch_task = asyncio.create_task(_credentials_watch_loop(app))
    session_watch_task = asyncio.create_task(session_accounts_watch_loop(app))
    logs_watch_task = asyncio.create_task(logs_watch_loop(app))
    sweeper_task = asyncio.create_task(process_alive_sweeper_loop(app))
    heal_task = asyncio.create_task(_heal_sweep_loop())
    logger.info("Started background token refresh (every 30min)")
    logger.info("Started credential file watcher (every 3s)")
    logger.info("Started session-accounts watcher (every 3s)")
    logger.info("Started logs watcher (every 3s)")
    logger.info("Started process-alive sweeper (every 60s)")
    logger.info("Started heal sweep (every 5min)")

    yield

    # Shutdown: cancel background tasks
    for task in (refresh_task, cred_watch_task, session_watch_task, logs_watch_task, sweeper_task, heal_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Shutdown: close database
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

# CORS — dynamic origins based on JACKED_HOST / JACKED_PORT env vars
# At middleware init time we read env vars directly (lifespan hasn't run yet).
_cors_origins = _build_allowed_origins(
    os.environ.get("JACKED_HOST", "127.0.0.1"),
    int(os.environ.get("JACKED_PORT", "8321")),
)
# allow_credentials must be False when origins is ["*"] (CORS spec violation otherwise)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Exception handlers ---


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


# --- WebSocket event bus ---


@app.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket):
    """General-purpose WebSocket event bus.

    Clients connect and optionally specify topics via query param:
    ``/api/ws?topics=credentials_changed,usage_updated``
    Default is ``*`` (all topics).
    """
    # Origin check — only enforced when not binding to 0.0.0.0
    allowed = getattr(app.state, "allowed_origins", ["*"])
    if "*" not in allowed:
        origin = ws.headers.get("origin", "")
        if not origin or origin == "null" or origin not in allowed:
            await ws.close(code=4003, reason="Origin not allowed")
            return

    await ws.accept()

    # Parse topic subscriptions from query param
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
        # Server-side keepalive — send ping every 30s to survive reverse proxies
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
                # We don't expect client messages, but must consume them
                # to detect disconnects
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

from jacked.api.routes import system, analytics, features  # noqa: E402

app.include_router(system.router, prefix="/api", tags=["system"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(features.router, prefix="/api", tags=["features"])

# Credential switching + session tracking endpoints
try:
    from jacked.api.routes import credentials  # noqa: E402

    app.include_router(credentials.router, prefix="/api/auth", tags=["credentials"])
except ImportError:
    logger.debug("Credentials routes not loaded")

# Auth routes loaded conditionally (depend on web.database, web.oauth, web.auth)
try:
    from jacked.api.routes import auth  # noqa: E402

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
except ImportError:
    logger.debug("Auth routes not loaded (web backend modules not yet available)")


# --- Static files + SPA catch-all ---

if WEB_DIR.exists():
    # Mount css/js/assets as static
    _css_dir = WEB_DIR / "css"
    _js_dir = WEB_DIR / "js"
    _assets_dir = WEB_DIR / "assets"

    if _css_dir.exists():
        app.mount("/css", StaticFiles(directory=_css_dir), name="css")
    if _js_dir.exists():
        app.mount("/js", StaticFiles(directory=_js_dir), name="js")
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve static files or fall back to index.html for SPA routing."""
        # Don't serve SPA for API routes
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": {"message": "Not found", "code": "NOT_FOUND"}},
            )

        # Serve static files if they exist (with path traversal protection)
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

        # SPA catch-all: serve index.html
        index_path = WEB_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)

        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": "Frontend not found", "code": "NOT_FOUND"}},
        )
