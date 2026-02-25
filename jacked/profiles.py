"""Security profile export, import, validation, backup, and restore.

Profiles capture the full gatekeeper configuration + Claude Code permission
rules as a portable JSON file.  Import creates an automatic backup so the
user can one-click undo.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from packaging.version import Version
from pydantic import BaseModel, Field, field_validator, model_validator

from jacked import __version__

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROFILE_DIR_NAME = "profiles"
BACKUP_DIR_NAME = ".backups"

# Mirrored from permissions.py — broad patterns that bypass security
_DANGEROUS_PATTERNS = {
    "Bash(*:*)",
    "Bash(rm:*)",
    "Bash(sudo:*)",
    "Bash(rm -rf:*)",
    "Bash(sh:*)",
    "Bash(bash:*)",
}

# Allowlisted gatekeeper DB keys (NEVER include gatekeeper.api_key)
_CONFIG_KEYS = (
    "gatekeeper.model",
    "gatekeeper.eval_method",
    "gatekeeper.command_categories",
    "gatekeeper.path_safety",
    "gatekeeper.tools",
)

_VALID_MODELS = {"haiku", "sonnet", "opus"}
_VALID_EVAL_METHODS = {"api_first", "cli_first", "api_only", "cli_only"}
_VALID_CATEGORY_MODES = {"allow", "evaluate", "ask", "deny"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,63}$")

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class GatekeeperConfig(BaseModel):
    """Gatekeeper settings snapshot (all optional for partial profiles)."""

    model_config = {"extra": "forbid"}

    model: Optional[str] = None
    eval_method: Optional[str] = None
    command_categories: Optional[dict[str, str]] = None
    path_safety: Optional[dict] = None
    tool_toggles: Optional[dict[str, bool]] = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_MODELS:
            raise ValueError(f"Invalid model '{v}', must be one of {_VALID_MODELS}")
        return v

    @field_validator("eval_method")
    @classmethod
    def validate_eval_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_EVAL_METHODS:
            raise ValueError(
                f"Invalid eval_method '{v}', must be one of {_VALID_EVAL_METHODS}"
            )
        return v

    @field_validator("command_categories")
    @classmethod
    def validate_command_categories(
        cls, v: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        if v is not None:
            for cat, mode in v.items():
                if mode not in _VALID_CATEGORY_MODES:
                    raise ValueError(
                        f"Invalid mode '{mode}' for category '{cat}', "
                        f"must be one of {_VALID_CATEGORY_MODES}"
                    )
        return v


class RulesSchema(BaseModel):
    """Claude Code permission rules."""

    model_config = {"extra": "forbid"}

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)


class ProfileSchema(BaseModel):
    """Top-level profile document."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=100)
    jacked_version: str = ""
    created_at: str = ""
    rules: RulesSchema = Field(default_factory=RulesSchema)
    gatekeeper_config: GatekeeperConfig = Field(default_factory=GatekeeperConfig)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                "Name must be 1-64 chars, alphanumeric/dashes/spaces, "
                "no leading space"
            )
        # Path traversal guard
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError("Name must not contain path traversal characters")
        return v

    @model_validator(mode="after")
    def check_no_traversal(self):
        """Extra traversal guard on fully constructed name."""
        filename = _name_to_filename(self.name)
        if ".." in filename or "/" in filename:
            raise ValueError("Name must not contain path traversal characters")
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name_to_filename(name: str) -> str:
    """Convert profile name to safe filename.

    >>> _name_to_filename("My Cool Profile")
    'my-cool-profile.json'
    >>> _name_to_filename("simple")
    'simple.json'
    """
    return name.lower().replace(" ", "-") + ".json"


def _dangerous_category_modes(categories: dict[str, str]) -> list[str]:
    """Return warnings for dangerous category mode settings."""
    warnings: list[str] = []
    for cat, mode in categories.items():
        if mode == "allow":
            warnings.append(
                f"Category '{cat}' set to 'allow' — commands run without review"
            )
    return warnings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_profile(data: dict) -> list[str]:
    """Validate profile data and return warnings.

    Raises ValueError if the profile version is higher than the running
    jacked version (forward-incompatible).

    Returns a list of warning strings for risky-but-valid settings.
    """
    warnings: list[str] = []

    # Version check — reject if profile needs a newer jacked
    profile_version_str = data.get("jacked_version", "")
    if profile_version_str:
        try:
            profile_ver = Version(profile_version_str)
            current_ver = Version(__version__)
            if profile_ver > current_ver:
                raise ValueError(
                    f"Profile requires jacked {profile_version_str} but you have "
                    f"{__version__}. Upgrade jacked before importing."
                )
        except ValueError as e:
            if "Profile requires" in str(e):
                raise
            # Unparseable version — warn but don't block
            warnings.append(f"Could not parse profile version: {profile_version_str}")

    # Check rules for dangerous patterns
    rules = data.get("rules", {})
    for list_name in ("allow", "deny", "ask"):
        for pattern in rules.get(list_name, []):
            if pattern in _DANGEROUS_PATTERNS:
                warnings.append(
                    f"Dangerous pattern in {list_name}: '{pattern}'"
                )

    # Check gatekeeper config
    gk = data.get("gatekeeper_config", {})

    # path_safety checks
    ps = gk.get("path_safety", {})
    if ps:
        if ps.get("enabled") is False:
            warnings.append("Path safety is disabled — all paths will be accessible")

        allowed_paths = ps.get("allowed_paths", [])
        for p in allowed_paths:
            if p in ("/", "/*"):
                warnings.append(
                    f"Root allowed_path '{p}' — entire filesystem accessible"
                )

    # command_categories checks
    cats = gk.get("command_categories", {})
    if cats:
        warnings.extend(_dangerous_category_modes(cats))

    return warnings


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_profile(
    name: str,
    description: str,
    author: str,
    db,
    settings_json: dict,
    profiles_dir: Path,
) -> Path:
    """Export current gatekeeper config + rules as a profile.

    Args:
        name: Human-readable profile name.
        description: Optional description.
        author: Optional author name.
        db: Database instance (for reading gatekeeper settings).
        settings_json: Current contents of ~/.claude/settings.json.
        profiles_dir: Directory to save profile into.

    Returns:
        Path to the created profile file.
    """
    # Build gatekeeper_config from DB by allowlisted keys only
    gk_config: dict = {}
    for key in _CONFIG_KEYS:
        raw = db.get_setting(key)
        if raw is not None:
            # Strip the "gatekeeper." prefix for the profile key
            short_key = key.split(".", 1)[1]
            try:
                gk_config[short_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                gk_config[short_key] = raw

    # Rename keys to match schema
    if "tools" in gk_config:
        gk_config["tool_toggles"] = gk_config.pop("tools")

    # Extract rules from settings.json
    perms = settings_json.get("permissions", {})
    rules = {
        "allow": perms.get("allow", []),
        "deny": perms.get("deny", []),
        "ask": perms.get("ask", []),
    }

    profile = ProfileSchema(
        name=name,
        description=description,
        author=author,
        jacked_version=__version__,
        created_at=datetime.now(timezone.utc).isoformat(),
        rules=RulesSchema(**rules),
        gatekeeper_config=GatekeeperConfig(**gk_config),
    )

    profiles_dir.mkdir(parents=True, exist_ok=True)
    filename = _name_to_filename(name)
    filepath = profiles_dir / filename
    filepath.write_text(
        profile.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return filepath


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------


def _create_backup(db, settings_json: dict, backup_dir: Path) -> Path:
    """Create a timestamped backup of current config before import.

    Returns the path to the backup file.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"backup-{ts}.json"

    # Snapshot gatekeeper config from DB
    gk_snapshot: dict = {}
    for key in _CONFIG_KEYS:
        raw = db.get_setting(key)
        if raw is not None:
            gk_snapshot[key] = raw

    backup_data = {
        "timestamp": ts,
        "jacked_version": __version__,
        "settings_json": settings_json,
        "gatekeeper_db": gk_snapshot,
    }
    backup_path.write_text(json.dumps(backup_data, indent=2), encoding="utf-8")
    return backup_path


def get_latest_backup(backup_dir: Path) -> Optional[Path]:
    """Return the most recent backup file, or None."""
    if not backup_dir.exists():
        return None
    backups = sorted(backup_dir.glob("backup-*.json"), reverse=True)
    return backups[0] if backups else None


def restore_backup(backup_path: Path, db, write_settings_fn) -> None:
    """Restore gatekeeper config + rules from a backup file.

    Args:
        backup_path: Path to the backup JSON file.
        db: Database instance for writing gatekeeper settings.
        write_settings_fn: Callable(dict) to write settings.json.
    """
    data = json.loads(backup_path.read_text(encoding="utf-8"))

    # Restore settings.json
    write_settings_fn(data["settings_json"])

    # Restore gatekeeper DB settings
    for key, value in data.get("gatekeeper_db", {}).items():
        db.set_setting(key, value)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_profile(
    profile_data: dict,
    db,
    settings_json: dict,
    write_settings_fn,
    profiles_dir: Path,
    backup_dir: Path,
) -> tuple[Path, list[str]]:
    """Validate, backup, and apply a profile.

    Args:
        profile_data: Raw profile dict (will be schema-validated).
        db: Database instance.
        settings_json: Current settings.json contents.
        write_settings_fn: Callable(dict) to write settings.json atomically.
        profiles_dir: Where to save the profile file.
        backup_dir: Where to save the pre-import backup.

    Returns:
        Tuple of (backup_path, warnings).

    Raises:
        ValueError: If schema validation fails or version too high.
        pydantic.ValidationError: If profile structure is invalid.
    """
    # Schema-validate
    profile = ProfileSchema(**profile_data)

    # Validate content (may raise ValueError for version)
    warnings = validate_profile(profile_data)

    # Create backup before modifying anything
    backup_path = _create_backup(db, settings_json, backup_dir)

    # Apply rules to settings.json
    new_settings = dict(settings_json)
    rules = profile.rules.model_dump()
    new_settings["permissions"] = {
        "allow": rules["allow"],
        "deny": rules["deny"],
        "ask": rules["ask"],
    }
    write_settings_fn(new_settings)

    # Apply gatekeeper config to DB
    gk = profile.gatekeeper_config
    if gk.model is not None:
        db.set_setting("gatekeeper.model", json.dumps(gk.model))
    if gk.eval_method is not None:
        db.set_setting("gatekeeper.eval_method", json.dumps(gk.eval_method))
    if gk.command_categories is not None:
        db.set_setting(
            "gatekeeper.command_categories", json.dumps(gk.command_categories)
        )
    if gk.path_safety is not None:
        db.set_setting("gatekeeper.path_safety", json.dumps(gk.path_safety))
    if gk.tool_toggles is not None:
        db.set_setting("gatekeeper.tools", json.dumps(gk.tool_toggles))

    # Save profile to profiles dir
    profiles_dir.mkdir(parents=True, exist_ok=True)
    filename = _name_to_filename(profile.name)
    filepath = profiles_dir / filename
    filepath.write_text(
        profile.model_dump_json(indent=2),
        encoding="utf-8",
    )

    return backup_path, warnings


# ---------------------------------------------------------------------------
# List / Delete
# ---------------------------------------------------------------------------


def list_profiles(profiles_dir: Path) -> list[dict]:
    """List all saved profiles with metadata.

    Returns list of dicts with name, description, author, jacked_version,
    created_at, and filename.
    """
    if not profiles_dir.exists():
        return []

    results = []
    for f in sorted(profiles_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append(
                {
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "author": data.get("author", ""),
                    "jacked_version": data.get("jacked_version", ""),
                    "created_at": data.get("created_at", ""),
                    "filename": f.name,
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return results


def delete_profile(name: str, profiles_dir: Path) -> bool:
    """Delete a profile by name.

    Returns True if deleted, False if not found.
    """
    filename = _name_to_filename(name)
    filepath = profiles_dir / filename
    if filepath.exists():
        filepath.unlink()
        return True
    return False
