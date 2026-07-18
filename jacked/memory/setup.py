"""Memory-vault enable/disable engine, shared by the dashboard and the CLI.

The dashboard Features toggle (``jacked/api/routes/features.py``) flips the
memory vault through here so the enable/disable wiring lives in exactly one
place. ``enable`` initializes the vault NON-INTERACTIVELY when it is missing,
records the enabled flag, installs the capture + recall settings.json hook
entries, installs the post-merge git hooks into the vault's mapped repos, and
counts un-migrated ``.remember`` dirs. ``disable`` removes our settings entries
and our git hooks and clears the enabled flag -- it NEVER touches vault files.

Everything resolves through ``vault.jacked_home()`` / ``vault.vault_dir()`` so
``$JACKED_HOME`` / ``$JACKED_VAULT_DIR`` fully isolate tests. No FastAPI, no
Rich: the functions return plain dicts describing what changed. The interactive
group-review path stays in ``jacked memory init``; this engine auto-accepts the
suggested groups so a one-click dashboard toggle never blocks on a prompt.

The hook-entry math is delegated to ``memory.hooks_config`` (the single source
both this engine and the CLI installer share); the hook command strings come
from the CLI's canonical ``_build_hook_command`` so the dashboard and
``jacked install`` never drift on the binary path they register.
"""
from __future__ import annotations

import logging
from pathlib import Path

from jacked.memory import githook, hooks_config, settings_io
from jacked.memory import migrate as migrate_mod
from jacked.memory import vault as vault_mod

logger = logging.getLogger(__name__)

# The two hook module names, anchored in the settings.json command strings the
# installer writes. Capture is the async SessionEnd/PreCompact pair; recall is
# the synchronous SessionStart brief.
_CAPTURE_HOOK = "memory_capture"
_RECALL_HOOK = "memory_recall"


# --------------------------------------------------------------------------- #
# settings.json I/O (delegated to the shared, corruption-safe module)
# --------------------------------------------------------------------------- #

def _hook_command(hook_name: str) -> str:
    """The settings.json command for a jacked hook, via the CLI's canonical
    builder (upgrade-safe binary path). Imported lazily so this module never
    pulls the heavy CLI at load time (and never risks an import cycle with the
    Features route, which imports this module)."""
    from jacked.cli import _build_hook_command

    return _build_hook_command(hook_name)


# --------------------------------------------------------------------------- #
# Root resolution
# --------------------------------------------------------------------------- #

def _default_roots(home: Path) -> list[Path]:
    """Non-interactive init roots: ``<home>/Github`` when it exists, else none.

    A missing Github dir yields an empty vault (groups are created on the fly as
    notes land); a server process never scans an arbitrary cwd. The interactive
    ``jacked memory init`` remains the path that lets a user pick other roots.
    """
    gh = Path(home) / "Github"
    return [gh] if gh.is_dir() else []


def _config_roots(vault: Path) -> list[Path]:
    """The roots recorded in the vault's vault.json (for git-hook + migration
    discovery once the vault exists)."""
    cfg = vault_mod.load_vault_config(vault)
    return [Path(r) for r in (cfg.get("roots") or [])]


# --------------------------------------------------------------------------- #
# enable / disable
# --------------------------------------------------------------------------- #

def enable(home: Path | None = None) -> dict:
    """Turn the memory vault on. Idempotent.

    Steps: init the vault non-interactively if missing (scan ``<home>/Github``,
    auto-accept the suggested groups), record the enabled flag, install the
    capture (async) + recall (sync) settings.json entries, install our
    post-merge git hooks into every mapped repo (refusals surfaced, never
    forced), and count migratable ``.remember`` dirs. Returns::

        {vault_path, initialized_now, groups: {group: [repo...]},
         hooks_installed: [...], git_hooks: {identity: {...}},
         migration_available: int}
    """
    home = vault_mod.jacked_home() if home is None else Path(home)
    vault = vault_mod.vault_dir()

    initialized_now = False
    if not vault_mod.is_initialized(vault):
        roots = _default_roots(home)
        discovered = vault_mod.scan_roots(roots)
        groups = vault_mod.suggest_groups(discovered)  # auto-accept the suggestion
        vault_mod.init_vault(vault, [str(r) for r in roots], groups)
        initialized_now = True

    vault_mod.set_enabled(home, True, vault=vault)

    # Capture (async pair) + recall (sync) settings.json entries. A corrupt
    # settings.json raises SettingsUnreadableError here and aborts the enable
    # LOUDLY rather than clobbering the user's hooks with a fresh file.
    settings_path = settings_io.settings_path(home)
    settings = settings_io.read_settings(settings_path)
    changed = hooks_config.ensure_capture_entries(settings, _hook_command(_CAPTURE_HOOK))
    changed = hooks_config.ensure_recall_entry(settings, _hook_command(_RECALL_HOOK)) or changed
    if changed:
        settings_io.write_settings(settings_path, settings)

    # Post-merge git hooks into every mapped repo (foreign hooks refused, not forced).
    roots = _config_roots(vault)
    git_hooks = githook.install_for_mapped_repos(vault, roots)

    migration_available = len(migrate_mod.discover_remember_dirs(roots))

    cfg = vault_mod.load_vault_config(vault)
    groups_summary = {
        g: info.get("repos", []) for g, info in (cfg.get("groups") or {}).items()
    }

    return {
        "vault_path": str(vault),
        "initialized_now": initialized_now,
        "groups": groups_summary,
        "hooks_installed": [_CAPTURE_HOOK, _RECALL_HOOK],
        "git_hooks": git_hooks,
        "migration_available": migration_available,
    }


def disable(home: Path | None = None) -> dict:
    """Turn the memory vault off. Idempotent.

    Removes our capture + recall settings.json entries and our post-merge git
    hooks (marker-verified; foreign hooks are never touched), then clears the
    enabled flag. The vault itself -- its git repo and every note -- is LEFT
    COMPLETELY INTACT, so disable is fully reversible by re-enabling. Returns
    ``{vault_path, hooks_removed, git_hooks: {identity: {...}}, enabled: False}``.
    """
    home = vault_mod.jacked_home() if home is None else Path(home)
    vault = vault_mod.vault_dir()

    settings_path = settings_io.settings_path(home)
    settings = settings_io.read_settings(settings_path)
    changed = hooks_config.remove_capture_entries(settings)
    changed = hooks_config.remove_recall_entries(settings) or changed
    if changed:
        settings_io.write_settings(settings_path, settings)

    # Only mappable when a vault exists to read repo_map from. Never touches
    # vault content -- only the repos' .git/hooks/post-merge that WE installed.
    git_hooks: dict = {}
    if vault_mod.is_initialized(vault):
        git_hooks = githook.remove_for_mapped_repos(vault, _config_roots(vault))

    vault_mod.set_enabled(home, False)

    return {
        "vault_path": str(vault),
        "hooks_removed": changed,
        "git_hooks": git_hooks,
        "enabled": False,
    }
