"""System routes — health, version, installations, settings, gatekeeper config."""

from typing import Any, Literal, Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


# --- Pydantic v2 response models ---

class HealthResponse(BaseModel):
    status: str
    db: bool


class VersionResponse(BaseModel):
    current: str
    latest: Optional[str] = None
    outdated: bool = False
    ahead: bool = False
    checked_at: Optional[str] = None
    next_check_at: Optional[str] = None


class InstallationResponse(BaseModel):
    id: int
    repo_path: str
    repo_name: str
    jacked_version: Optional[str] = None
    hooks_installed: Optional[list[str]] = None
    rules_installed: bool = False
    agents_installed: Optional[list[str]] = None
    commands_installed: Optional[list[str]] = None
    guardrails_installed: bool = False
    last_scanned_at: Optional[str] = None
    created_at: Optional[str] = None


class SettingResponse(BaseModel):
    key: str
    value: Any
    updated_at: Optional[str] = None


class SettingUpdateRequest(BaseModel):
    value: Any


class GatekeeperConfigRequest(BaseModel):
    model: Literal["haiku", "sonnet", "opus"] = "haiku"
    eval_method: Literal["api_first", "cli_first", "api_only", "cli_only"] = "api_first"
    api_key: Optional[str] = None


class PathSafetyConfigRequest(BaseModel):
    enabled: bool = True
    allowed_paths: list[str] = []
    disabled_patterns: list[str] = []


class ProjectActivity(BaseModel):
    repo_path: str
    repo_name: str
    gatekeeper_decisions: int = 0
    gatekeeper_allowed: int = 0
    commands_run: int = 0
    hook_executions: int = 0
    last_activity: Optional[str] = None
    first_seen: Optional[str] = None
    unique_sessions: int = 0


class InstalledComponent(BaseModel):
    name: str
    display_name: str
    installed: bool


class GlobalInstallation(BaseModel):
    version: str
    agents: list[InstalledComponent]
    commands: list[InstalledComponent]
    hooks: list[InstalledComponent]
    knowledge: list[InstalledComponent]


class InstallationsOverview(BaseModel):
    global_install: GlobalInstallation
    projects: list[ProjectActivity]
    total_projects: int


# --- Routes ---

@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request):
    """Health check. Returns DB connectivity status."""
    db = getattr(request.app.state, "db", None)
    return HealthResponse(status="ok", db=db is not None)


def _version_response(result: dict | None) -> "VersionResponse":
    """Build VersionResponse from check_version_cached result."""
    from datetime import datetime, timezone

    from jacked import __version__

    if result is None:
        return VersionResponse(current=__version__)

    checked_iso = None
    next_iso = None
    if result.get("checked_at"):
        checked_iso = datetime.fromtimestamp(result["checked_at"], tz=timezone.utc).isoformat()
    if result.get("next_check_at"):
        next_iso = datetime.fromtimestamp(result["next_check_at"], tz=timezone.utc).isoformat()

    return VersionResponse(
        current=__version__,
        latest=result["latest"],
        outdated=result["outdated"],
        ahead=result.get("ahead", False),
        checked_at=checked_iso,
        next_check_at=next_iso,
    )


@router.get("/version", response_model=VersionResponse)
def get_version():
    """Current version and latest PyPI version."""
    from jacked import __version__
    from jacked.version_check import check_version_cached

    return _version_response(check_version_cached(__version__))


@router.post("/version/refresh", response_model=VersionResponse)
def refresh_version(request: Request):
    """Force re-check against PyPI, bypassing cache."""
    from jacked import __version__
    from jacked.version_check import check_version_cached

    result = check_version_cached(__version__, force=True)

    db = getattr(request.app.state, "db", None)
    if db is not None and result is not None:
        try:
            db.record_version_check(
                current_version=__version__,
                latest_version=result["latest"],
                outdated=result["outdated"],
                cache_hit=False,
            )
        except Exception:
            pass

    return _version_response(result)


@router.get("/installations", response_model=list[InstallationResponse])
async def list_installations(request: Request):
    """List repos where jacked is installed."""
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    rows = db.list_installations()
    results = []
    for row in rows:
        r = dict(row)
        for field in ("hooks_installed", "agents_installed", "commands_installed"):
            val = r.get(field)
            if isinstance(val, str):
                try:
                    r[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    r[field] = None
        results.append(InstallationResponse(**r))
    return results


@router.get("/installations/overview", response_model=InstallationsOverview)
async def installations_overview(request: Request):
    """Global install state + per-project activity summary."""
    from pathlib import Path

    from jacked import __version__
    from jacked.api.routes.features import (
        CLAUDE_DIR,
        DATA_ROOT,
        _detect_hook_installed,
        _detect_rules_status,
        _get_valid_agent_names,
        _get_valid_command_names,
        _name_to_display,
        _read_settings_json,
    )

    settings = _read_settings_json()

    # Agents
    agent_names = _get_valid_agent_names()
    agents_dst = CLAUDE_DIR / "agents"
    agents = []
    for name in agent_names:
        installed = (agents_dst / f"{name}.md").exists()
        agents.append(InstalledComponent(name=name, display_name=_name_to_display(name), installed=installed))

    # Commands
    command_names = _get_valid_command_names()
    commands_dst = CLAUDE_DIR / "commands"
    commands = []
    for name in command_names:
        installed = (commands_dst / f"{name}.md").exists()
        commands.append(InstalledComponent(name=name, display_name=_name_to_display(name), installed=installed))

    # Hooks
    hooks = [
        InstalledComponent(name="security_gatekeeper", display_name="Gatekeeper", installed=_detect_hook_installed(settings, "security_gatekeeper")),
        InstalledComponent(name="session_indexing", display_name="Indexing", installed=_detect_hook_installed(settings, "session_indexing")),
        InstalledComponent(name="sounds", display_name="Sounds", installed=_detect_hook_installed(settings, "sounds")),
    ]

    # Knowledge
    rules_status = _detect_rules_status()
    skill_installed = (CLAUDE_DIR / "skills" / "jacked" / "SKILL.md").exists()
    ref_installed = (CLAUDE_DIR / "jacked-reference.md").exists()
    knowledge = [
        InstalledComponent(name="rules", display_name="Rules", installed=rules_status.get("installed", False)),
        InstalledComponent(name="skill", display_name="Skill", installed=skill_installed),
        InstalledComponent(name="reference", display_name="Reference", installed=ref_installed),
    ]

    global_install = GlobalInstallation(
        version=__version__,
        agents=agents,
        commands=commands,
        hooks=hooks,
        knowledge=knowledge,
    )

    # Project activity from DB
    projects: list[ProjectActivity] = []
    total_projects = 0
    db = getattr(request.app.state, "db", None)
    if db is not None:
        try:
            rows = db.get_project_activity_summary(limit=20)
            total_projects = len(rows)
            for row in rows:
                rp = row["repo_path"]
                projects.append(ProjectActivity(
                    repo_path=rp,
                    repo_name=Path(rp).name if rp else "unknown",
                    gatekeeper_decisions=row.get("gatekeeper_decisions") or 0,
                    gatekeeper_allowed=row.get("gatekeeper_allowed") or 0,
                    commands_run=row.get("commands_run") or 0,
                    hook_executions=row.get("hook_executions") or 0,
                    last_activity=row.get("last_activity"),
                    first_seen=row.get("first_seen"),
                    unique_sessions=row.get("unique_sessions") or 0,
                ))
        except Exception:
            pass

    return InstallationsOverview(
        global_install=global_install,
        projects=projects,
        total_projects=total_projects,
    )


# --- Gatekeeper logs ---

@router.get("/logs/sessions")
async def get_gatekeeper_sessions(request: Request):
    """Session summaries for gatekeeper decisions."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    return db.list_gatekeeper_sessions(limit=50)


@router.get("/logs/gatekeeper")
async def get_gatekeeper_logs(
    request: Request,
    limit: int = 200,
    decision: Optional[str] = None,
    method: Optional[str] = None,
    session_id: Optional[str] = None,
):
    """Recent gatekeeper decisions from DB. Newest first."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    clamped = min(max(limit, 1), 1000)
    rows = db.list_gatekeeper_decisions(limit=clamped, session_id=session_id)

    if decision:
        rows = [r for r in rows if r.get("decision") == decision]
    if method:
        rows = [r for r in rows if r.get("method") == method]

    return rows


@router.delete("/logs/gatekeeper")
async def purge_gatekeeper_logs(
    request: Request,
    older_than_days: Optional[int] = None,
    session_id: Optional[str] = None,
):
    """Purge gatekeeper decisions by age or session."""
    from datetime import datetime, timedelta

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    before_iso = None
    if older_than_days is not None:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        before_iso = cutoff.isoformat()

    count = db.purge_gatekeeper_decisions(before_iso=before_iso, session_id=session_id)
    return {"purged": count}


@router.get("/logs/gatekeeper/export")
async def export_gatekeeper_logs(
    request: Request,
    session_id: Optional[str] = None,
    decision: Optional[str] = None,
):
    """Export gatekeeper decisions as downloadable JSON."""
    import json

    from fastapi.responses import Response

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    rows = db.export_gatekeeper_decisions(session_id=session_id, decision=decision)

    suffix = ""
    if session_id:
        suffix = f"-{session_id[:8]}"

    from datetime import datetime
    datestamp = datetime.utcnow().strftime("%Y%m%d")
    filename = f"gatekeeper-logs{suffix}-{datestamp}.json"

    return Response(
        content=json.dumps(rows, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Additional log endpoints (hooks, version checks) ---

@router.get("/logs/hooks")
async def list_hook_logs(
    request: Request,
    limit: int = 200,
    hook_name: Optional[str] = None,
):
    """List hook execution logs."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"logs": []}
    limit = max(1, min(500, limit))
    return {"logs": db.list_hook_executions(limit=limit, hook_name=hook_name)}


@router.get("/logs/version-checks")
async def list_version_check_logs(
    request: Request,
    limit: int = 100,
):
    """List version check logs."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"logs": []}
    limit = max(1, min(200, limit))
    return {"logs": db.list_version_checks(limit=limit)}


# --- Gatekeeper config (static routes BEFORE parameterized /{key}) ---

@router.get("/settings/gatekeeper")
async def get_gatekeeper_config(request: Request):
    """Gatekeeper LLM config with status info. Never exposes API key."""
    import json
    import os
    import shutil

    db = getattr(request.app.state, "db", None)

    model = "haiku"
    eval_method = "api_first"
    api_key_source = None

    if db is not None:
        model_raw = db.get_setting("gatekeeper.model")
        if model_raw:
            try:
                model = json.loads(model_raw)
            except (json.JSONDecodeError, TypeError):
                model = model_raw

        method_raw = db.get_setting("gatekeeper.eval_method")
        if method_raw:
            try:
                eval_method = json.loads(method_raw)
            except (json.JSONDecodeError, TypeError):
                eval_method = method_raw

        key_raw = db.get_setting("gatekeeper.api_key")
        if key_raw:
            try:
                key_val = json.loads(key_raw)
            except (json.JSONDecodeError, TypeError):
                key_val = key_raw
            if key_val:
                api_key_source = "db"

    if api_key_source is None and os.environ.get("ANTHROPIC_API_KEY"):
        api_key_source = "env"

    cli_available = shutil.which("claude") is not None

    return {
        "model": model,
        "eval_method": eval_method,
        "api_key_set": api_key_source is not None,
        "api_key_source": api_key_source,
        "cli_available": cli_available,
    }


@router.put("/settings/gatekeeper")
async def update_gatekeeper_config(body: GatekeeperConfigRequest, request: Request):
    """Update gatekeeper config with validated fields."""
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    db.set_setting("gatekeeper.model", json.dumps(body.model))
    db.set_setting("gatekeeper.eval_method", json.dumps(body.eval_method))

    if body.api_key is not None:
        if body.api_key == "":
            db.delete_setting("gatekeeper.api_key")
        else:
            db.set_setting("gatekeeper.api_key", json.dumps(body.api_key))

    return {"model": body.model, "eval_method": body.eval_method, "updated": True}


@router.post("/settings/gatekeeper/test-api-key")
async def test_gatekeeper_api_key(request: Request):
    """Test if the configured API key works with a minimal request."""
    import json
    import os

    db = getattr(request.app.state, "db", None)
    api_key = ""

    if db is not None:
        key_raw = db.get_setting("gatekeeper.api_key")
        if key_raw:
            try:
                api_key = json.loads(key_raw)
            except (json.JSONDecodeError, TypeError):
                api_key = key_raw

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        return {"success": False, "error": "No API key configured (check DB or ANTHROPIC_API_KEY env var)"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        return {"success": True, "response": response.content[0].text.strip()[:20]}
    except ImportError:
        return {"success": False, "error": "anthropic SDK not installed (pip install anthropic)"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


# --- Gatekeeper prompt ---

@router.get("/settings/gatekeeper/prompt")
async def get_gatekeeper_prompt():
    """Current gatekeeper prompt text and source. Never exposes internal paths."""
    from pathlib import Path

    from jacked.data.hooks.security_gatekeeper import (
        PROMPT_PATH,
        SECURITY_PROMPT,
    )

    source = "built-in"
    text = SECURITY_PROMPT
    if PROMPT_PATH.exists():
        try:
            custom = PROMPT_PATH.read_text(encoding="utf-8").strip()
            if custom:
                text = custom
                source = "custom"
        except Exception:
            pass

    return {"text": text, "source": source, "default_text": SECURITY_PROMPT}


class PromptUpdateRequest(BaseModel):
    text: str


@router.put("/settings/gatekeeper/prompt")
async def update_gatekeeper_prompt(body: PromptUpdateRequest):
    """Save a custom gatekeeper prompt. Validates required placeholders."""
    from jacked.data.hooks.security_gatekeeper import PROMPT_PATH

    required = {"{command}", "{cwd}", "{file_context}"}
    missing = [p for p in sorted(required) if p not in body.text]
    if missing:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "message": f"Missing required placeholders: {', '.join(missing)}",
                    "code": "MISSING_PLACEHOLDERS",
                    "missing": missing,
                }
            },
        )

    PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_PATH.write_text(body.text, encoding="utf-8")
    return {"source": "custom", "updated": True}


@router.delete("/settings/gatekeeper/prompt")
async def delete_gatekeeper_prompt():
    """Delete custom prompt file, reverting to built-in."""
    from jacked.data.hooks.security_gatekeeper import PROMPT_PATH

    if PROMPT_PATH.exists():
        PROMPT_PATH.unlink()
        return {"deleted": True, "source": "built-in"}
    return {"deleted": False, "source": "built-in"}


# --- Gatekeeper path safety config ---

@router.get("/settings/gatekeeper/path-safety")
async def get_path_safety_config(request: Request):
    """Path safety config + available rules metadata."""
    import json

    from jacked.data.hooks.security_gatekeeper import get_path_safety_rules_metadata

    db = getattr(request.app.state, "db", None)

    config = {"enabled": True, "allowed_paths": [], "disabled_patterns": []}

    if db is not None:
        raw = db.get_setting("gatekeeper.path_safety")
        if raw:
            try:
                config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

    return {
        "enabled": config.get("enabled", True),
        "allowed_paths": config.get("allowed_paths", []),
        "disabled_patterns": config.get("disabled_patterns", []),
        "available_rules": get_path_safety_rules_metadata(),
    }


@router.put("/settings/gatekeeper/path-safety")
async def update_path_safety_config(body: PathSafetyConfigRequest, request: Request):
    """Update path safety config with validation."""
    import json

    from jacked.data.hooks.security_gatekeeper import (
        SENSITIVE_DIR_RULES,
        SENSITIVE_FILE_RULES,
    )

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    # Validate allowed_paths
    if len(body.allowed_paths) > 20:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Maximum 20 allowed paths", "code": "TOO_MANY_PATHS"}},
        )
    for p in body.allowed_paths:
        if len(p) > 500:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": f"Path too long (max 500 chars): {p[:50]}...", "code": "PATH_TOO_LONG"}},
            )
        # Reject root-level paths that would disable the entire project boundary
        normalized = p.replace("\\", "/").rstrip("/")
        if normalized in ("", "/") or (len(normalized) == 2 and normalized[1] == ":"):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": f"Root paths not allowed (would disable project boundary): {p}", "code": "ROOT_PATH_REJECTED"}},
            )

    # Validate disabled_patterns — must be known rule keys
    valid_keys = set(SENSITIVE_FILE_RULES.keys()) | set(SENSITIVE_DIR_RULES.keys())
    invalid = [k for k in body.disabled_patterns if k not in valid_keys]
    if invalid:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": f"Unknown pattern keys: {', '.join(invalid)}", "code": "INVALID_PATTERN_KEY"}},
        )

    config = {
        "enabled": body.enabled,
        "allowed_paths": body.allowed_paths,
        "disabled_patterns": body.disabled_patterns,
    }
    db.set_setting("gatekeeper.path_safety", json.dumps(config))

    return {"updated": True, **config}


# --- Generic settings (parameterized routes AFTER static ones) ---

@router.get("/settings", response_model=list[SettingResponse])
async def get_settings(request: Request):
    """All settings as key/value pairs. Filters out gatekeeper.api_key."""
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    rows = db.list_settings()
    results = []
    for row in rows:
        r = dict(row)
        if r.get("key") == "gatekeeper.api_key":
            continue
        val = r.get("value")
        if isinstance(val, str):
            try:
                r["value"] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(SettingResponse(**r))
    return results


@router.put("/settings/{key}")
async def update_setting(key: str, body: SettingUpdateRequest, request: Request):
    """Update a setting."""
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    value_str = json.dumps(body.value)
    db.set_setting(key, value_str)
    return {"key": key, "value": body.value, "updated": True}


@router.delete("/settings/{key}")
async def delete_setting(key: str, request: Request):
    """Delete a setting by key."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    deleted = db.delete_setting(key)
    if not deleted:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": f"Setting '{key}' not found", "code": "NOT_FOUND"}},
        )
    return {"key": key, "deleted": True}
