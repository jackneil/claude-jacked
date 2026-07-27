"""Enable/disable engine for the jacked statusline.

Shared by the CLI installer, the `jacked statusline` command group, and
the dashboard Features toggle, so the registered command can never drift
between surfaces (the memory-vault setup module is the precedent).

Design contracts:
- The registered command is `"<abs-python>" -m jacked.statusline` with
  the absolute path of the interpreter running this code. Never a bare
  `python`/`python3`/`py` name: the statusline spawns through the OS
  shell, and name resolution is exactly what breaks across machines.
- A statusLine entry jacked did not write is FOREIGN. `jacked install`
  never replaces it. An explicit enable (dashboard toggle or
  `jacked statusline enable`) takes over but saves the foreign value in
  the state file; disable and uninstall restore it.
- Enable state is three-valued (enabled / disabled / unset) like skill
  packs: unset means "default on", and an explicit disable survives
  upgrades instead of being silently re-enabled.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from jacked.memory.settings_io import read_settings, settings_path, write_settings

STATE_PATH_NAME = "jacked-statusline.json"
STATE_VERSION = 1

# Substring that marks a statusLine command as jacked-managed. Both the
# current form ("<python>" -m jacked.statusline) and any future wrapper
# must keep it, or uninstall would strand the entry.
COMMAND_MARKER = "-m jacked.statusline"


def jacked_home() -> Path:
    """Home dir, honoring $JACKED_HOME (tests redirect it)."""
    return Path(os.environ.get("JACKED_HOME") or Path.home())


def state_path(home: Path) -> Path:
    return home / ".claude" / STATE_PATH_NAME


def build_command() -> str:
    """The statusLine command string for this installation.

    `sys.executable` is the uv tool venv's interpreter (or the dev venv's),
    already absolute on every platform. Quoting with plain double quotes is
    correct for both POSIX `sh -c` and Windows `cmd /c` spawns.
    """
    return f'"{sys.executable}" -m jacked.statusline'


def is_ours(command: object) -> bool:
    """True when a statusLine command string is jacked-managed.

    >>> is_ours('"/usr/bin/python3" -m jacked.statusline')
    True
    >>> is_ours("bash \\"$HOME/.claude/statusline.sh\\"")
    False
    >>> is_ours(None)
    False
    """
    return isinstance(command, str) and COMMAND_MARKER in command


def current_entry(settings: dict) -> Optional[dict]:
    """The statusLine entry when present and dict-shaped, else None."""
    entry = settings.get("statusLine")
    return entry if isinstance(entry, dict) else None


def entry_state(settings: dict) -> str:
    """Classify the statusLine key: "absent" | "ours" | "foreign"."""
    entry = settings.get("statusLine")
    if entry is None:
        return "absent"
    if isinstance(entry, dict) and is_ours(entry.get("command")):
        return "ours"
    return "foreign"


def ensure_entry(settings: dict, command: str) -> bool:
    """Register/refresh our statusLine entry in the settings dict.

    Returns True when the dict changed. Pure dict math -- no I/O -- so
    the CLI installer can apply it to its shared in-memory settings.
    """
    wanted = {"type": "command", "command": command}
    if settings.get("statusLine") == wanted:
        return False
    settings["statusLine"] = wanted
    return True


def remove_entry(settings: dict) -> bool:
    """Remove our statusLine entry. Never touches a foreign one."""
    if entry_state(settings) != "ours":
        return False
    del settings["statusLine"]
    return True


def load_state(home: Path) -> dict:
    """Read the state file, tolerating absence and corruption.

    Shape: {"version": 1, "state": "enabled"|"disabled"|"", "previous": dict|None}
    "" means unset -> the default (on) applies.
    """
    path = state_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    state = data.get("state")
    if state not in ("enabled", "disabled"):
        state = ""
    previous = data.get("previous")
    if not isinstance(previous, dict):
        previous = None
    return {"version": STATE_VERSION, "state": state, "previous": previous}


def save_state(home: Path, state: dict) -> None:
    """Atomic write with a writer-unique tmp (CLI/route/hook may race)."""
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{STATE_PATH_NAME}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_effectively_enabled(home: Path) -> bool:
    """Three-state resolution: explicit state wins, unset means on."""
    return load_state(home)["state"] != "disabled"


def enable(home: Path) -> dict:
    """Register the statusline, taking over a foreign entry with a backup.

    Raises SettingsUnreadableError (from settings_io) on a corrupt
    settings.json -- callers surface that rather than clobbering.
    """
    path = settings_path(home)
    settings = read_settings(path)
    took_over = None
    state = load_state(home)
    if entry_state(settings) == "foreign":
        took_over = settings["statusLine"]
        state["previous"] = (
            took_over if isinstance(took_over, dict) else {"value": took_over}
        )
    changed = ensure_entry(settings, build_command())
    if changed:
        write_settings(path, settings)
    state["state"] = "enabled"
    save_state(home, state)
    return {
        "changed": changed,
        "took_over_foreign": took_over is not None,
    }


def disable(home: Path) -> dict:
    """Remove our entry; restore a saved foreign entry when one exists.

    The backup restores even when our entry is already gone (removed out
    of band): the promise is "disable brings your old statusline back".
    Only a NEWER foreign entry supersedes the backup -- the user replaced
    the statusline themselves, so restoring over it would clobber their
    latest choice.
    """
    path = settings_path(home)
    settings = read_settings(path)
    state = load_state(home)
    where = entry_state(settings)
    changed = remove_entry(settings)
    restored = False
    if state["previous"] is not None and where in ("ours", "absent"):
        settings["statusLine"] = state["previous"]
        restored = True
        changed = True
    if changed:
        write_settings(path, settings)
    state["state"] = "disabled"
    state["previous"] = None
    save_state(home, state)
    return {"changed": changed, "restored_previous": restored}


def sync_on_install(home: Path, settings: dict) -> str:
    """Install-time sync against the CLI's shared in-memory settings dict.

    Returns what happened: "installed" | "migrated" | "unchanged" |
    "skipped_disabled" | "skipped_foreign". Pure dict math on `settings`;
    the caller owns the atomic write. Install never overrides an explicit
    disable and never touches a foreign entry (enabling over a foreign
    entry is reserved for the explicit toggle, which records a backup).
    """
    state = load_state(home)
    if state["state"] == "disabled":
        return "skipped_disabled"
    where = entry_state(settings)
    if where == "foreign":
        return "skipped_foreign"
    if where == "ours":
        return "migrated" if ensure_entry(settings, build_command()) else "unchanged"
    ensure_entry(settings, build_command())
    return "installed"


def remove_on_uninstall(home: Path, settings: dict) -> bool:
    """Uninstall-time removal against the shared settings dict.

    Restores a saved foreign entry and deletes the state file. Returns
    True when the settings dict changed. Same restore rule as disable():
    the backup comes back when our entry is present OR already gone, but
    never over a newer foreign entry the user chose themselves.
    """
    state = load_state(home)
    where = entry_state(settings)
    changed = remove_entry(settings)
    if state["previous"] is not None and where in ("ours", "absent"):
        settings["statusLine"] = state["previous"]
        changed = True
    try:
        state_path(home).unlink()
    except OSError:
        pass
    return changed
