"""Skill packs: jacked-curated pointers into upstream GitHub skills repos.

A pack is a named bundle of skills that live in a third-party repo. Install,
update and remove are orchestrated through the `npx skills` CLI
(vercel-labs/skills) so upstream stays the source of truth: jacked never
vendors the skill content, it only records which packs the user has enabled.

Hard-won rule baked in here (data-integrity doctrine): the `skills` CLI exits 0
even when it installs nothing (e.g. an upstream skill was renamed/removed), so
exit codes are never trusted. Every install/remove is VERIFIED against the
filesystem afterward and any drift is surfaced loudly.

Layout the CLI produces, and that we verify against:
  ~/.agents/skills/<name>/            canonical skill directory
  ~/.claude/skills/<name> -> ../../.agents/skills/<name>   relative symlink
  ~/.agents/.skill-lock.json          {"version":3,"skills":{<name>:{...}},...}

Our own enable-state (independent of what's on disk) lives at
  ~/.claude/jacked-packs.json         {"version":1,"enabled":{<name>:{...}}}
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jacked.findbin import find_bin

logger = logging.getLogger(__name__)

STATE_PATH_NAME = "jacked-packs.json"   # lives at home/.claude/jacked-packs.json

_NPX_MISSING_MSG = (
    "Node.js with npx is required for skill packs. "
    "Install Node 18+ (https://nodejs.org) and re-run."
)


@dataclass(frozen=True)
class Pack:
    name: str
    display_name: str
    description: str
    source: str
    homepage: str
    skills: tuple[str, ...]


@dataclass
class PackOpResult:
    ok: bool
    installed: list[str] = field(default_factory=list)   # verified present on disk after the op
    missing: list[str] = field(default_factory=list)     # expected but absent afterward -> loud failure
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # e.g. foreign-source skills we refused to remove
    message: str = ""                                    # human-readable summary or error


# --------------------------------------------------------------------------- #
# Registry (bundled data/packs.json)
# --------------------------------------------------------------------------- #

def load_registry(data_root: Path) -> dict[str, Pack]:
    """Load the pack registry from ``data_root/packs.json``.

    Returns ``{name: Pack}``. Tolerant of a missing or malformed file (logs a
    warning and returns ``{}``): a broken bundled registry degrades to "no
    packs" rather than crashing the dashboard.
    """
    path = Path(data_root) / "packs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Pack registry not found at %s", path)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable pack registry %s: %s", path, e)
        return {}

    packs: dict[str, Pack] = {}
    for name, spec in (raw.get("packs") or {}).items():
        packs[name] = Pack(
            name=name,
            display_name=spec.get("display_name", name),
            description=spec.get("description", ""),
            source=spec.get("source", ""),
            homepage=spec.get("homepage", ""),
            skills=tuple(spec.get("skills", [])),
        )
    return packs


# --------------------------------------------------------------------------- #
# Enable-state (home/.claude/jacked-packs.json)
# --------------------------------------------------------------------------- #

def _state_path(home: Path) -> Path:
    return Path(home) / ".claude" / STATE_PATH_NAME


def load_state(home: Path) -> dict:
    """Load enable-state. Returns ``{}`` if the file is missing; logs a warning
    and returns ``{}`` if it is present but corrupt."""
    path = _state_path(home)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable packs state %s: %s", path, e)
        return {}


def save_state(home: Path, state: dict) -> None:
    """Atomically write enable-state (tmp file in the same dir + os.replace)."""
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def set_enabled(home: Path, name: str, enabled: bool) -> None:
    """Mark a pack enabled (records enabled_at) or disabled (drops the entry)."""
    state = load_state(home)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    enabled_map = state.setdefault("enabled", {})
    if enabled:
        enabled_map[name] = {"enabled_at": _now_iso()}
    else:
        enabled_map.pop(name, None)
    save_state(home, state)


def enabled_pack_names(home: Path) -> list[str]:
    """Names of packs currently marked enabled, sorted."""
    state = load_state(home)
    enabled = state.get("enabled", {}) if isinstance(state, dict) else {}
    return sorted(enabled.keys())


# --------------------------------------------------------------------------- #
# npx skills orchestration
# --------------------------------------------------------------------------- #

def find_npx() -> str | None:
    """Locate the npx binary, or None when Node.js is not installed."""
    return find_bin("npx")


def read_lockfile(home: Path) -> dict:
    """Parse ``home/.agents/.skill-lock.json``. Returns ``{}`` if absent/corrupt."""
    path = Path(home) / ".agents" / ".skill-lock.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable skills lockfile %s: %s", path, e)
        return {}


def install_pack(
    pack: Pack, home: Path, *, include_codex: bool, timeout: int = 600
) -> PackOpResult:
    """Install every skill of ``pack`` via ``npx skills add`` and verify on disk."""
    npx = find_npx()
    if not npx:
        return PackOpResult(ok=False, missing=list(pack.skills), message=_NPX_MISSING_MSG)

    cmd = _add_command(npx, pack, include_codex=include_codex)
    ok, tail = _run_skills(cmd, timeout=timeout)
    if not ok:
        return PackOpResult(
            ok=False, missing=list(pack.skills),
            message=tail or "npx skills add failed.",
        )

    installed = [s for s in pack.skills if _skill_installed(home, s)]
    missing = [s for s in pack.skills if s not in installed]
    if missing:
        msg = (
            f"Installed {len(installed)}/{len(pack.skills)} skills for pack "
            f"'{pack.name}'. Missing after install: {', '.join(missing)}. "
            f"Upstream ({pack.source}) may have renamed or removed these skills; "
            "fix jacked/data/packs.json and run `jacked packs update`."
        )
        return PackOpResult(ok=False, installed=installed, missing=missing, message=msg)

    return PackOpResult(
        ok=True, installed=installed,
        message=f"Installed {len(installed)} skill(s) for pack '{pack.display_name}'.",
    )


def update_packs(
    packs: list[Pack], home: Path, *, include_codex: bool, timeout: int = 600
) -> PackOpResult:
    """Update all globally installed skills, then verify + repair each pack.

    Runs ``npx skills update`` once (it updates every source), then re-verifies
    every skill of every given pack. A pack with any missing skill gets ONE
    ``install_pack`` repair attempt and is re-verified. Results aggregate into a
    single PackOpResult.
    """
    npx = find_npx()
    if not npx:
        return PackOpResult(ok=False, message=_NPX_MISSING_MSG)

    ok, tail = _run_skills([npx, "-y", "skills", "update", "-g", "-y"], timeout=timeout)
    if not ok:
        return PackOpResult(ok=False, message=tail or "npx skills update failed.")

    all_installed: list[str] = []
    all_missing: list[str] = []
    notes: list[str] = []
    overall_ok = True

    for pack in packs:
        missing = [s for s in pack.skills if not _skill_installed(home, s)]
        if not missing:
            all_installed.extend(pack.skills)
            notes.append(f"{pack.name}: up to date ({len(pack.skills)} skill(s))")
            continue

        # One repair pass through the normal install path.
        install_pack(pack, home, include_codex=include_codex, timeout=timeout)
        installed = [s for s in pack.skills if _skill_installed(home, s)]
        still_missing = [s for s in pack.skills if s not in installed]
        all_installed.extend(installed)
        if still_missing:
            overall_ok = False
            all_missing.extend(still_missing)
            notes.append(f"{pack.name}: still missing after repair: {', '.join(still_missing)}")
        else:
            notes.append(f"{pack.name}: repaired {', '.join(missing)}")

    msg = "Updated skill packs. " + "; ".join(notes) if notes else "Updated skill packs."
    return PackOpResult(
        ok=overall_ok, installed=all_installed, missing=all_missing, message=msg,
    )


def remove_pack(pack: Pack, home: Path, *, timeout: int = 300) -> PackOpResult:
    """Remove a pack's skills via ``npx skills remove`` -- own-source skills only.

    Reads the lockfile first. A skill is removed only when its lock entry exists
    AND its recorded source matches ``pack.source``. Anything with a different
    source or no lock entry is left alone and reported in ``skipped`` (we never
    delete a same-named skill the user installed from elsewhere, and never rm
    anything ourselves -- all removal goes through the skills CLI).
    """
    lock = read_lockfile(home)
    lock_skills = lock.get("skills", {}) if isinstance(lock, dict) else {}

    to_remove: list[str] = []
    skipped: list[str] = []
    for name in pack.skills:
        entry = lock_skills.get(name)
        if isinstance(entry, dict) and entry.get("source") == pack.source:
            to_remove.append(name)
        else:
            skipped.append(name)

    if not to_remove:
        msg = (
            f"Nothing removed for pack '{pack.name}': no skills on disk are "
            f"tracked as installed from {pack.source}."
        )
        if skipped:
            msg += f" Skipped (different source or not tracked): {', '.join(skipped)}."
        return PackOpResult(ok=True, skipped=skipped, message=msg)

    npx = find_npx()
    if not npx:
        return PackOpResult(ok=False, skipped=skipped, message=_NPX_MISSING_MSG)

    cmd = [npx, "-y", "skills", "remove", *to_remove, "-g", "-y"]
    ok, tail = _run_skills(cmd, timeout=timeout)
    if not ok:
        return PackOpResult(
            ok=False, skipped=skipped, message=tail or "npx skills remove failed.",
        )

    removed = [s for s in to_remove if not _skill_present(home, s)]
    still = [s for s in to_remove if s not in removed]
    if still:
        msg = (
            f"Removed {len(removed)} skill(s) for pack '{pack.name}', but these "
            f"still exist on disk after removal: {', '.join(still)}."
        )
        return PackOpResult(ok=False, removed=removed, skipped=skipped, message=msg)

    msg = f"Removed {len(removed)} skill(s) for pack '{pack.display_name}'."
    if skipped:
        msg += f" Skipped (different source or not tracked): {', '.join(skipped)}."
    return PackOpResult(ok=True, removed=removed, skipped=skipped, message=msg)


def pack_status(pack: Pack, home: Path) -> dict:
    """Per-skill install/source status for one pack, for the dashboard + CLI."""
    lock = read_lockfile(home)
    lock_skills = lock.get("skills", {}) if isinstance(lock, dict) else {}

    skills: list[dict] = []
    installed_count = 0
    for name in pack.skills:
        installed = (Path(home) / ".claude" / "skills" / name / "SKILL.md").exists()
        entry = lock_skills.get(name)
        if isinstance(entry, dict):
            source_ok: bool | None = entry.get("source") == pack.source
            updated_at = entry.get("updatedAt") or entry.get("installedAt")
        else:
            source_ok = None
            updated_at = None
        if installed:
            installed_count += 1
        skills.append({
            "name": name,
            "installed": installed,
            "source_ok": source_ok,
            "updated_at": updated_at,
        })

    return {
        "name": pack.name,
        "display_name": pack.display_name,
        "description": pack.description,
        "homepage": pack.homepage,
        "source": pack.source,
        "skills": skills,
        "installed_count": installed_count,
        "total": len(pack.skills),
    }


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_command(npx: str, pack: Pack, *, include_codex: bool) -> list[str]:
    agents = ["claude-code"]
    if include_codex:
        agents.append("codex")
    return [
        npx, "-y", "skills", "add", pack.source,
        "--skill", *pack.skills,
        "-g", "-y", "-a", *agents,
    ]


def _skill_installed(home: Path, name: str) -> bool:
    """A skill is installed when BOTH the canonical dir and the claude-side
    symlink resolve to a SKILL.md (Path.exists follows symlinks)."""
    home = Path(home)
    claude_link = home / ".claude" / "skills" / name / "SKILL.md"
    canonical = home / ".agents" / "skills" / name / "SKILL.md"
    return claude_link.exists() and canonical.exists()


def _skill_present(home: Path, name: str) -> bool:
    """True if any trace of the skill remains (dir, file, or dangling symlink)."""
    home = Path(home)
    claude_link = home / ".claude" / "skills" / name
    canonical = home / ".agents" / "skills" / name
    return (
        claude_link.exists() or claude_link.is_symlink()
        or canonical.exists() or canonical.is_symlink()
    )


def _tail(text: str, n: int = 500) -> str:
    return (text or "").strip()[-n:]


def _run_skills(cmd: list[str], *, timeout: int) -> tuple[bool, str]:
    """Run one npx-skills subprocess. Returns (ok, message_tail).

    ok is True only on a clean exit 0. On non-zero exit or timeout it returns
    (False, <last ~500 chars of stderr/stdout>). Never raises for flow control;
    os.environ is inherited untouched (npx needs the real HOME/PATH).
    """
    logger.info("Running skills command: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = _tail((e.stderr or "") + (e.stdout or ""))
        logger.warning("skills command timed out after %ss: %s", timeout, " ".join(cmd))
        return False, f"Timed out after {timeout}s. {out}".strip()
    except OSError as e:
        logger.warning("skills command failed to launch: %s", e)
        return False, f"Failed to run npx: {e}"

    if proc.returncode != 0:
        out = _tail((proc.stderr or "") + (proc.stdout or ""))
        logger.warning("skills command exited %s: %s", proc.returncode, out)
        return False, f"npx skills exited {proc.returncode}. {out}".strip()
    return True, ""
