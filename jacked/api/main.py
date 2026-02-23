"""FastAPI application for jacked web dashboard."""

import asyncio
import json
import logging
import os
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
from jacked.api.websocket import WebSocketRegistry

logger = logging.getLogger(__name__)

# Static web files shipped with the package
WEB_DIR = Path(__file__).parent.parent / "data" / "web"

TOKEN_REFRESH_INTERVAL = 1800  # 30 minutes
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


async def _token_refresh_loop():
    """Background task to refresh tokens every 30 minutes."""
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
    """Background task to heal stuck accounts every 5 minutes."""
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
    app.state.allowed_origins = _build_allowed_origins(host, port)

    if host == "0.0.0.0":
        logger.warning(
            "Dashboard exposed to network — consider using a VPN or tunnel for security"
        )

    # Start background tasks
    refresh_task = asyncio.create_task(_token_refresh_loop())
    session_watch_task = asyncio.create_task(session_accounts_watch_loop(app))
    logs_watch_task = asyncio.create_task(logs_watch_loop(app))
    sweeper_task = asyncio.create_task(process_alive_sweeper_loop(app))
    heal_task = asyncio.create_task(_heal_sweep_loop())
    logger.info("Started background token refresh (every 30min)")
    logger.info("Started session-accounts watcher (every 3s)")
    logger.info("Started logs watcher (every 3s)")
    logger.info("Started process-alive sweeper (every 60s)")
    logger.info("Started heal sweep (every 5min)")

    yield

    # Shutdown: cancel background tasks
    for task in (refresh_task, session_watch_task, logs_watch_task, sweeper_task, heal_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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

_cors_origins = _build_allowed_origins(
    os.environ.get("JACKED_HOST", "127.0.0.1"),
    int(os.environ.get("JACKED_PORT", "8321")),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    allowed = getattr(app.state, "allowed_origins", ["*"])
    if "*" not in allowed:
        origin = ws.headers.get("origin", "")
        if not origin or origin == "null" or origin not in allowed:
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

from jacked.api.routes import system, analytics, features  # noqa: E402

app.include_router(system.router, prefix="/api", tags=["system"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(features.router, prefix="/api", tags=["features"])

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

    if _css_dir.exists():
        app.mount("/css", StaticFiles(directory=_css_dir), name="css")
    if _js_dir.exists():
        app.mount("/js", StaticFiles(directory=_js_dir), name="js")
    if _assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

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
            return FileResponse(index_path)

        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": "Frontend not found", "code": "NOT_FOUND"}},
        )
