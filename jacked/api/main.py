"""FastAPI application for jacked web dashboard."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jacked import __version__

logger = logging.getLogger(__name__)

# Static web files shipped with the package
WEB_DIR = Path(__file__).parent.parent / "data" / "web"

TOKEN_REFRESH_INTERVAL = 1800  # 30 minutes


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
                    result["checked"], result["refreshed"], result["failed"],
                )
        except Exception as e:
            logger.warning("Token refresh loop error: %s", e)


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

    # Start background token refresh
    refresh_task = asyncio.create_task(_token_refresh_loop())
    logger.info("Started background token refresh (every 30min)")

    yield

    # Shutdown: cancel background tasks
    refresh_task.cancel()
    try:
        await refresh_task
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

# CORS — only allow localhost dashboard origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8321",
        "http://localhost:8321",
    ],
    allow_credentials=True,
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
        content={"error": {"message": "An internal error occurred", "code": "INTERNAL_ERROR"}},
    )


# --- Include route modules ---

from jacked.api.routes import system, analytics, features  # noqa: E402

app.include_router(system.router, prefix="/api", tags=["system"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(features.router, prefix="/api", tags=["features"])

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
