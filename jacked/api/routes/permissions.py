"""Project-level permission management and single-rule add endpoint."""

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jacked.api.routes.features import (
    VALID_PERMISSION_MODES,
    _read_settings_json,
    _settings_lock,
    _write_settings_json,
)

router = APIRouter()

_project_settings_lock = asyncio.Lock()


def _read_project_settings(repo_path: str) -> dict:
    """Read <repo>/.claude/settings.local.json, returning {} if missing."""
    path = Path(repo_path) / ".claude" / "settings.local.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_project_settings(repo_path: str, data: dict):
    """Write <repo>/.claude/settings.local.json atomically."""
    claude_dir = Path(repo_path) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "settings.local.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _validate_repo_path(repo_path: str, db=None) -> Path | None:
    """Validate repo_path is a real git project directory known to jacked.

    Rejects root, home, non-directories, paths without .git, and
    projects not known to jacked (matching _validate_project_path
    pattern in system.py).
    """
    if not repo_path:
        return None
    p = Path(repo_path).resolve()
    if not p.is_dir() or p == Path("/") or p == Path.home():
        return None
    if not (p / ".git").exists():
        return None
    # Must be a project known to jacked (has session activity in DB)
    if db is not None:
        try:
            rows = db.get_project_activity_summary(limit=100)
            known_paths = {r["repo_path"] for r in rows if r.get("repo_path")}
            normalized = str(p).replace("\\", "/")
            if str(p) not in known_paths and normalized not in known_paths:
                return None
        except Exception:
            return None  # Fail closed — reject if DB unavailable
    return p


# --- Endpoints ---


@router.get("/claude-settings/project-permissions")
async def get_project_permissions(repo_path: str, request: Request):
    """Read project-level Claude Code permissions."""
    db = getattr(request.app.state, "db", None)
    resolved = _validate_repo_path(repo_path, db=db)
    if not resolved:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Invalid repo_path"}},
        )
    settings = _read_project_settings(str(resolved))
    perms = settings.get("permissions", {})
    return {
        "allow": perms.get("allow", []),
        "deny": perms.get("deny", []),
        "ask": perms.get("ask", []),
        "defaultMode": perms.get("defaultMode", "default"),
    }


class ProjectPermissionsRequest(BaseModel):
    repo_path: str
    allow: list[str] | None = None
    deny: list[str] | None = None
    ask: list[str] | None = None
    defaultMode: str | None = None


@router.put("/claude-settings/project-permissions")
async def set_project_permissions(body: ProjectPermissionsRequest, request: Request):
    """Update project-level Claude Code permissions."""
    if body.defaultMode and body.defaultMode not in VALID_PERMISSION_MODES:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": f"Invalid mode: {body.defaultMode}"}},
        )
    db = getattr(request.app.state, "db", None)
    resolved = _validate_repo_path(body.repo_path, db=db)
    if not resolved:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Invalid repo_path"}},
        )
    # Validate all patterns against the same rules as add_permission_rule —
    # dangerous-patterns blocklist + file-tool depth check. Prevents bypass via bulk PUT.
    for list_name, patterns in [("allow", body.allow), ("deny", body.deny), ("ask", body.ask)]:
        if patterns is None:
            continue
        for pat in patterns:
            stripped = pat.strip()
            if stripped.lower() in _DANGEROUS_PATTERNS_LOWER:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "error": {
                            "message": f"Pattern '{pat}' in {list_name} list is too broad and would bypass gatekeeper security"
                        }
                    },
                )
            ft_match = _FILE_TOOL_PREFIX_RE.match(stripped)
            if ft_match and ft_match.group(1) in _FILE_TOOLS:
                segments = [s for s in ft_match.group(2).split("/") if s]
                if len(segments) < 3:
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={
                            "error": {
                                "message": (
                                    f"Pattern '{pat}' in {list_name} list is too broad — "
                                    "path must have at least 3 directory segments"
                                )
                            }
                        },
                    )

    async with _project_settings_lock:
        settings = _read_project_settings(str(resolved))
        if "permissions" not in settings:
            settings["permissions"] = {}
        if body.allow is not None:
            settings["permissions"]["allow"] = body.allow
        if body.deny is not None:
            settings["permissions"]["deny"] = body.deny
        if body.ask is not None:
            settings["permissions"]["ask"] = body.ask
        if body.defaultMode is not None:
            settings["permissions"]["defaultMode"] = body.defaultMode
        _write_project_settings(str(resolved), settings)
    return {"ok": True}


# Non-exhaustive UX guardrail — gatekeeper DENY_PATTERNS (Tier 0) fire before
# PERMS (Tier 2), so this blocklist prevents user confusion, not security bypass.
_DANGEROUS_PATTERNS = {
    "Bash(*:*)",
    "Bash(rm:*)",
    "Bash(sudo:*)",
    "Bash(rm -rf:*)",
    "Bash(sh:*)",
    "Bash(bash:*)",
    # File-tool root patterns — would allow access to every file on the system
    "Read(/:*)",
    "Edit(/:*)",
    "Write(/:*)",
    "Grep(/:*)",
    "Glob(/:*)",
    "NotebookEdit(/:*)",
    # Empty-prefix patterns — bypass all path matching
    "Read(:*)",
    "Edit(:*)",
    "Write(:*)",
    "Grep(:*)",
    "Glob(:*)",
    "NotebookEdit(:*)",
    "Bash(:*)",
}
_DANGEROUS_PATTERNS_LOWER = {p.lower() for p in _DANGEROUS_PATTERNS}

_FILE_TOOLS = {"Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"}
_FILE_TOOL_PREFIX_RE = re.compile(r"^(\w+)\((.+):\*\)$")


class AddRuleRequest(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=500)
    list_name: str = "allow"
    scope: str = "global"
    repo_path: str | None = None


@router.post("/claude-settings/permissions/rule")
async def add_permission_rule(body: AddRuleRequest, request: Request):
    """Add a single permission rule to allow/deny/ask list."""
    if body.list_name not in ("allow", "deny", "ask"):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Invalid list_name"}},
        )
    pattern = body.pattern.strip()
    if not pattern:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Pattern required"}},
        )
    # Case-insensitive check: tool names are case-sensitive in Claude Code
    # (Read, Bash, etc.) but we normalize to catch bypass attempts like "bash(*:*)".
    if pattern.lower() in _DANGEROUS_PATTERNS_LOWER:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "message": f"Pattern '{pattern}' is too broad and would bypass gatekeeper security"
                }
            },
        )

    # Block overly-broad file-tool prefix patterns (< 3 directory segments)
    ft_match = _FILE_TOOL_PREFIX_RE.match(pattern)
    if ft_match and ft_match.group(1) in _FILE_TOOLS:
        path_prefix = ft_match.group(2)
        segments = [s for s in path_prefix.split("/") if s]
        if len(segments) < 3:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": {
                        "message": (
                            f"Pattern '{pattern}' is too broad — "
                            "path must have at least 3 directory segments"
                        )
                    }
                },
            )

    if body.scope == "project":
        db = getattr(request.app.state, "db", None)
        resolved = _validate_repo_path(body.repo_path, db=db)
        if not resolved:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": "Invalid repo_path"}},
            )
        async with _project_settings_lock:
            settings = _read_project_settings(str(resolved))
            if "permissions" not in settings:
                settings["permissions"] = {}
            rules = settings["permissions"].get(body.list_name, [])
            if pattern not in rules:
                rules.append(pattern)
                settings["permissions"][body.list_name] = rules
                _write_project_settings(str(resolved), settings)
        return {"ok": True, "scope": "project"}
    else:
        async with _settings_lock:
            settings = _read_settings_json()
            if "permissions" not in settings:
                settings["permissions"] = {}
            rules = settings["permissions"].get(body.list_name, [])
            if pattern not in rules:
                rules.append(pattern)
                settings["permissions"][body.list_name] = rules
                _write_settings_json(settings)
        return {"ok": True, "scope": "global"}
