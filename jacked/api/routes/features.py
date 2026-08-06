"""Feature detection and toggle routes — agents, commands, hooks, knowledge."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jacked.memory.settings_io import SettingsUnreadableError

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Constants ---

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
DATA_ROOT = Path(__file__).parent.parent.parent / "data"

# Markers
SOUND_MARKER = "# jacked-sound: "
RULES_START_PREFIX = "# jacked-behaviors-v"
RULES_END_MARKER = "# end-jacked-behaviors"

# Valid hook/knowledge names (allowlist)
VALID_HOOKS = {"sounds", "memory_vault", "statusline"}


def _get_valid_skill_names() -> list[str]:
    """List skill names from package source."""
    skills_dir = DATA_ROOT / "skills"
    if not skills_dir.exists():
        return []
    return [d.name for d in sorted(skills_dir.iterdir())
            if d.is_dir() and (d / "SKILL.md").exists()]


def _get_valid_knowledge_names() -> set[str]:
    """Dynamic allowlist of knowledge feature names."""
    base = {"rules", "reference"}
    for name in _get_valid_skill_names():
        base.add(f"skill_{name}")
    return base


# ---------------------------------------------------------------------------
# Claude Code settings constants — these control Claude Code itself, not jacked
# ---------------------------------------------------------------------------

# Env var toggles (on/off via value_on / removed from env section)
TOGGLEABLE_ENV_VARS = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": {
        "display_name": "Agent Teams (Swarms)",
        "description": "Multiple agents working in parallel on complex tasks",
        "section": "experimental",
    },
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": {
        "display_name": "Disable Auto Memory",
        "description": "Stop Claude from auto-writing to CLAUDE.md",
        "section": "experimental",
    },
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": {
        "display_name": "Prompt Suggestions",
        "description": "Show prompt suggestions when idle",
        "section": "experimental",
        "value_on": "true",
    },
    "DISABLE_PROMPT_CACHING": {
        "display_name": "Disable Prompt Caching",
        "description": "Turn off prompt caching (costs more, useful for debugging)",
        "section": "privacy",
    },
    "DISABLE_TELEMETRY": {
        "display_name": "Disable Telemetry",
        "description": "Opt out of Statsig usage tracking",
        "section": "privacy",
    },
    "DISABLE_ERROR_REPORTING": {
        "display_name": "Disable Error Reporting",
        "description": "Opt out of Sentry error reporting",
        "section": "privacy",
    },
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": {
        "display_name": "Disable Non-Essential Traffic",
        "description": "Block all non-essential network traffic",
        "section": "privacy",
    },
}

# Env vars with numeric values
NUMERIC_ENV_VARS = {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": {
        "display_name": "Max Output Tokens",
        "description": "Maximum response length (default: 32000, max: 64000)",
        "default": "32000",
        "min": 1000,
        "max": 64000,
        "section": "performance",
    },
    "MAX_THINKING_TOKENS": {
        "display_name": "Max Thinking Tokens",
        "description": "Extended thinking budget (default: 31999)",
        "default": "31999",
        "min": 1000,
        "max": 128000,
        "section": "performance",
    },
    "BASH_DEFAULT_TIMEOUT_MS": {
        "display_name": "Bash Timeout (ms)",
        "description": "Default timeout for bash commands (default: 120000 = 2min)",
        "default": "120000",
        "min": 5000,
        "max": 600000,
        "section": "performance",
    },
    "CLAUDE_CODE_AUTOCOMPACT_PCT_OVERRIDE": {
        "display_name": "Auto-Compact Threshold (%)",
        "description": "Context capacity % to trigger auto-compaction (default: ~80)",
        "default": "80",
        "min": 1,
        "max": 100,
        "section": "performance",
    },
}

# Direct settings.json keys (bool or simple values)
DIRECT_SETTINGS = {
    "alwaysThinkingEnabled": {
        "display_name": "Always Use Extended Thinking",
        "description": "Enable extended thinking by default for all prompts",
        "type": "bool",
        "default": False,
    },
    "showTurnDuration": {
        "display_name": "Show Turn Duration",
        "description": "Display how long each response took",
        "type": "bool",
        "default": True,
    },
    "spinnerTipsEnabled": {
        "display_name": "Spinner Tips",
        "description": "Show helpful tips during loading animations",
        "type": "bool",
        "default": True,
    },
    "cleanupPeriodDays": {
        "display_name": "Session Cleanup (days)",
        "description": "Delete inactive sessions older than this many days",
        "type": "number",
        "default": 30,
        "min": 1,
        "max": 365,
    },
}

VALID_PERMISSION_MODES = {"plan", "default", "bypassPermissions", "acceptEdits"}

# Lock for settings.json mutations (single-process, no external deps)
_settings_lock = asyncio.Lock()


def reset_locks() -> None:
    """Rebind to the current event loop — see routes.auth.reset_locks.

    ``_dcr_lock`` is defined further down (next to the DCR engine endpoints it
    guards) but must be rebound here too — one reset_locks per module is the
    contract enforced by tests/unit/api/test_reset_locks.py.
    """
    global _settings_lock, _dcr_lock
    _settings_lock = asyncio.Lock()
    _dcr_lock = asyncio.Lock()


# --- Pydantic models ---

class FeatureToggleRequest(BaseModel):
    enabled: bool


# --- Helpers ---

def _parse_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file.

    >>> import tempfile, os
    >>> p = Path(tempfile.mktemp(suffix='.md'))
    >>> _ = p.write_text('---\\nname: test-agent\\ndescription: "Does stuff"\\nmodel: haiku\\n---\\nBody.', encoding='utf-8')
    >>> fm = _parse_frontmatter(p)
    >>> fm['name'], fm['model']
    ('test-agent', 'haiku')
    >>> os.unlink(str(p))
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    result = {}
    for line in block.split("\n"):
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip('"').strip("'")
        # Truncate long descriptions
        if key.strip() == "description" and len(val) > 120:
            val = val[:117] + "..."
        result[key.strip()] = val
    return result


def _name_to_display(name: str) -> str:
    """Convert kebab-case filename to Title Case display name.

    >>> _name_to_display('double-check-reviewer')
    'Double Check Reviewer'
    >>> _name_to_display('dc')
    'Dc'
    """
    return " ".join(word.capitalize() for word in name.split("-"))


def _read_settings_json() -> dict:
    """Read ~/.claude/settings.json via the corruption-safe reader.

    A missing file returns ``{}`` (a fresh install). A PRESENT-but-unreadable file
    (a JSON typo, a partial write, a permissions hiccup) raises
    ``SettingsUnreadableError`` -- the old behavior of returning ``{}`` here was a
    latent wipe bug: the next read-modify-write would then CLOBBER every real
    hook, permission, and env var the user had. Mutation endpoints turn this into
    a 503 (``_settings_unreadable_response``); read-only consumers catch it and
    degrade to ``{}`` (surfacing ``settings_unreadable`` where the shape allows).
    """
    from jacked.memory.settings_io import read_settings

    return read_settings(SETTINGS_JSON)


def _settings_unreadable_response() -> JSONResponse:
    """The 503 a mutation endpoint returns when settings.json exists but can't be
    parsed: refuse to modify it rather than clobber the user's real config with a
    fresh file (mirrors the memory-vault toggle's refusal shape)."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {
            "message": "settings.json is unreadable; refusing to modify it",
            "code": "SETTINGS_UNREADABLE",
        }},
    )


def _write_settings_json(data: dict):
    """Write ~/.claude/settings.json atomically via a writer-unique temp + replace.

    Writer-unique (mkstemp) rather than a fixed ``.json.tmp``: the CLI, the
    dashboard, and hook processes can all write this same file, and a shared tmp
    path lets one process os.replace another's half-written temp away.
    """
    SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{SETTINGS_JSON.name}.", suffix=".tmp", dir=str(SETTINGS_JSON.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp_name, SETTINGS_JSON)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _get_valid_agent_names() -> list[str]:
    """List agent names from package source."""
    agents_dir = DATA_ROOT / "agents"
    if not agents_dir.exists():
        return []
    return [f.stem for f in sorted(agents_dir.glob("*.md"))]


def _get_valid_command_names() -> list[str]:
    """List command names from package source."""
    commands_dir = DATA_ROOT / "commands"
    if not commands_dir.exists():
        return []
    return [f.stem for f in sorted(commands_dir.glob("*.md"))]


def _validate_name(name: str) -> bool:
    """Reject path traversal attempts."""
    if not name:
        return False
    if any(c in name for c in ("/", "\\", "\0")):
        return False
    if ".." in name:
        return False
    return True


def _detect_hook_installed(settings: dict, hook_name: str) -> bool:
    """Check if a hook is installed in settings.json."""
    hooks = settings.get("hooks", {})

    if hook_name == "sounds":
        for hook_type in ("Notification", "Stop"):
            for entry in hooks.get(hook_type, []):
                if SOUND_MARKER in str(entry):
                    return True
        return False

    if hook_name == "memory_vault":
        # Installed when a memory-capture entry exists — reuse the installer's
        # own anchor logic so detection never re-implements the string math.
        # Read the SAME settings file the toggle writes: the memory engine
        # resolves its home via jacked_home() ($JACKED_HOME-aware), while this
        # module's SETTINGS_JSON is pinned to Path.home() — under a redirected
        # home those diverge and the checkbox would misreport its own toggle.
        from jacked.memory import hooks_config
        from jacked.memory import vault as memory_vault_mod

        mem_settings = settings
        mem_path = memory_vault_mod.jacked_home() / ".claude" / "settings.json"
        # Divert to the engine's file ONLY when SETTINGS_JSON still points at
        # the stock location ($JACKED_HOME redirected the engine elsewhere).
        # A caller/test that re-pins SETTINGS_JSON is authoritative — reading
        # past it leaks the real machine's settings into isolated contexts.
        stock_settings_json = Path.home() / ".claude" / "settings.json"
        if mem_path != SETTINGS_JSON and SETTINGS_JSON == stock_settings_json:
            try:
                loaded = json.loads(mem_path.read_text(encoding="utf-8"))
                mem_settings = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                mem_settings = {}
        return hooks_config.has_capture_entry(mem_settings)

    if hook_name == "statusline":
        # Same home-divergence rule as memory_vault: the toggle engine writes
        # through jacked_home(), so detection must read that same file when
        # $JACKED_HOME redirects it and SETTINGS_JSON is still stock.
        from jacked import statusline_setup

        sl_settings = settings
        sl_path = statusline_setup.jacked_home() / ".claude" / "settings.json"
        stock_settings_json = Path.home() / ".claude" / "settings.json"
        if sl_path != SETTINGS_JSON and SETTINGS_JSON == stock_settings_json:
            try:
                loaded = json.loads(sl_path.read_text(encoding="utf-8"))
                sl_settings = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                sl_settings = {}
        return statusline_setup.entry_state(sl_settings) == "ours"

    return False


def _memory_vault_health() -> dict | None:
    """The memory vault's health snapshot for the dashboard card, via
    ``vault.status()`` ($JACKED_HOME-aware). Cheap (one state read); a failure
    just omits the health object rather than breaking the feature list."""
    try:
        from jacked.memory import vault as memory_vault_mod

        st = memory_vault_mod.status()
        return {
            "last_capture": st.get("last_capture"),
            "last_capture_error": st.get("last_capture_error"),
            "last_recall": st.get("last_recall"),
            "last_sync_error": st.get("last_sync_error"),
            "retry_pending": st.get("retry_pending"),
            "drift_added": st.get("drift_added"),
            "drift_threshold": st.get("drift_threshold"),
            "capture_failures": st.get("capture_failures"),
        }
    except Exception:  # noqa: BLE001 -- health is best-effort; never break /features
        logger.debug("memory vault health read failed", exc_info=True)
        return None


def _detect_rules_status() -> dict:
    """Check behavioral rules installation status.

    Returns dict with 'installed' and optionally 'corrupt' keys.
    """
    if not CLAUDE_MD.exists():
        return {"installed": False}

    try:
        content = CLAUDE_MD.read_text(encoding="utf-8")
    except OSError:
        return {"installed": False}

    has_start = RULES_START_PREFIX in content
    has_end = RULES_END_MARKER in content

    if has_start and has_end:
        return {"installed": True}
    if has_start != has_end:
        return {"installed": False, "corrupt": True}
    return {"installed": False}


# --- GET /api/features ---

@router.get("/features")
async def list_features():
    """Full feature manifest with installed status."""
    # Read-only consumer: a corrupt settings.json must not 500 the whole feature
    # list. Degrade to {} for detection and flag it so the client can warn.
    try:
        settings = _read_settings_json()
        settings_unreadable = False
    except SettingsUnreadableError:
        settings = {}
        settings_unreadable = True

    # Agents
    agents = []
    agents_src = DATA_ROOT / "agents"
    for name in _get_valid_agent_names():
        src = agents_src / f"{name}.md"
        installed_path = CLAUDE_DIR / "agents" / f"{name}.md"
        fm = _parse_frontmatter(src)
        agents.append({
            "name": name,
            "display_name": fm.get("name", _name_to_display(name)),
            "description": fm.get("description", ""),
            "installed": installed_path.exists(),
            "source_available": src.exists(),
            "model": fm.get("model"),
        })

    # Commands
    commands = []
    commands_src = DATA_ROOT / "commands"
    for name in _get_valid_command_names():
        src = commands_src / f"{name}.md"
        installed_path = CLAUDE_DIR / "commands" / f"{name}.md"
        fm = _parse_frontmatter(src)
        commands.append({
            "name": name,
            "display_name": f"/{name}",
            "description": fm.get("description", ""),
            "installed": installed_path.exists(),
            "source_available": src.exists(),
        })

    # Hooks
    hooks = []
    hook_meta = {
        "sounds": {
            "display_name": "Sound Notifications",
            "description": "Play sounds on notifications and session completion",
        },
        "memory_vault": {
            "display_name": "Memory Vault",
            "description": "Cross-repo memory: capture on session end and merges, recall brief at session start",
        },
        "statusline": {
            "display_name": "Statusline",
            "description": "One-line session status in Claude Code: model, effort, context use, rate limits, active account",
        },
    }
    for name in ("sounds", "memory_vault", "statusline"):
        meta = hook_meta[name]
        installed = _detect_hook_installed(settings, name)
        entry = {
            "name": name,
            "display_name": meta["display_name"],
            "description": meta["description"],
            "installed": installed,
            "source_available": True,
        }
        if name == "memory_vault" and installed:
            entry["health"] = _memory_vault_health()
        hooks.append(entry)

    # Knowledge
    rules_status = _detect_rules_status()
    knowledge = [
        {
            "name": "rules",
            "display_name": "Behavioral Rules",
            "description": "Coding habits and workflow rules added to ~/.claude/CLAUDE.md",
            "installed": rules_status.get("installed", False),
            "source_available": (DATA_ROOT / "rules" / "jacked_behaviors.md").exists(),
            "corrupt": rules_status.get("corrupt", False),
        },
    ]
    for skill_name in _get_valid_skill_names():
        src = DATA_ROOT / "skills" / skill_name / "SKILL.md"
        fm = _parse_frontmatter(src)
        knowledge.append({
            "name": f"skill_{skill_name}",
            "display_name": fm.get("name", f"/{skill_name} Skill"),
            "description": fm.get("description", ""),
            "installed": (CLAUDE_DIR / "skills" / skill_name / "SKILL.md").exists(),
            "source_available": src.exists(),
        })
    knowledge.append({
        "name": "reference",
        "display_name": "Reference Doc",
        "description": "Comprehensive knowledge document about jacked for Claude",
        "installed": (CLAUDE_DIR / "jacked-reference.md").exists(),
        "source_available": (DATA_ROOT / "rules" / "jacked-reference.md").exists(),
    })

    return {
        "agents": agents, "commands": commands, "hooks": hooks,
        "knowledge": knowledge,
        "settings_unreadable": settings_unreadable,
    }


# --- PUT /api/features/{category}/{name} ---

@router.put("/features/{category}/{name}")
async def toggle_feature(
    category: Literal["agents", "commands", "hooks", "knowledge"],
    name: str,
    body: FeatureToggleRequest,
    request: Request,
):
    """Enable or disable a feature."""
    # Validate name
    if not _validate_name(name):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": "Invalid feature name", "code": "INVALID_FEATURE"}},
        )

    # Validate against allowlist
    if category == "agents":
        if name not in _get_valid_agent_names():
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": f"Unknown agent: {name}", "code": "INVALID_FEATURE"}},
            )
        return await _toggle_file_feature(
            src=DATA_ROOT / "agents" / f"{name}.md",
            dst=CLAUDE_DIR / "agents" / f"{name}.md",
            enabled=body.enabled,
            name=name,
            category=category,
        )

    if category == "commands":
        if name not in _get_valid_command_names():
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": f"Unknown command: {name}", "code": "INVALID_FEATURE"}},
            )
        return await _toggle_file_feature(
            src=DATA_ROOT / "commands" / f"{name}.md",
            dst=CLAUDE_DIR / "commands" / f"{name}.md",
            enabled=body.enabled,
            name=name,
            category=category,
        )

    if category == "hooks":
        if name not in VALID_HOOKS:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": f"Unknown hook: {name}", "code": "INVALID_FEATURE"}},
            )
        db = getattr(request.app.state, "db", None)
        return await _toggle_hook(name, body.enabled, db=db)

    if category == "knowledge":
        if name not in _get_valid_knowledge_names():
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": f"Unknown knowledge item: {name}", "code": "INVALID_FEATURE"}},
            )
        return await _toggle_knowledge(name, body.enabled)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": {"message": f"Invalid category: {category}", "code": "INVALID_CATEGORY"}},
    )


# --- Toggle helpers ---

async def _toggle_file_feature(src: Path, dst: Path, enabled: bool, name: str, category: str):
    """Enable/disable a file-based feature (agents, commands)."""
    if enabled:
        if not src.exists():
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": "Source file not found. Reinstall jacked.", "code": "SOURCE_UNAVAILABLE"}},
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Path traversal final check
        try:
            dst.resolve().relative_to(CLAUDE_DIR.resolve())
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": "Invalid path", "code": "INVALID_FEATURE"}},
            )
        shutil.copy(src, dst)
    else:
        if dst.exists():
            dst.unlink()

    return {"name": name, "category": category, "enabled": enabled}


async def _toggle_hook(name: str, enabled: bool, db=None):
    """Enable/disable a hook by adding/removing entries in settings.json."""
    if name == "memory_vault":
        return await _toggle_memory_vault(enabled)
    if name == "statusline":
        return await _toggle_statusline(enabled)

    async with _settings_lock:
        try:
            settings = _read_settings_json()
        except SettingsUnreadableError:
            return _settings_unreadable_response()
        if "hooks" not in settings:
            settings["hooks"] = {}

        if name == "sounds":
            if enabled:
                _enable_sound_hooks(settings)
            else:
                _disable_sound_hooks(settings)

        _write_settings_json(settings)

    return {"name": name, "category": "hooks", "enabled": enabled}


async def _toggle_memory_vault(enabled: bool):
    """Enable/disable the memory vault via the shared setup engine.

    The engine does slow side-effects (non-interactive vault init, per-repo git
    hook install), so it runs off the event loop like the packs toggle. Home is
    resolved from ``$JACKED_HOME`` so the dashboard writes state + settings where
    the CLI reads them. Its result payload (vault path, groups,
    ``migration_available``, git-hook summary) is merged into the response so the
    dashboard can surface what happened. The settings lock is held across the
    call so a concurrent sound/env toggle can't clobber the settings.json write
    the engine performs.
    """
    from jacked.memory import setup as memory_setup
    from jacked.memory import vault as memory_vault

    home = memory_vault.jacked_home()
    async with _settings_lock:
        try:
            if enabled:
                result = await asyncio.to_thread(memory_setup.enable, home)
            else:
                result = await asyncio.to_thread(memory_setup.disable, home)
        except SettingsUnreadableError:
            # settings.json exists but can't be parsed: refuse to touch it rather
            # than clobber the user's hooks/permissions with a fresh file.
            return _settings_unreadable_response()

    return {"name": "memory_vault", "category": "hooks", "enabled": enabled, **result}


async def _toggle_statusline(enabled: bool):
    """Enable/disable the statusline via the shared setup engine.

    Same shape as the memory-vault toggle: home from ``$JACKED_HOME`` so the
    dashboard and CLI write the same files, engine off the event loop, the
    settings lock held across the engine's settings.json write. The result
    carries ``took_over_foreign`` / ``restored_previous`` so the dashboard
    can tell the user what happened to a pre-existing statusline.
    """
    from jacked import statusline_setup

    home = statusline_setup.jacked_home()
    async with _settings_lock:
        try:
            if enabled:
                result = await asyncio.to_thread(statusline_setup.enable, home)
            else:
                result = await asyncio.to_thread(statusline_setup.disable, home)
        except SettingsUnreadableError:
            return _settings_unreadable_response()

    return {"name": "statusline", "category": "hooks", "enabled": enabled, **result}


async def _toggle_knowledge(name: str, enabled: bool):
    """Enable/disable a knowledge feature."""
    if name == "rules":
        return await _toggle_rules(enabled)
    if name.startswith("skill_"):
        skill_name = name[len("skill_"):]
        if _validate_name(skill_name):
            src = DATA_ROOT / "skills" / skill_name / "SKILL.md"
            dst = CLAUDE_DIR / "skills" / skill_name / "SKILL.md"
            if src.exists():
                return await _toggle_file_feature(src, dst, enabled, name, "knowledge")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": f"Unknown skill: {skill_name}", "code": "INVALID_FEATURE"}},
        )
    if name == "reference":
        src = DATA_ROOT / "rules" / "jacked-reference.md"
        dst = CLAUDE_DIR / "jacked-reference.md"
        return await _toggle_file_feature(src, dst, enabled, name, "knowledge")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": {"message": f"Unknown knowledge: {name}", "code": "INVALID_FEATURE"}},
    )


async def _toggle_rules(enabled: bool):
    """Enable/disable behavioral rules in CLAUDE.md."""
    if enabled:
        rules_src = DATA_ROOT / "rules" / "jacked_behaviors.md"
        if not rules_src.exists():
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": "Rules source not found. Reinstall jacked.", "code": "SOURCE_UNAVAILABLE"}},
            )
        rules_text = rules_src.read_text(encoding="utf-8").strip()

        CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if CLAUDE_MD.exists():
            existing = CLAUDE_MD.read_text(encoding="utf-8")

        # Check if already installed
        if RULES_START_PREFIX in existing and RULES_END_MARKER in existing:
            return {"name": "rules", "category": "knowledge", "enabled": True}

        # Orphaned markers
        has_start = RULES_START_PREFIX in existing
        has_end = RULES_END_MARKER in existing
        if has_start != has_end:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {
                    "message": "CLAUDE.md has corrupted jacked rules markers. Fix manually or remove the orphaned marker.",
                    "code": "FILE_CORRUPT",
                }},
            )

        # Append rules
        if existing and not existing.endswith("\n\n"):
            if existing.endswith("\n"):
                new_content = existing + "\n" + rules_text + "\n"
            else:
                new_content = existing + "\n\n" + rules_text + "\n"
        else:
            new_content = existing + rules_text + "\n"

        CLAUDE_MD.write_text(new_content, encoding="utf-8")

    else:
        # Remove rules
        if not CLAUDE_MD.exists():
            return {"name": "rules", "category": "knowledge", "enabled": False}

        content = CLAUDE_MD.read_text(encoding="utf-8")
        if RULES_START_PREFIX not in content or RULES_END_MARKER not in content:
            return {"name": "rules", "category": "knowledge", "enabled": False}

        start_idx = content.index(RULES_START_PREFIX)
        end_idx = content.index(RULES_END_MARKER) + len(RULES_END_MARKER)

        before = content[:start_idx].rstrip("\n")
        after = content[end_idx:].lstrip("\n")

        if before and after:
            new_content = before + "\n\n" + after
        elif before:
            new_content = before + "\n"
        else:
            new_content = after

        CLAUDE_MD.write_text(new_content, encoding="utf-8")

    return {"name": "rules", "category": "knowledge", "enabled": enabled}


# --- Hook enable/disable helpers ---

def _enable_sound_hooks(settings: dict):
    """Add sound notification hooks."""
    from jacked.cli import _get_sound_command, _replace_stale_sound_hook, _sound_hook_marker

    marker = _sound_hook_marker()

    # Notification hook
    if "Notification" not in settings["hooks"]:
        settings["hooks"]["Notification"] = []
    if not any(marker in str(h) for h in settings["hooks"]["Notification"]):
        settings["hooks"]["Notification"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": marker + _get_sound_command("notification")}]
        })
    else:
        _replace_stale_sound_hook(settings["hooks"]["Notification"], marker, "notification")

    # Stop sound hook
    if "Stop" not in settings["hooks"]:
        settings["hooks"]["Stop"] = []
    if not any(marker in str(h) for h in settings["hooks"]["Stop"]):
        settings["hooks"]["Stop"].append({
            "matcher": "",
            "hooks": [{"type": "command", "command": marker + _get_sound_command("complete")}]
        })
    else:
        _replace_stale_sound_hook(settings["hooks"]["Stop"], marker, "complete")


def _disable_sound_hooks(settings: dict):
    """Remove sound hooks from settings."""
    from jacked.cli import _sound_hook_marker

    marker = _sound_hook_marker()
    for hook_type in ("Notification", "Stop"):
        if hook_type in settings.get("hooks", {}):
            settings["hooks"][hook_type] = [
                h for h in settings["hooks"][hook_type]
                if marker not in str(h)
            ]


# ---------------------------------------------------------------------------
# Claude Code settings endpoints — read/write ~/.claude/settings.json
# These expose Claude Code's own config, not jacked features.
# ---------------------------------------------------------------------------

@router.get("/claude-settings")
async def get_claude_settings():
    """Return current state of all Claude Code settings."""
    # Read-only: a corrupt settings.json degrades to empty defaults plus a flag
    # rather than 500ing the settings panel.
    try:
        settings = _read_settings_json()
        settings_unreadable = False
    except SettingsUnreadableError:
        settings = {}
        settings_unreadable = True
    env_section = settings.get("env", {})

    # Env var toggles
    env_toggles = []
    for var_name, meta in TOGGLEABLE_ENV_VARS.items():
        value_on = meta.get("value_on", "1")
        env_toggles.append({
            "name": var_name,
            "display_name": meta["display_name"],
            "description": meta["description"],
            "section": meta["section"],
            "enabled": env_section.get(var_name) == value_on,
        })

    # Numeric env vars
    env_numeric = []
    for var_name, meta in NUMERIC_ENV_VARS.items():
        env_numeric.append({
            "name": var_name,
            "display_name": meta["display_name"],
            "description": meta["description"],
            "section": meta["section"],
            "value": env_section.get(var_name, meta["default"]),
            "default": meta["default"],
            "min": meta["min"],
            "max": meta["max"],
        })

    # Direct settings keys
    direct_settings = []
    for key, meta in DIRECT_SETTINGS.items():
        direct_settings.append({
            "name": key,
            "display_name": meta["display_name"],
            "description": meta["description"],
            "type": meta["type"],
            "value": settings.get(key, meta["default"]),
            "default": meta["default"],
        })

    # Plugins
    enabled_plugins = settings.get("enabledPlugins", {})
    plugins = [
        {"name": name, "enabled": bool(val)}
        for name, val in sorted(enabled_plugins.items())
    ]

    # Permissions
    perms = settings.get("permissions", {})
    permissions = {
        "allow": perms.get("allow", []),
        "deny": perms.get("deny", []),
        "ask": perms.get("ask", []),
        "defaultMode": perms.get("defaultMode", "default"),
    }

    return {
        "env_toggles": env_toggles,
        "env_numeric": env_numeric,
        "direct_settings": direct_settings,
        "plugins": plugins,
        "permissions": permissions,
        "settings_unreadable": settings_unreadable,
    }


class EnvToggleRequest(BaseModel):
    enabled: bool | None = None
    value: str | None = None


@router.put("/claude-settings/env/{name}")
async def set_claude_env(name: str, body: EnvToggleRequest):
    """Toggle or set a Claude Code env var in settings.json."""
    is_toggle = name in TOGGLEABLE_ENV_VARS
    is_numeric = name in NUMERIC_ENV_VARS

    if not is_toggle and not is_numeric:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": f"Unknown env var: {name}", "code": "INVALID_ENV_VAR"}},
        )

    async with _settings_lock:
        try:
            settings = _read_settings_json()
        except SettingsUnreadableError:
            return _settings_unreadable_response()
        if "env" not in settings:
            settings["env"] = {}

        if is_toggle:
            if body.enabled is None:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"error": {"message": "enabled field is required for toggle vars", "code": "MISSING_FIELD"}},
                )
            value_on = TOGGLEABLE_ENV_VARS[name].get("value_on", "1")
            if body.enabled:
                settings["env"][name] = value_on
            else:
                settings["env"].pop(name, None)
        else:
            # Numeric
            meta = NUMERIC_ENV_VARS[name]
            raw = body.value if body.value is not None else meta["default"]
            try:
                num = int(raw)
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"error": {"message": "Value must be a number", "code": "INVALID_VALUE"}},
                )
            num = max(meta["min"], min(meta["max"], num))
            settings["env"][name] = str(num)

        _write_settings_json(settings)

    return {"name": name, "ok": True}


class DirectSettingRequest(BaseModel):
    value: bool | int | str | None = None


@router.put("/claude-settings/key/{name}")
async def set_claude_key(name: str, body: DirectSettingRequest):
    """Set a direct Claude Code settings.json key."""
    if name not in DIRECT_SETTINGS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": f"Unknown setting: {name}", "code": "INVALID_SETTING"}},
        )

    meta = DIRECT_SETTINGS[name]

    async with _settings_lock:
        try:
            settings = _read_settings_json()
        except SettingsUnreadableError:
            return _settings_unreadable_response()

        if meta["type"] == "bool":
            if not isinstance(body.value, bool):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"error": {"message": "Value must be a boolean (true/false)", "code": "INVALID_VALUE"}},
                )
            settings[name] = body.value
        elif meta["type"] == "number":
            try:
                num = int(body.value)
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"error": {"message": "Value must be a number", "code": "INVALID_VALUE"}},
                )
            lo = meta.get("min", num)
            hi = meta.get("max", num)
            settings[name] = max(lo, min(hi, num))

        _write_settings_json(settings)

    return {"name": name, "ok": True}


class PluginToggleRequest(BaseModel):
    enabled: bool


@router.put("/claude-settings/plugins/{name:path}")
async def toggle_claude_plugin(name: str, body: PluginToggleRequest):
    """Enable or disable a Claude Code plugin."""
    if not name or len(name) > 200 or "\0" in name:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": "Invalid plugin name", "code": "INVALID_PLUGIN"}},
        )
    async with _settings_lock:
        try:
            settings = _read_settings_json()
        except SettingsUnreadableError:
            return _settings_unreadable_response()
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}

        # Explicit true/false — Claude Code treats absent keys as "enabled
        # by default", so .pop() doesn't actually disable plugins.
        # Note: upstream Claude Code bugs may ignore false for some plugins
        # (see anthropics/claude-code#13344); may need installed_plugins.json
        # manipulation as follow-up.
        settings["enabledPlugins"][name] = body.enabled

        _write_settings_json(settings)

    return {"name": name, "enabled": body.enabled}


class PermissionsRequest(BaseModel):
    allow: list[str] | None = None
    deny: list[str] | None = None
    ask: list[str] | None = None
    defaultMode: str | None = None


@router.put("/claude-settings/permissions")
async def set_claude_permissions(body: PermissionsRequest):
    """Update Claude Code permission rules."""
    if body.defaultMode and body.defaultMode not in VALID_PERMISSION_MODES:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": f"Invalid mode: {body.defaultMode}. Valid: {', '.join(sorted(VALID_PERMISSION_MODES))}", "code": "INVALID_MODE"}},
        )

    async with _settings_lock:
        try:
            settings = _read_settings_json()
        except SettingsUnreadableError:
            return _settings_unreadable_response()
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

        _write_settings_json(settings)

    return {"ok": True}


@router.get("/claude-settings/raw")
async def get_raw_settings():
    """Return the raw settings.json content for the editor.

    Read-only: a corrupt file degrades to empty content plus a flag so the editor
    can warn instead of 500ing. The PUT path requires an explicit
    ``confirm_overwrite`` before it replaces the file, so an accidental save of {}
    can't silently wipe a real config.
    """
    try:
        return {"content": _read_settings_json(), "settings_unreadable": False}
    except SettingsUnreadableError:
        return {"content": {}, "settings_unreadable": True}


class RawSettingsRequest(BaseModel):
    content: dict
    confirm_overwrite: bool = False


@router.put("/claude-settings/raw")
async def set_raw_settings(body: RawSettingsRequest):
    """Overwrite settings.json with raw JSON content. Requires confirm_overwrite."""
    if not body.confirm_overwrite:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": "Set confirm_overwrite: true to overwrite settings.json", "code": "CONFIRMATION_REQUIRED"}},
        )
    async with _settings_lock:
        _write_settings_json(body.content)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chrome DevTools MCP endpoints — manage browser tool configuration
# ---------------------------------------------------------------------------

@router.get("/chrome-devtools-mcp")
async def get_chrome_devtools_mcp():
    """Get Chrome DevTools MCP configuration status."""
    from jacked.cli import _get_chrome_devtools_mcp_status

    result = await asyncio.to_thread(_get_chrome_devtools_mcp_status)
    return result


class ChromeDevToolsMCPRequest(BaseModel):
    mode: Literal["autoConnect", "browserUrl", "launch", "headless"]
    # Keep in sync with CHROME_DEVTOOLS_MODES in jacked/cli.py


@router.put("/chrome-devtools-mcp")
async def set_chrome_devtools_mcp(body: ChromeDevToolsMCPRequest):
    """Update Chrome DevTools MCP connection mode."""
    from jacked.cli import _set_chrome_devtools_mcp_mode

    success, message = await asyncio.to_thread(
        _set_chrome_devtools_mcp_mode, body.mode
    )
    if not success:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"message": message}},
        )
    return {"ok": True, "mode": body.mode, "message": message}


@router.delete("/chrome-devtools-mcp")
async def remove_chrome_devtools_mcp():
    """Remove Chrome DevTools MCP configuration."""
    from jacked.cli import _remove_chrome_devtools_mcp

    removed = await asyncio.to_thread(_remove_chrome_devtools_mcp)
    if not removed:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": {"message": "Failed to remove Chrome DevTools MCP"}},
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# DCR review-engine endpoints — which engine /dcr runs its reviewer agents on
#
# Backed by <home>/.claude/jacked-dcr.json, a file of its own (NOT settings.json),
# so it gets its own lock: a DCR write must not serialize behind, or race, a
# settings.json write. ``keep_on_claude`` is read-only here on purpose — the lens
# list is CLI-only for now.
# ---------------------------------------------------------------------------

_dcr_lock = asyncio.Lock()


def _dcr_unreadable_response() -> JSONResponse:
    """The 503 the DCR mutation endpoint returns when jacked-dcr.json exists but
    cannot be parsed: refuse to modify it rather than clobber the user's real
    config with a fresh file (mirrors ``_settings_unreadable_response``)."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {
            "message": (
                "jacked-dcr.json is unreadable; refusing to modify it. "
                "Fix the JSON or delete the file, then try again."
            ),
            "code": "DCR_SETTINGS_UNREADABLE",
        }},
    )


class DcrEngineRequest(BaseModel):
    # Keep in sync with ENGINE_CHOICES in jacked/dcr_settings.py
    engine: Literal["claude", "codex"]
    model: str | None = None
    effort: str | None = None


def _apply_dcr_engine(home: Path, engine: str, model, effort) -> dict:
    """Merge the request into the DCR config, then return the fresh contract.

    Delegates the read-merge-write to ``dcr_settings.update_config`` — the same
    function the CLI uses — so the two surfaces cannot drift on merge semantics,
    and so the write is serialized by the cross-process file lock (the asyncio
    ``_dcr_lock`` only covers this process).

    Blocking (file IO plus the codex preflight subprocess) — call it through
    ``asyncio.to_thread``. Raises ``DcrSettingsUnreadableError`` on a corrupt
    file, before anything is written, and ``DcrSettingsAccessError`` when the
    filesystem refuses the write.
    """
    from jacked import dcr_settings

    dcr_settings.update_config(home, engine=engine, model=model, effort=effort)
    return dcr_settings.resolve(home)


@router.get("/dcr-engine")
async def get_dcr_engine():
    """Return the resolved DCR engine contract (config plus codex preflight).

    Read-only and never 500s on a bad file: a corrupt jacked-dcr.json resolves to
    Claude with a ``reason`` the dashboard can show.
    """
    from jacked import dcr_settings

    home = dcr_settings.jacked_home()
    return await asyncio.to_thread(dcr_settings.resolve, home)


@router.put("/dcr-engine")
async def set_dcr_engine(body: DcrEngineRequest):
    """Set the DCR review engine (and optionally its model and effort)."""
    from jacked import dcr_settings
    from jacked.dcr_settings import DcrSettingsAccessError, DcrSettingsUnreadableError

    if body.effort is not None and body.effort not in dcr_settings.VALID_EFFORTS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {
                "message": (
                    f"Invalid effort '{body.effort}'. Valid values: "
                    + ", ".join(dcr_settings.EFFORT_CHOICES)
                ),
                "code": "INVALID_VALUE",
            }},
        )

    home = dcr_settings.jacked_home()
    async with _dcr_lock:
        try:
            return await asyncio.to_thread(
                _apply_dcr_engine, home, body.engine, body.model, body.effort
            )
        except DcrSettingsUnreadableError:
            return _dcr_unreadable_response()
        except DcrSettingsAccessError as exc:
            # The filesystem refused the write (permissions, read-only volume,
            # full disk). Nothing was changed; the message names the path and
            # the directory whose permissions to check.
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": {
                    "message": str(exc),
                    "code": "DCR_SETTINGS_UNWRITABLE",
                }},
            )
        except ValueError as exc:
            # write_config rejected a field (an empty model is the reachable case).
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"message": str(exc), "code": "INVALID_VALUE"}},
            )
