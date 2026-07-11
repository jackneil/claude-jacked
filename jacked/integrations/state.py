"""Read/write the agent-reach install-state file (``~/.claude/jacked-reach-state.json``).

Extracted from the runner so ``agent_reach.py`` stays under the per-file line
guardrail. Free functions over a ``home`` dir; atomic writes via
``_util.atomic_write_json``; a corrupt/partial file reads as None (never raises).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from jacked.integrations._util import atomic_write_json, now_iso

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "jacked-reach-state.json"


def state_path(home: Path) -> Path:
    return home / ".claude" / STATE_FILE_NAME


def read_state(home: Path) -> dict | None:
    try:
        return json.loads(state_path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("ignoring unreadable reach state file: %s", e)
        return None


def existing_channels(home: Path) -> list[str]:
    state = read_state(home)
    channels = state.get("channels_enabled") if state else None
    return list(channels) if isinstance(channels, list) else []


def write_state(home: Path, *, installed_sha: str, override_active: bool, channels_enabled: list[str]) -> None:
    # Read-modify-write: update the install-managed keys but PRESERVE any others
    # (e.g. channel_specs recorded by record_channel), so a re-pin during the same
    # install() is not wiped by the final write, and no key is silently dropped.
    state = read_state(home) or {}
    state.update({
        "installed_sha": installed_sha,
        "installed_at": now_iso(),
        "skill_hashes_verified": True,
        "channels_enabled": list(channels_enabled),
        "override_active": bool(override_active),
    })
    atomic_write_json(state_path(home), state)


def delete_state(home: Path) -> None:
    try:
        state_path(home).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not remove reach state file: %s", e)


def record_channel(home: Path, name: str, specs: list[str]) -> None:
    """Record an enabled channel's NAME and the exact pinned SPECS installed."""
    state = read_state(home) or {}
    enabled = state.get("channels_enabled")
    enabled = list(enabled) if isinstance(enabled, list) else []
    if name not in enabled:
        enabled.append(name)
    state["channels_enabled"] = sorted(enabled)
    versions = state.get("channel_specs")
    versions = dict(versions) if isinstance(versions, dict) else {}
    versions[name] = list(specs)
    state["channel_specs"] = versions
    atomic_write_json(state_path(home), state)
