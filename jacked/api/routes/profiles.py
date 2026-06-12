"""Profiles API — export, import, validate, list, delete security profiles."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import jacked.profiles as prof

logger = logging.getLogger(__name__)

router = APIRouter()

_profiles_lock = asyncio.Lock()


def reset_locks() -> None:
    """Rebind to the current event loop — see routes.auth.reset_locks."""
    global _profiles_lock
    _profiles_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db(request: Request):
    """Get database from app state, or None."""
    return getattr(request.app.state, "db", None)


def _db_unavailable():
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
    )


def _get_profiles_dir() -> Path:
    return Path.home() / ".claude" / "jacked" / prof.PROFILE_DIR_NAME


def _get_backup_dir() -> Path:
    return _get_profiles_dir() / prof.BACKUP_DIR_NAME


def _read_settings_json() -> dict:
    """Read ~/.claude/settings.json, returning {} if missing or corrupt."""
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_settings_json(data: dict):
    """Write ~/.claude/settings.json atomically (write-to-temp then rename)."""
    path = Path.home() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=100)


class ImportRequest(BaseModel):
    profile: dict


class ValidateRequest(BaseModel):
    profile: dict


class RestoreResponse(BaseModel):
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/")
async def list_profiles_endpoint():
    """List all saved profiles."""
    profiles = prof.list_profiles(_get_profiles_dir())
    return {"profiles": profiles}


@router.post("/export")
async def export_profile_endpoint(body: ExportRequest, request: Request):
    """Export current config as a named profile."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    async with _profiles_lock:
        try:
            settings = _read_settings_json()
            filepath = prof.export_profile(
                name=body.name,
                description=body.description,
                author=body.author,
                db=db,
                settings_json=settings,
                profiles_dir=_get_profiles_dir(),
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": {"message": str(e)}},
            )

    return {"ok": True, "path": str(filepath), "name": body.name}


@router.post("/validate")
async def validate_profile_endpoint(body: ValidateRequest):
    """Validate a profile dict and return warnings."""
    try:
        # Schema validation
        prof.ProfileSchema(**body.profile)
        # Content validation (may raise ValueError for version)
        warnings = prof.validate_profile(body.profile)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"message": str(e)}, "valid": False},
        )

    return {"valid": True, "warnings": warnings}


@router.post("/import")
async def import_profile_endpoint(body: ImportRequest, request: Request):
    """Validate, backup, and apply a profile."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    async with _profiles_lock:
        try:
            settings = _read_settings_json()
            backup_path, warnings = prof.import_profile(
                profile_data=body.profile,
                db=db,
                settings_json=settings,
                write_settings_fn=_write_settings_json,
                profiles_dir=_get_profiles_dir(),
                backup_dir=_get_backup_dir(),
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": {"message": str(e)}},
            )

    return {
        "ok": True,
        "backup_path": str(backup_path),
        "warnings": warnings,
        "name": body.profile.get("name", ""),
    }


@router.post("/restore")
async def restore_backup_endpoint(request: Request):
    """Restore from the latest backup."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    async with _profiles_lock:
        backup_path = prof.get_latest_backup(_get_backup_dir())
        if backup_path is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": {"message": "No backup found"}},
            )

        try:
            prof.restore_backup(backup_path, db, _write_settings_json)
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": f"Restore failed: {e}"}},
            )

    return {"ok": True, "message": "Restored from backup", "backup": str(backup_path)}


@router.delete("/{profile_name}")
async def delete_profile_endpoint(profile_name: str, request: Request):
    """Delete a profile by name."""
    # Basic validation
    if not profile_name or ".." in profile_name or "/" in profile_name:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"message": "Invalid profile name"}},
        )

    deleted = prof.delete_profile(profile_name, _get_profiles_dir())
    if not deleted:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": f"Profile '{profile_name}' not found"}},
        )

    return {"ok": True, "name": profile_name}
