"""System routes — health, version, installations, settings."""

import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jacked.api.mutation_gate import denied_response, mutation_decision
from jacked.api.project_paths import first_outside_project, outside_project_response
from jacked.api.remote_access import REASON_OUTSIDE_SCOPE, REASON_REMOTE_ACCESS_OFF
from jacked.api.routes.settings_swap import SWAP_SETTING_KEYS
from jacked.api.upgrade_steps import UpgradeStepError, _drain, _stop_child, _upgrade_run_cmd  # noqa: F401
from jacked.findbin import find_bin
from jacked.install_summary import last_install_path
from jacked.memory.vault import atomic_write_text
from jacked.public_errors import describe, public_message
from jacked.service import update_status as _update_status_mod
from jacked.web.database import SELECTED_ACCOUNT_KEYS, Database

logger = logging.getLogger(__name__)

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
    update_status_file: Optional[str] = None


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
    env_path: Optional[str] = None
    last_scanned_at: Optional[str] = None
    created_at: Optional[str] = None


class SettingResponse(BaseModel):
    key: str
    value: Any
    updated_at: Optional[str] = None


class SettingUpdateRequest(BaseModel):
    value: Any


class ProjectActivity(BaseModel):
    repo_path: str
    repo_name: str
    commands_run: int = 0
    hook_executions: int = 0
    last_activity: Optional[str] = None
    first_seen: Optional[str] = None
    unique_sessions: int = 0
    has_guardrails: bool = False
    guardrails_file: Optional[str] = None
    has_lint_hook: bool = False
    detected_language: Optional[str] = None
    env_path: Optional[str] = None
    has_lessons: bool = False
    lessons_count: int = 0


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
    skills: list[InstalledComponent] = []


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


@router.get("/admin/debug/identity-watcher")
async def debug_identity_watcher(request: Request):
    """Report the Claude config identity watcher and the identity coverage.

    Diagnostic-only, values-free: account row ids, reasons, durations and
    counts. It shows when the watcher cannot repair a config (owner not
    proven, breaker tripped, CLAUDE_CONFIG_DIR set) and how many Claude
    accounts still have no stored profile.
    """
    from jacked.api.session_observer import IDENTITY_WATCHER

    db = getattr(request.app.state, "db", None)
    coverage = None
    if db is not None:
        try:
            coverage = await asyncio.to_thread(db.claude_identity_coverage)
        except Exception as e:  # noqa: BLE001 - diagnostics must still answer
            coverage = {"error": type(e).__name__}
    return {"watcher": IDENTITY_WATCHER.status(), "coverage": coverage}


@router.get("/admin/debug/tasks")
async def debug_tasks(request: Request):
    """Dump every asyncio task on the running event loop.

    Diagnostic-only — returns each task's name, done state, and the
    file:line of its current frame so a wedged task can be pinpointed
    without attaching a debugger. The dashboard binds to 127.0.0.1, so
    no auth is layered here.
    """
    import time as _time
    out = []
    for t in asyncio.all_tasks():
        coro = t.get_coro()
        frame = getattr(coro, "cr_frame", None)
        location = None
        if frame is not None:
            location = (
                f"{frame.f_code.co_filename}:{frame.f_lineno} "
                f"in {frame.f_code.co_name}"
            )
        try:
            cancelled = t.cancelled()
        except Exception:
            cancelled = None
        out.append({
            "name": t.get_name(),
            "done": t.done(),
            "cancelled": cancelled,
            "coro_name": getattr(coro, "__qualname__", repr(coro)),
            "location": location,
        })

    # Heartbeat snapshot for the active poll loop — pairs with the task
    # list so the caller can correlate "is the task object alive" with
    # "is the task body actually ticking".
    last_tick = getattr(request.app.state, "active_poll_last_tick_at", None)
    heartbeat = {
        "active_poll_last_tick_at_monotonic": last_tick,
        "active_poll_heartbeat_age_seconds": (
            _time.monotonic() - last_tick if last_tick is not None else None
        ),
    }
    return {"count": len(out), "tasks": out, "heartbeat": heartbeat}


def _version_response(result: dict | None) -> "VersionResponse":
    """Build VersionResponse from check_version_cached result."""
    from datetime import datetime, timezone

    from jacked import __version__

    if result is None:
        return VersionResponse(
            current=__version__,
            update_status_file=str(_update_status_mod.UPDATE_STATUS_FILE),
        )

    checked_iso = None
    next_iso = None
    if result.get("checked_at"):
        checked_iso = datetime.fromtimestamp(
            result["checked_at"], tz=timezone.utc
        ).isoformat()
    if result.get("next_check_at"):
        next_iso = datetime.fromtimestamp(
            result["next_check_at"], tz=timezone.utc
        ).isoformat()

    return VersionResponse(
        current=__version__,
        latest=result["latest"],
        outdated=result["outdated"],
        ahead=result.get("ahead", False),
        checked_at=checked_iso,
        next_check_at=next_iso,
        update_status_file=str(_update_status_mod.UPDATE_STATUS_FILE),
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


class ProjectInitRequest(BaseModel):
    repo_path: str
    language: Optional[Literal["python", "node", "rust", "go"]] = None
    force: bool = False


def _validate_project_path(repo_path: str, request: Request) -> Optional[JSONResponse]:
    """Validate repo_path for init endpoints: exists, has .git, is known."""
    from pathlib import Path

    p = Path(repo_path)
    if not p.is_dir():
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "message": f"Not a directory: {repo_path}",
                    "code": "NOT_DIRECTORY",
                }
            },
        )
    if not (p / ".git").is_dir():
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "message": f"No .git directory in: {repo_path}",
                    "code": "NOT_GIT_REPO",
                }
            },
        )
    # Verify project is known to jacked (has activity in DB)
    db = getattr(request.app.state, "db", None)
    if db is not None:
        try:
            rows = db.get_project_activity_summary(limit=100)
            known_paths = {r["repo_path"] for r in rows if r.get("repo_path")}
            normalized = str(p).replace("\\", "/")
            if normalized not in known_paths and str(p) not in known_paths:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={
                        "error": {
                            "message": f"Unknown project: {repo_path}. Must have jacked activity.",
                            "code": "UNKNOWN_PROJECT",
                        }
                    },
                )
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": {
                        "message": "Database unavailable for project verification",
                        "code": "DB_ERROR",
                    }
                },
            )
    return None


@router.get("/update/status")
def get_update_status():
    """Return the current update-status JSON + its mtime.

    Used by /update.html (polled every 1s). mtime_iso is server-reported
    so the client-side stuck-detection doesn't reset on repeated stale reads.
    Returns {"status": null, "mtime_iso": null} when no update is in flight.
    A plain def (threadpool): the liveness probe can read the process table.
    """
    data, mtime_iso = _update_status_mod.read_status_with_mtime(
        _update_status_mod.UPDATE_STATUS_FILE,
    )
    # True / False / None (no pid, or the probe could not tell). The page
    # treats an unchanged record as abandoned only when this is not True,
    # the same rule update_status.in_progress_is_stale applies.
    alive = _update_status_mod.updater_liveness(data) if data else None
    return {"status": data, "mtime_iso": mtime_iso, "updater_alive": alive}


@router.get("/install/summary")
async def get_install_summary():
    """Return the last-install change record + its mtime.

    Used by the dashboard "what changed" panel. The record is written by
    ``jacked install`` to ``.claude/jacked-last-install.json`` under jacked's
    home (``$JACKED_HOME``-aware, see ``last_install_path``). Never raises:
    a missing or corrupt file yields {"summary": None, "mtime_iso": None}.
    """
    p = last_install_path()
    try:
        if not p.exists():
            return {"summary": None, "mtime_iso": None}
        summary = json.loads(p.read_text(encoding="utf-8"))
        mtime_iso = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        return {"summary": summary, "mtime_iso": mtime_iso}
    except (OSError, json.JSONDecodeError):
        return {"summary": None, "mtime_iso": None}


@router.post("/project/guardrails-init")
def project_guardrails_init(body: ProjectInitRequest, request: Request):
    """Create JACKED_GUARDRAILS.md in a project from templates."""
    error = _validate_project_path(body.repo_path, request)
    if error:
        return error

    from jacked.guardrails import create_guardrails

    result = create_guardrails(body.repo_path, language=body.language, force=body.force)
    return result


@router.post("/project/lint-hook-init")
def project_lint_hook_init(body: ProjectInitRequest, request: Request):
    """Install a pre-push lint hook in a project's .git/hooks/."""
    error = _validate_project_path(body.repo_path, request)
    if error:
        return error

    from jacked.guardrails import install_hook

    result = install_hook(body.repo_path, language=body.language, force=body.force)
    return result


# --- Project Env endpoints ---


class EnvUpdateRequest(BaseModel):
    repo_path: str
    env_path: str


@router.get("/project/env")
def get_project_env(repo_path: str, request: Request):
    """Get the configured Python env for a project.

    >>> # GET /api/project/env?repo_path=/some/repo
    """
    from pathlib import Path

    error = _validate_project_path(repo_path, request)
    if error:
        return error

    env_path = None
    source = None

    # Read from .git/jacked/env
    env_file = Path(repo_path) / ".git" / "jacked" / "env"
    if env_file.exists():
        try:
            env_path = env_file.read_text(encoding="utf-8").strip()
            source = "file"
        except Exception:
            pass

    # Also check DB
    db = getattr(request.app.state, "db", None)
    if db and not env_path:
        try:
            inst = db.get_installation_by_repo(repo_path)
            if inst and inst.get("env_path"):
                env_path = inst["env_path"]
                source = "db"
        except Exception:
            pass

    return {"env_path": env_path, "source": source}


@router.put("/project/env")
def update_project_env(body: EnvUpdateRequest, request: Request):
    """Manually set the Python env path for a project.

    >>> # PUT /api/project/env  {"repo_path": "...", "env_path": "..."}
    """
    from jacked.cli import _validate_env_path, _write_project_env

    error = _validate_project_path(body.repo_path, request)
    if error:
        return error

    # Validate the env path
    validation_error = _validate_env_path(body.env_path)
    if validation_error:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {"message": validation_error, "code": "INVALID_ENV_PATH"}
            },
        )

    # Write to .git/jacked/env
    _write_project_env(body.repo_path, body.env_path)

    # Write to DB
    db = getattr(request.app.state, "db", None)
    if db:
        try:
            db.update_installation_env(body.repo_path, body.env_path)
        except Exception:
            pass

    return {"env_path": body.env_path, "source": "manual"}


@router.post("/project/env/detect")
def detect_project_env(body: ProjectInitRequest, request: Request):
    """Auto-detect and store the Python env for a project.

    >>> # POST /api/project/env/detect  {"repo_path": "..."}
    """
    from jacked.cli import _detect_project_env, _validate_env_path, _write_project_env

    error = _validate_project_path(body.repo_path, request)
    if error:
        return error

    env_path = _detect_project_env()
    if not env_path:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "message": "No Python env detected",
                    "code": "NO_ENV_DETECTED",
                }
            },
        )

    validation_error = _validate_env_path(env_path)
    if validation_error:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {"message": validation_error, "code": "INVALID_ENV_PATH"}
            },
        )

    _write_project_env(body.repo_path, env_path)

    db = getattr(request.app.state, "db", None)
    if db:
        try:
            db.update_installation_env(body.repo_path, env_path)
        except Exception:
            pass

    return {"env_path": env_path, "source": "auto"}


class LessonItem(BaseModel):
    index: int
    strike: int = 1
    text: str

    model_config = {"str_max_length": 2000}


class LessonsUpdateRequest(BaseModel):
    repo_path: str
    lessons: list[LessonItem]


@router.get("/project/lessons")
def get_project_lessons(repo_path: str, request: Request):
    """Read and parse lessons.md for a project."""
    from pathlib import Path
    import re

    error = _validate_project_path(repo_path, request)
    if error:
        return error
    # A repository can ship lessons.md as a link to a private file.
    if first_outside_project(repo_path, "lessons.md"):
        return outside_project_response("lessons.md")

    lessons_path = Path(repo_path) / "lessons.md"
    if not lessons_path.exists():
        return {"exists": False, "lessons": []}

    try:
        text = lessons_path.read_text(encoding="utf-8")
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"message": "Failed to read lessons.md", "code": "READ_ERROR"}
            },
        )

    lessons = []
    pattern = re.compile(r"^- \[(\d+)x\]\s+(.+)$")
    for idx, line in enumerate(text.splitlines()):
        line = line.strip()
        m = pattern.match(line)
        if m:
            lessons.append(
                {"index": idx, "strike": int(m.group(1)), "text": m.group(2)}
            )
        elif line.startswith("- "):
            lessons.append({"index": idx, "strike": 0, "text": line[2:]})

    return {"exists": True, "lessons": lessons}


@router.put("/project/lessons")
def update_project_lessons(body: LessonsUpdateRequest, request: Request):
    """Write updated lessons back to lessons.md."""
    from pathlib import Path

    if len(body.lessons) > 200:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {"message": "Too many lessons (max 200)", "code": "TOO_MANY"}
            },
        )

    error = _validate_project_path(body.repo_path, request)
    if error:
        return error
    # The save keeps the file's header lines, so a lessons.md that links to
    # a private file would copy that file's text into the project.
    if first_outside_project(body.repo_path, "lessons.md"):
        return outside_project_response("lessons.md")

    lessons_path = Path(body.repo_path) / "lessons.md"

    # Preserve any header content (lines before the first lesson bullet)
    header_lines = []
    if lessons_path.exists():
        try:
            existing = lessons_path.read_text(encoding="utf-8")
            for line in existing.splitlines():
                if line.strip().startswith("- "):
                    break
                header_lines.append(line)
        except Exception:
            pass

    # Default header if file is new or had none
    if not header_lines:
        header_lines = ["# Lessons", ""]

    # Build lesson lines, filtering out empty text and stripping newlines
    lesson_lines = []
    for lesson in body.lessons:
        text = lesson.text.replace("\n", " ").replace("\r", " ").strip()
        if not text:
            continue
        if lesson.strike > 0:
            lesson_lines.append(f"- [{lesson.strike}x] {text}")
        else:
            lesson_lines.append(f"- {text}")

    content = "\n".join(header_lines + lesson_lines) + "\n"

    # The link check runs again right before the write: this handler runs in
    # the threadpool, and a link made after the check above would move the
    # write outside the project.
    if first_outside_project(body.repo_path, "lessons.md"):
        return outside_project_response("lessons.md")

    try:
        # Two tabs can save at once (threadpool): a whole-file replace keeps
        # either version intact. A new lessons.md is a repo document, not private.
        atomic_write_text(lessons_path, content, new_file_mode=0o644)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "message": "Failed to write lessons.md",
                    "code": "WRITE_ERROR",
                }
            },
        )

    return {"saved": True, "count": len(lesson_lines)}


@router.get("/installations", response_model=list[InstallationResponse])
def list_installations(request: Request):
    """List repos where jacked is installed."""
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}
            },
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
def installations_overview(request: Request):
    """Global install state + per-project activity summary."""
    from pathlib import Path

    from jacked import __version__
    from jacked.api.routes.features import (
        CLAUDE_DIR,
        _detect_hook_installed,
        _detect_rules_status,
        _get_valid_agent_names,
        _get_valid_command_names,
        _get_valid_skill_names,
        _name_to_display,
        _read_settings_json,
    )
    from jacked.memory.settings_io import SettingsUnreadableError

    # Read-only status readout: a corrupt settings.json degrades to empty
    # detection rather than 500ing the whole installed-components view.
    try:
        settings = _read_settings_json()
    except SettingsUnreadableError:
        settings = {}

    # Agents
    agent_names = _get_valid_agent_names()
    agents_dst = CLAUDE_DIR / "agents"
    agents = []
    for name in agent_names:
        installed = (agents_dst / f"{name}.md").exists()
        agents.append(
            InstalledComponent(
                name=name, display_name=_name_to_display(name), installed=installed
            )
        )

    # Commands
    command_names = _get_valid_command_names()
    commands_dst = CLAUDE_DIR / "commands"
    commands = []
    for name in command_names:
        installed = (commands_dst / f"{name}.md").exists()
        commands.append(
            InstalledComponent(
                name=name, display_name=_name_to_display(name), installed=installed
            )
        )

    # Hooks (gatekeeper + session indexing were retired in 0.70.0)
    hooks = [
        InstalledComponent(
            name="sounds",
            display_name="Sounds",
            installed=_detect_hook_installed(settings, "sounds"),
        ),
    ]

    # Knowledge
    rules_status = _detect_rules_status()
    ref_installed = (CLAUDE_DIR / "jacked-reference.md").exists()
    knowledge = [
        InstalledComponent(
            name="rules",
            display_name="Rules",
            installed=rules_status.get("installed", False),
        ),
        InstalledComponent(
            name="reference", display_name="Reference", installed=ref_installed
        ),
    ]

    # Skills (dynamic — one per SKILL.md in package)
    skills = [
        InstalledComponent(
            name=f"skill_{skill_name}",
            display_name=f"/{skill_name}",
            installed=(CLAUDE_DIR / "skills" / skill_name / "SKILL.md").exists(),
        )
        for skill_name in _get_valid_skill_names()
    ]

    global_install = GlobalInstallation(
        version=__version__,
        agents=agents,
        commands=commands,
        hooks=hooks,
        knowledge=knowledge,
        skills=skills,
    )

    # Project activity from DB
    projects: list[ProjectActivity] = []
    total_projects = 0
    db = getattr(request.app.state, "db", None)
    if db is not None:
        try:
            rows = db.get_project_activity_summary(limit=20)
            total_projects = len(rows)
            from jacked.guardrails import check_project_setup

            for row in rows:
                rp = row["repo_path"]
                setup = check_project_setup(rp) if rp else {}
                projects.append(
                    ProjectActivity(
                        repo_path=rp,
                        repo_name=Path(rp).name if rp else "unknown",
                        commands_run=row.get("commands_run") or 0,
                        hook_executions=row.get("hook_executions") or 0,
                        last_activity=row.get("last_activity"),
                        first_seen=row.get("first_seen"),
                        unique_sessions=row.get("unique_sessions") or 0,
                        has_guardrails=setup.get("has_guardrails", False),
                        guardrails_file=setup.get("guardrails_file"),
                        has_lint_hook=setup.get("has_lint_hook", False),
                        detected_language=setup.get("detected_language"),
                        env_path=setup.get("env_path"),
                        has_lessons=setup.get("has_lessons", False),
                        lessons_count=setup.get("lessons_count", 0),
                    )
                )
        except Exception:
            logger.exception("Failed to load project activity")

    return InstallationsOverview(
        global_install=global_install,
        projects=projects,
        total_projects=total_projects,
    )


# --- Self-upgrade ---

_upgrade_lock = asyncio.Lock()
# The scheduled upgrade tasks. asyncio keeps only weak references to tasks,
# so the module holds the strong ones until each finishes (a set: two
# requests can be scheduled on one loop tick, and the second drops itself).
_upgrade_tasks: set[asyncio.Task] = set()


def reset_locks() -> None:
    """Rebind to the current event loop — see routes.auth.reset_locks."""
    global _upgrade_lock
    _upgrade_lock = asyncio.Lock()
    _upgrade_tasks.clear()


async def cancel_running_upgrade() -> None:
    """Cancel and await any scheduled upgrade task (service shutdown).

    Called first in the lifespan shutdown, while the log handlers are still
    attached, so the installer child is terminated and its outcome logged
    before the database closes and the loop is torn down.
    """
    for task in list(_upgrade_tasks):
        if task.done():
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must continue
            logger.warning("Upgrade task ended with %s during shutdown", type(exc).__name__)


async def _upgrade_broadcast(ws_registry, event: str, payload: dict) -> None:
    if ws_registry:
        try:
            await ws_registry.broadcast(event, payload)
        except Exception:
            logger.exception("Failed to broadcast %s", event)


# Reason-keyed denial copy for a refused remote upgrade. The two denials
# have different fixes, so they get different sentences. No em-dashes
# (user-facing). The codes are the policy reasons themselves.
_UPGRADE_DENIED_MESSAGES = {
    REASON_REMOTE_ACCESS_OFF: (
        "Upgrades from this browser are off. Turn on remote access under "
        "Settings > Advanced on the host machine."
    ),
    REASON_OUTSIDE_SCOPE: (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}
_SWITCH_IN_PROGRESS = "Account switch in progress. Try again when it finishes."
_LEASE_UNAVAILABLE = (
    "The credential switch lease is not available. Check the service log on "
    "the host machine."
)


def _switch_lease():
    """The credential switch lease, on the same home every activation uses."""
    from jacked.credentials.runtime import switch_lease_for

    return switch_lease_for(Path.home())


def _switch_lease_refusal() -> str | None:
    """Why the switch lease cannot be taken right now, or None when it can.

    A non-blocking probe: acquire and release at once. The upgrade itself
    takes the lease again for its whole run, so this only turns a certain
    refusal into a clean answer before anything is scheduled. ``"held"``
    means a switch is in progress; ``"unusable"`` means the lock file could
    not be opened, which refuses fail-closed and needs the service log.
    """
    lease = _switch_lease()
    with lease.acquire() as held:
        return None if held else (lease.refusal or "held")


def _refusal_message(refusal: str) -> str:
    return _LEASE_UNAVAILABLE if refusal == "unusable" else _SWITCH_IN_PROGRESS


async def _do_upgrade(ws_registry, client_host: str | None = None) -> None:
    if _upgrade_lock.locked():
        # The route checked the lock before scheduling, but two requests can
        # both pass that check on one loop tick; the second must not queue
        # behind the first and run an upgrade nobody asked for.
        logger.info("Upgrade already in progress; a second request was dropped")
        return
    async with _upgrade_lock:
        # Hold the credential switch lease across the install and the exec:
        # a switch that starts mid-restart would leave its journal row
        # pending and its stores half-written. Python opens the lock file
        # close-on-exec, so the new process starts with the lease released.
        lease = _switch_lease()
        with lease.acquire() as held:
            if not held:
                refusal = lease.refusal or "held"
                logger.warning(
                    "Upgrade refused for client %s: the credential switch lease is %s",
                    client_host,
                    refusal,
                )
                await _upgrade_broadcast(
                    ws_registry, "upgrade_failed", {"error": _refusal_message(refusal)}
                )
                return
            logger.info("Upgrade started (requested by client %s)", client_host)
            await _run_upgrade_steps(ws_registry)


async def _run_upgrade_steps(ws_registry) -> None:
    """Upgrade, reinstall, then replace this process. Caller holds both leases."""
    stage = "upgrade"
    try:
        await _upgrade_broadcast(ws_registry, "upgrade_started", {})

        uv_bin = find_bin("uv")
        if not uv_bin:
            raise UpgradeStepError("uv not found on PATH. Cannot upgrade.")
        await _upgrade_broadcast(ws_registry, "upgrade_progress",
                                 {"step": "upgrade", "message": "Upgrading claude-jacked\u2026"})
        await _upgrade_run_cmd([uv_bin, "tool", "upgrade", "claude-jacked"])

        stage = "reinstall"
        jacked_bin = find_bin("jacked")
        if not jacked_bin:
            raise UpgradeStepError("jacked not found on PATH. Cannot reinstall.")
        await _upgrade_broadcast(ws_registry, "upgrade_progress",
                                 {"step": "reinstall", "message": "Reinstalling files\u2026"})
        await _upgrade_run_cmd([jacked_bin, "install", "--force"])

        stage = "restart"
        await _upgrade_broadcast(ws_registry, "upgrade_complete",
                                 {"message": "Restarting server\u2026"})
        await asyncio.sleep(1.5)  # let WS message flush
        # Replace the running process with a fresh start (picks up the new
        # venv). Use the shared exec-target resolver, not a raw
        # os.execv(sys.argv[0], ...): under `python -m jacked` / `pythonw
        # -m jacked` argv[0] is __main__.py (no exec bit) and a raw execv
        # raises OSError, stranding the dashboard on the old code.
        from jacked.service.restart import _exec_target

        prog, argv = _exec_target()
        os.execv(prog, argv)

    except Exception as exc:
        # Durable, values-free: the stage and the exception type. The dialog gets
        # an UpgradeStepError's report, else fixed text: str(exc) can name a path.
        logger.warning("Upgrade failed at stage %s: %s", stage, describe(exc))
        fixed = f"Upgrade failed at the {stage} step. Check the jacked log on the host machine for the cause."
        await _upgrade_broadcast(ws_registry, "upgrade_failed", {"error": public_message(exc, fixed)})


@router.post("/upgrade")
async def system_upgrade(request: Request):
    """Upgrade claude-jacked, reinstall files, and restart the server.

    The same remote-access policy that gates credential switches gates this
    install-and-restart of the host: loopback always may; a remote client may
    only while remote access is on and its address is inside the scope. The
    gate runs before any lock check, so a refused remote call can never
    wedge a later local upgrade behind a 409. Then the running-upgrade lock,
    then the credential switch lease, then the task.
    """
    allowed, reason, host = await asyncio.to_thread(
        mutation_decision, request, audit="upgrade"
    )
    if not allowed:
        return denied_response(reason, _UPGRADE_DENIED_MESSAGES, code=reason.upper())
    # The running upgrade holds the switch lease itself, so this check comes
    # before the lease probe: a second request during an upgrade is told
    # about the upgrade, not about an account switch.
    if _upgrade_lock.locked():
        return JSONResponse(status_code=409, content={"detail": "Upgrade already in progress"}, headers={"Cache-Control": "no-store"})
    refusal = await asyncio.to_thread(_switch_lease_refusal)
    if refusal == "unusable":
        # Fail closed, and say so where an operator can act on it: the lease
        # module already logged why the lock file could not be opened.
        logger.warning(
            "Upgrade refused for client %s: the credential switch lease is unusable",
            host,
        )
        return JSONResponse(status_code=503, content={"detail": _LEASE_UNAVAILABLE}, headers={"Cache-Control": "no-store"})
    if refusal is not None:
        # Attributable without secrets: the client address and the reason.
        logger.info(
            "Upgrade refused for client %s: an account switch holds the lease", host
        )
        return JSONResponse(status_code=409, content={"detail": _SWITCH_IN_PROGRESS}, headers={"Cache-Control": "no-store"})
    ws_registry = getattr(request.app.state, "ws_registry", None)
    task = asyncio.create_task(_do_upgrade(ws_registry, host))
    _upgrade_tasks.add(task)
    task.add_done_callback(_upgrade_tasks.discard)
    return {"status": "started"}




@router.get("/logs/hooks")
def list_hook_logs(
    request: Request,
    limit: int = 200,
    offset: int = 0,
    hook_name: Optional[str] = None,
):
    """List hook execution logs with pagination."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"logs": [], "total": 0, "limit": limit, "offset": offset}
    clamped_limit = max(1, min(500, limit))
    clamped_offset = max(offset, 0)
    result = db.list_hook_executions(
        limit=clamped_limit, offset=clamped_offset, hook_name=hook_name
    )
    return {
        "logs": result["rows"],
        "total": result["total"],
        "limit": clamped_limit,
        "offset": clamped_offset,
    }


@router.get("/logs/version-checks")
def list_version_check_logs(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """List version check logs with pagination."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"logs": [], "total": 0, "limit": limit, "offset": offset}
    clamped_limit = max(1, min(200, limit))
    clamped_offset = max(offset, 0)
    result = db.list_version_checks(limit=clamped_limit, offset=clamped_offset)
    return {
        "logs": result["rows"],
        "total": result["total"],
        "limit": clamped_limit,
        "offset": clamped_offset,
    }




@router.get("/settings", response_model=list[SettingResponse])
def get_settings(request: Request):
    """All settings as key/value pairs.

    Filters out legacy gatekeeper.* rows — the feature was retired in 0.70.0
    but old DBs still carry its settings (including a possible stored API
    key, which must never be listed).
    """
    import json

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}
            },
        )

    rows = db.list_settings()
    results = []
    for row in rows:
        r = dict(row)
        if str(r.get("key", "")).startswith("gatekeeper."):
            continue
        val = r.get("value")
        if isinstance(val, str):
            try:
                r["value"] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(SettingResponse(**r))
    return results


_PROTECTED_SETTING_KEYS = {
    # Legacy secret from the retired gatekeeper — old DBs may still carry it;
    # never allow it to be rewritten (or listed) via the generic endpoint.
    "gatekeeper.api_key",
    # Every auto-swap and window keeper setting belongs to PUT
    # /api/settings/swap-settings, which validates them as one set (warning
    # below critical, active hours in order). Taken from that route's own key
    # list, so a new swap setting is protected the day it is added.
    *SWAP_SETTING_KEYS,
    # Bind host is a security decision — only the dedicated remote-access
    # endpoint (with its restart flow) may write these, never the generic PUT.
    "remote_access_enabled",
    "remote_access_scope",
    # The credential-selection pointers: the account that jacked selected for
    # new sessions, and the active account. The authority guard, the identity
    # watcher and the OAuth flow act on them, so only the switch path (with
    # its mutation gate, switch audit and rate limit) may move them.
    # _is_protected_setting adds the pointer of every other provider.
    *SELECTED_ACCOUNT_KEYS,
}
# The pointers of each provider other than Claude:
# Database.active_account_setting_key(provider) is the Claude key plus
# "_<provider>", and a desired pointer of another provider follows the same
# shape. Matching both stems protects the pointers of a provider that is
# added later, the day it is added.
_PROVIDER_POINTER_PREFIXES = tuple(key + "_" for key in SELECTED_ACCOUNT_KEYS)

# Reason-keyed copy for a refused remote change through the generic settings
# endpoint. The two denials have different fixes, so they get different
# sentences. No em-dashes (user-facing). The codes are the policy reasons.
_SETTINGS_DENIED_MESSAGES = {
    REASON_REMOTE_ACCESS_OFF: (
        "Changing settings from this browser is off. Turn on remote access "
        "under Settings > Advanced on the host machine."
    ),
    REASON_OUTSIDE_SCOPE: (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}


def _is_protected_setting(key: str) -> bool:
    """Return True when only a dedicated endpoint may write or delete ``key``.

    >>> _is_protected_setting("desired_account_id")
    True
    >>> _is_protected_setting(Database.active_account_setting_key("codex"))
    True
    >>> _is_protected_setting("desired_account_id_codex")
    True
    >>> _is_protected_setting("color_theme"), _is_protected_setting("active_account_ids_note")
    (False, False)
    """
    return key in _PROTECTED_SETTING_KEYS or key.startswith(_PROVIDER_POINTER_PREFIXES)


def _protected_key_response(key: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "message": f"Use the dedicated endpoint for '{key}'",
                "code": "PROTECTED_KEY",
            }
        },
    )


@router.put("/settings/{key}")
def update_setting(key: str, body: SettingUpdateRequest, request: Request):
    """Update a setting.

    The gate of an account switch runs first: loopback always may; a remote
    client may only while remote access is on and its address is inside the
    scope. Then a key that a dedicated endpoint owns, or a retired key, is
    refused (422).
    """
    import json

    allowed, reason, _host = mutation_decision(request, audit="setting")
    if not allowed:
        return denied_response(reason, _SETTINGS_DENIED_MESSAGES, code=reason.upper())
    if _is_protected_setting(key):
        return _protected_key_response(key)
    # A retired key is deleted when the database opens, and no code reads it,
    # so a write would only look like a setting that works. DELETE stays open:
    # it removes a row that an old jacked wrote after the open.
    if key in Database.RETIRED_SETTING_KEYS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "message": f"jacked no longer uses the setting '{key}'.",
                    "code": "RETIRED_KEY",
                }
            },
        )

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}
            },
        )

    value_str = json.dumps(body.value)
    db.set_setting(key, value_str)
    return {"key": key, "value": body.value, "updated": True}


@router.delete("/settings/{key}")
def delete_setting(key: str, request: Request):
    """Delete a setting by key.

    The same gate as the PUT runs first, then the protected-key check.
    """
    allowed, reason, _host = mutation_decision(request, audit="setting")
    if not allowed:
        return denied_response(reason, _SETTINGS_DENIED_MESSAGES, code=reason.upper())
    if _is_protected_setting(key):
        return _protected_key_response(key)

    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}
            },
        )

    deleted = db.delete_setting(key)
    if not deleted:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {"message": f"Setting '{key}' not found", "code": "NOT_FOUND"}
            },
        )
    return {"key": key, "deleted": True}
