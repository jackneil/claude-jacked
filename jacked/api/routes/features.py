"""Feature detection and toggle routes — agents, commands, hooks, knowledge."""

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Constants ---

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
DATA_ROOT = Path(__file__).parent.parent.parent / "data"

# Markers (match cli.py exactly)
SECURITY_MARKER = "# jacked-security"
SOUND_MARKER = "# jacked-sound: "
RULES_START_PREFIX = "# jacked-behaviors-v"
RULES_END_MARKER = "# end-jacked-behaviors"

# Valid hook/knowledge names (allowlist)
VALID_HOOKS = {"security_gatekeeper", "session_indexing", "sounds"}
VALID_KNOWLEDGE = {"rules", "skill", "reference"}

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
    """Read ~/.claude/settings.json, returning {} if missing or corrupt."""
    if not SETTINGS_JSON.exists():
        return {}
    try:
        return json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_settings_json(data: dict):
    """Write ~/.claude/settings.json atomically (write-to-temp then rename)."""
    SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_JSON)


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

    if hook_name == "security_gatekeeper":
        for entry in hooks.get("PreToolUse", []):
            if SECURITY_MARKER in str(entry) or "security_gatekeeper" in str(entry):
                return True
        # Check legacy PermissionRequest too
        for entry in hooks.get("PermissionRequest", []):
            if SECURITY_MARKER in str(entry) or "security_gatekeeper" in str(entry):
                return True
        return False

    if hook_name == "session_indexing":
        for entry in hooks.get("Stop", []):
            for h in entry.get("hooks", []):
                if "jacked" in h.get("command", ""):
                    return True
        return False

    if hook_name == "sounds":
        for hook_type in ("Notification", "Stop"):
            for entry in hooks.get(hook_type, []):
                if SOUND_MARKER in str(entry):
                    return True
        return False

    return False


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
    settings = _read_settings_json()

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
        "security_gatekeeper": {
            "display_name": "Security Gatekeeper",
            "description": "LLM-powered command evaluation for auto-approving safe commands",
        },
        "session_indexing": {
            "display_name": "Session Indexing",
            "description": "Index Claude sessions for semantic search (requires qdrant-client)",
        },
        "sounds": {
            "display_name": "Sound Notifications",
            "description": "Play sounds on notifications and session completion",
        },
    }
    for name in ("security_gatekeeper", "session_indexing", "sounds"):
        meta = hook_meta[name]
        hooks.append({
            "name": name,
            "display_name": meta["display_name"],
            "description": meta["description"],
            "installed": _detect_hook_installed(settings, name),
            "source_available": True,
        })

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
        {
            "name": "skill",
            "display_name": "/jacked Skill",
            "description": "Search and load context from past Claude sessions",
            "installed": (CLAUDE_DIR / "skills" / "jacked" / "SKILL.md").exists(),
            "source_available": (DATA_ROOT / "skills" / "jacked" / "SKILL.md").exists(),
        },
        {
            "name": "reference",
            "display_name": "Reference Doc",
            "description": "Comprehensive knowledge document about jacked for Claude",
            "installed": (CLAUDE_DIR / "jacked-reference.md").exists(),
            "source_available": (DATA_ROOT / "rules" / "jacked-reference.md").exists(),
        },
    ]

    return {
        "agents": agents, "commands": commands, "hooks": hooks,
        "knowledge": knowledge,
    }


# --- PUT /api/features/{category}/{name} ---

@router.put("/features/{category}/{name}")
async def toggle_feature(
    category: Literal["agents", "commands", "hooks", "knowledge"],
    name: str,
    body: FeatureToggleRequest,
):
    """Enable or disable a feature."""
    # Validate name
    if not _validate_name(name):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Invalid feature name", "code": "INVALID_FEATURE"}},
        )

    # Validate against allowlist
    if category == "agents":
        if name not in _get_valid_agent_names():
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": f"Unknown hook: {name}", "code": "INVALID_FEATURE"}},
            )
        return await _toggle_hook(name, body.enabled)

    if category == "knowledge":
        if name not in VALID_KNOWLEDGE:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": f"Unknown knowledge item: {name}", "code": "INVALID_FEATURE"}},
            )
        return await _toggle_knowledge(name, body.enabled)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"message": f"Invalid category: {category}", "code": "INVALID_CATEGORY"}},
    )


# --- Toggle helpers ---

async def _toggle_file_feature(src: Path, dst: Path, enabled: bool, name: str, category: str):
    """Enable/disable a file-based feature (agents, commands)."""
    if enabled:
        if not src.exists():
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": {"message": f"Source file not found. Reinstall jacked.", "code": "SOURCE_UNAVAILABLE"}},
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Path traversal final check
        try:
            dst.resolve().relative_to(CLAUDE_DIR.resolve())
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": {"message": "Invalid path", "code": "INVALID_FEATURE"}},
            )
        shutil.copy(src, dst)
    else:
        if dst.exists():
            dst.unlink()

    return {"name": name, "category": category, "enabled": enabled}


async def _toggle_hook(name: str, enabled: bool):
    """Enable/disable a hook in settings.json."""
    async with _settings_lock:
        settings = _read_settings_json()
        if "hooks" not in settings:
            settings["hooks"] = {}

        if name == "security_gatekeeper":
            if enabled:
                _enable_security_hook(settings)
            else:
                _disable_security_hook(settings)

        elif name == "session_indexing":
            if enabled:
                _enable_session_indexing_hook(settings)
            else:
                _disable_session_indexing_hook(settings)

        elif name == "sounds":
            if enabled:
                _enable_sound_hooks(settings)
            else:
                _disable_sound_hooks(settings)

        _write_settings_json(settings)

    return {"name": name, "category": "hooks", "enabled": enabled}


async def _toggle_knowledge(name: str, enabled: bool):
    """Enable/disable a knowledge feature."""
    if name == "rules":
        return await _toggle_rules(enabled)
    if name == "skill":
        src = DATA_ROOT / "skills" / "jacked" / "SKILL.md"
        dst = CLAUDE_DIR / "skills" / "jacked" / "SKILL.md"
        return await _toggle_file_feature(src, dst, enabled, name, "knowledge")
    if name == "reference":
        src = DATA_ROOT / "rules" / "jacked-reference.md"
        dst = CLAUDE_DIR / "jacked-reference.md"
        return await _toggle_file_feature(src, dst, enabled, name, "knowledge")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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

def _enable_security_hook(settings: dict):
    """Add security gatekeeper hook to settings."""
    python_exe = sys.executable
    if not python_exe or not Path(python_exe).exists():
        python_exe = shutil.which("python3") or shutil.which("python") or "python"

    script_path = DATA_ROOT / "hooks" / "security_gatekeeper.py"
    python_path = str(Path(python_exe)).replace("\\", "/")
    script_str = str(script_path).replace("\\", "/")
    command_str = f"{python_path} {script_str}"

    if "PreToolUse" not in settings["hooks"]:
        settings["hooks"]["PreToolUse"] = []

    # Check if already installed
    for entry in settings["hooks"]["PreToolUse"]:
        if SECURITY_MARKER in str(entry) or "security_gatekeeper" in str(entry):
            return

    hook_entry = {
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": command_str,
            "timeout": 30,
        }]
    }
    settings["hooks"]["PreToolUse"].append(hook_entry)


def _disable_security_hook(settings: dict):
    """Remove security gatekeeper hook from settings."""
    for hook_type in ("PreToolUse", "PermissionRequest"):
        if hook_type in settings.get("hooks", {}):
            settings["hooks"][hook_type] = [
                h for h in settings["hooks"][hook_type]
                if SECURITY_MARKER not in str(h) and "security_gatekeeper" not in str(h)
            ]


def _enable_session_indexing_hook(settings: dict):
    """Add session indexing Stop hook."""
    if "Stop" not in settings["hooks"]:
        settings["hooks"]["Stop"] = []

    # Check if already installed
    for entry in settings["hooks"]["Stop"]:
        for h in entry.get("hooks", []):
            if "jacked" in h.get("command", ""):
                return

    settings["hooks"]["Stop"].append({
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": 'jacked index --repo "$CLAUDE_PROJECT_DIR"',
            "async": True,
        }]
    })


def _disable_session_indexing_hook(settings: dict):
    """Remove session indexing Stop hook."""
    if "Stop" not in settings.get("hooks", {}):
        return
    settings["hooks"]["Stop"] = [
        entry for entry in settings["hooks"]["Stop"]
        if not any("jacked" in h.get("command", "") for h in entry.get("hooks", []))
    ]


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
    settings = _read_settings_json()
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": f"Unknown env var: {name}", "code": "INVALID_ENV_VAR"}},
        )

    async with _settings_lock:
        settings = _read_settings_json()
        if "env" not in settings:
            settings["env"] = {}

        if is_toggle:
            if body.enabled is None:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"error": {"message": f"Value must be a number", "code": "INVALID_VALUE"}},
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": f"Unknown setting: {name}", "code": "INVALID_SETTING"}},
        )

    meta = DIRECT_SETTINGS[name]

    async with _settings_lock:
        settings = _read_settings_json()

        if meta["type"] == "bool":
            if not isinstance(body.value, bool):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"error": {"message": f"Value must be a boolean (true/false)", "code": "INVALID_VALUE"}},
                )
            settings[name] = body.value
        elif meta["type"] == "number":
            try:
                num = int(body.value)
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"error": {"message": f"Value must be a number", "code": "INVALID_VALUE"}},
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Invalid plugin name", "code": "INVALID_PLUGIN"}},
        )
    async with _settings_lock:
        settings = _read_settings_json()
        if "enabledPlugins" not in settings:
            settings["enabledPlugins"] = {}

        if body.enabled:
            settings["enabledPlugins"][name] = True
        else:
            settings["enabledPlugins"].pop(name, None)

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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": f"Invalid mode: {body.defaultMode}. Valid: {', '.join(sorted(VALID_PERMISSION_MODES))}", "code": "INVALID_MODE"}},
        )

    async with _settings_lock:
        settings = _read_settings_json()
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
    """Return the raw settings.json content for the editor."""
    return {"content": _read_settings_json()}


class RawSettingsRequest(BaseModel):
    content: dict
    confirm_overwrite: bool = False


@router.put("/claude-settings/raw")
async def set_raw_settings(body: RawSettingsRequest):
    """Overwrite settings.json with raw JSON content. Requires confirm_overwrite."""
    if not body.confirm_overwrite:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Set confirm_overwrite: true to overwrite settings.json", "code": "CONFIRMATION_REQUIRED"}},
        )
    async with _settings_lock:
        _write_settings_json(body.content)
    return {"ok": True}
