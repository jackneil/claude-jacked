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

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jacked.findbin import find_bin

logger = logging.getLogger(__name__)

STATE_PATH_NAME = "jacked-packs.json"   # lives at home/.claude/jacked-packs.json

# Pin the skills CLI to an empirically verified version. The behavioral
# contracts jacked relies on -- the on-disk layout, the lockfile v3 schema, the
# argv flags, and the agent names -- were verified against this exact version.
# Bump ONLY deliberately, and only with a fresh re-verification pass against
# those contracts; a silent floating version is how the rc=0 gotcha and the
# lockfile source checks would drift out from under us.
SKILLS_CLI_SPEC = "skills@1.5.17"

_NPX_MISSING_MSG = (
    "Node.js with npx is required for skill packs. "
    "Install Node 18+ (https://nodejs.org) and re-run."
)

# Structural validation for the bundled registry (defense in depth: the values
# below flow straight onto the skills-CLI argv, so a stray "-g" or "../evil"
# must never reach a subprocess). Each source segment must START alphanumeric:
# that anchors out leading "-" (argv flag smuggling) and leading "." ("..",
# ".git" and friends) in one stroke.
# \Z (not $) so a trailing newline can't sneak into a value: $ matches before a
# final \n, and while a list-argv/shell=False call makes that non-exploitable,
# an airtight contract costs nothing here.
_SOURCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z"
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")


@dataclass(frozen=True)
class Pack:
    name: str
    display_name: str
    description: str
    source: str
    homepage: str
    skills: tuple[str, ...]
    default: bool = False  # installed by a plain `jacked install` unless opted out


@dataclass
class PackOpResult:
    ok: bool
    installed: list[str] = field(default_factory=list)   # verified present on disk after the op
    missing: list[str] = field(default_factory=list)     # expected but absent afterward -> loud failure
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # e.g. foreign-source skills we refused to touch
    message: str = ""                                    # human-readable summary or error
    per_pack: dict = field(default_factory=dict)         # name -> PackOpResult (update_packs attribution)


# --------------------------------------------------------------------------- #
# Home resolution
# --------------------------------------------------------------------------- #

def jacked_home() -> Path:
    """Resolve jacked's home dir (the ~/.claude / ~/.agents parent).

    Mirrors ``cli._jacked_home`` so every pack surface (CLI, API) resolves the
    same home: honors ``$JACKED_HOME`` and otherwise falls back to the real
    home directory.
    """
    return Path(os.getenv("JACKED_HOME") or Path.home())


# --------------------------------------------------------------------------- #
# Registry (bundled data/packs.json)
# --------------------------------------------------------------------------- #

def load_registry(data_root: Path) -> dict[str, Pack]:
    """Load the pack registry from ``data_root/packs.json``.

    Returns ``{name: Pack}``. Tolerant of a missing or malformed file (logs a
    warning and returns ``{}``): a broken bundled registry degrades to "no
    packs" rather than crashing the dashboard.

    Every pack is structurally validated before it is admitted: ``source`` must
    look like ``owner/repo``, ``skills`` must be a list of safe skill names, and
    ``homepage`` must be empty or an ``https://`` URL. A pack that fails any
    check is skipped with a warning naming it and the reason -- the registry is
    the argv source for the skills CLI, so hostile or malformed values never get
    the chance to reach a subprocess.
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

    raw_packs = raw.get("packs") if isinstance(raw, dict) else None
    if raw_packs is None:
        raw_packs = {}
    if not isinstance(raw_packs, dict):
        logger.warning(
            "Pack registry %s: 'packs' is not an object (%s); ignoring registry",
            path, type(raw_packs).__name__,
        )
        return {}

    packs: dict[str, Pack] = {}
    for name, spec in raw_packs.items():
        if not isinstance(spec, dict):
            logger.warning("Skipping pack %r: entry is not an object", name)
            continue
        source = spec.get("source", "")
        skills = spec.get("skills", [])
        homepage = spec.get("homepage", "")
        if not (isinstance(source, str) and _SOURCE_RE.match(source)):
            logger.warning(
                "Skipping pack %r: invalid source %r (want 'owner/repo')", name, source
            )
            continue
        if not isinstance(skills, list):
            logger.warning(
                "Skipping pack %r: 'skills' is not a list (%s)", name, type(skills).__name__
            )
            continue
        bad = [s for s in skills if not (isinstance(s, str) and _SKILL_NAME_RE.match(s))]
        if bad:
            logger.warning("Skipping pack %r: invalid skill name(s): %s", name, bad)
            continue
        if not (isinstance(homepage, str) and (homepage == "" or homepage.startswith("https://"))):
            logger.warning(
                "Skipping pack %r: invalid homepage %r (want '' or an https:// URL)",
                name, homepage,
            )
            continue
        # `default` must be a real bool, not a truthy coercion: a stray
        # "default": "false" (a non-empty string) would otherwise silently flip
        # a pack ON. Fall back to opt-in (False) with a warning rather than
        # dropping the whole pack.
        default_raw = spec.get("default", False)
        if not isinstance(default_raw, bool):
            logger.warning(
                "Pack %r: 'default' is not a bool (%r); treating as false (opt-in)",
                name, default_raw,
            )
            default_raw = False
        packs[name] = Pack(
            name=name,
            display_name=spec.get("display_name", name),
            description=spec.get("description", ""),
            source=source,
            homepage=homepage,
            skills=tuple(skills),
            default=default_raw,
        )
    return packs


# --------------------------------------------------------------------------- #
# Enable-state (home/.claude/jacked-packs.json)
# --------------------------------------------------------------------------- #

def _state_path(home: Path) -> Path:
    return Path(home) / ".claude" / STATE_PATH_NAME


STATE_VERSION = 2


def load_state(home: Path) -> dict:
    """Load pack state, normalized to the current v2 shape.

    v2 shape: ``{"version": 2, "packs": {name: {"state": "enabled"|"disabled",
    "at": iso}}}``. A pack with NO entry has made no explicit decision -- the
    registry's ``default`` flag decides whether it installs (see
    ``is_effectively_enabled``). This three-state model (enabled / disabled /
    unset) is what lets default-on packs be durably opted out: a plain "drop the
    entry on disable" (the v1 behavior) would make "disabled" and "never
    decided" indistinguishable, so a default-on pack the user removed would
    silently reinstall on the next ``jacked install``.

    v1 files (``{"version": 1, "enabled": {name: {enabled_at}}}``) are migrated
    in memory: each enabled entry becomes ``state: "enabled"``. v1 had no
    disabled state (disable dropped the entry), so nothing maps to disabled. The
    migrated shape is only persisted on the next ``set_enabled`` write.

    Returns an empty-but-valid v2 dict if the file is missing, and logs a
    warning + returns empty-but-valid if it is present but corrupt.
    """
    empty = {"version": STATE_VERSION, "packs": {}}
    path = _state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable packs state %s: %s", path, e)
        return empty
    return _normalize_state(raw)


def _normalize_state(raw: object) -> dict:
    """Coerce any on-disk state shape (v1, v2, or garbage) to the v2 shape.

    A file written by a NEWER jacked (version > STATE_VERSION) is handled
    forward-compatibly rather than silently truncated: unknown top-level keys
    and unknown per-entry fields are PRESERVED across the load->mutate->save
    round-trip, and a warning is logged (mirroring read_lockfile's version
    guard). Only entries whose ``state`` this build can interpret
    (enabled/disabled) are trusted by consumers; an entry with a state value we
    don't recognize is kept on disk but treated as "no decision" here.
    """
    if not isinstance(raw, dict):
        return {"version": STATE_VERSION, "packs": {}}
    version = raw.get("version")
    if isinstance(version, int) and version > STATE_VERSION:
        logger.warning(
            "packs state version %s is newer than this build understands (%s); "
            "preserving unknown fields, treating unrecognized states as absent",
            version, STATE_VERSION,
        )
    packs_map = raw.get("packs")
    if isinstance(packs_map, dict):
        # v2+: preserve unknown top-level keys, unknown per-entry fields, AND
        # entries whose `state` this build doesn't recognize (a future third
        # state like "paused"). Drop only genuine garbage (a non-dict entry, or
        # a state that isn't even a string). An unrecognized state is kept on
        # disk here and treated as "no explicit decision" by is_effectively_enabled
        # so it is neither honored blindly nor silently destroyed.
        out = dict(raw)
        clean: dict = {}
        for name, entry in packs_map.items():
            if isinstance(entry, dict) and isinstance(entry.get("state"), str):
                clean[name] = entry
        out["packs"] = clean
        # Keep the file's true (newer) version so a future reader keys its
        # migration off the real schema, not a downgraded label.
        out["version"] = (
            version if (isinstance(version, int) and version > STATE_VERSION)
            else STATE_VERSION
        )
        return out
    # v1: {"enabled": {name: {enabled_at}}} -> everything enabled, none disabled.
    v1_enabled = raw.get("enabled")
    packs: dict = {}
    if isinstance(v1_enabled, dict):
        for name, entry in v1_enabled.items():
            at = entry.get("enabled_at") if isinstance(entry, dict) else None
            packs[name] = {"state": "enabled", "at": at}
    return {"version": STATE_VERSION, "packs": packs}


def save_state(home: Path, state: dict) -> None:
    """Atomically write enable-state via a writer-unique tmp + os.replace.

    The tmp name must be unique per writer: the CLI and the dashboard service
    are separate processes that can both call set_enabled against the same
    home. A shared tmp path lets one writer os.replace the other's half-written
    file away (FileNotFoundError for the loser, or a torn file that load_state
    silently degrades to {}, wiping enable-state).
    """
    path = _state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def set_enabled(home: Path, name: str, enabled: bool) -> None:
    """Record an EXPLICIT enable/disable decision for a pack, durably.

    Both directions write a state entry -- disable records ``state: "disabled"``
    rather than dropping the entry, so the decision survives future
    ``jacked install`` runs and a default-on pack the user removed is not
    silently reinstalled.
    """
    state = load_state(home)  # already normalized to v2
    packs_map = state.setdefault("packs", {})
    packs_map[name] = {"state": "enabled" if enabled else "disabled", "at": _now_iso()}
    save_state(home, state)


def pack_state(home: Path, name: str) -> str | None:
    """The recorded state string for a pack, or ``None`` if no entry exists.

    Normally ``"enabled"`` / ``"disabled"`` / ``None`` (no decision -> registry
    default applies). May also return a state string a NEWER jacked wrote that
    this build doesn't recognize; is_effectively_enabled treats any such value
    as "no decision" (falls through to the default)."""
    entry = load_state(home).get("packs", {}).get(name)
    return entry.get("state") if isinstance(entry, dict) else None


def is_effectively_enabled(pack: Pack, home: Path) -> bool:
    """Whether ``pack`` should be installed: a recognized explicit decision
    wins, and otherwise the registry ``default`` flag decides.

    A state value this build doesn't recognize (a future third state preserved
    on disk) is NOT treated as disabled -- it falls through to the registry
    default, matching the "no decision this build can interpret" contract in
    _normalize_state. A future jacked that understands the state re-honors it.
    """
    state = pack_state(home, pack.name)
    if state == "enabled":
        return True
    if state == "disabled":
        return False
    return pack.default


def enabled_pack_names(home: Path) -> list[str]:
    """Names of packs with an EXPLICIT ``enabled`` decision, sorted. Does NOT
    include default-on packs that were never explicitly toggled -- use
    ``effective_enabled_pack_names`` for "what should be installed"."""
    packs_map = load_state(home).get("packs", {})
    return sorted(
        n for n, e in packs_map.items()
        if isinstance(e, dict) and e.get("state") == "enabled"
    )


def effective_enabled_pack_names(home: Path, registry: dict[str, "Pack"]) -> list[str]:
    """Registry packs that should be installed right now, sorted: every pack
    whose effective state is enabled (explicit ``enabled``, or unset with
    ``default: true``). Packs in the state file but absent from the registry are
    ignored here (handled loudly by the callers that surface deregistration)."""
    return sorted(n for n, p in registry.items() if is_effectively_enabled(p, home))


# --------------------------------------------------------------------------- #
# npx skills orchestration
# --------------------------------------------------------------------------- #

def find_npx() -> str | None:
    """Locate the npx binary, or None when Node.js is not installed."""
    return find_bin("npx")


def read_lockfile(home: Path) -> dict:
    """Parse ``home/.agents/.skill-lock.json``. Returns ``{}`` if absent/corrupt.

    Emits a warning if the lockfile schema version is anything other than 3:
    the source-attribution checks (collision guard, own-source update/remove)
    are written against the v3 shape, so a different version means those checks
    may misfire and the pinned skills CLI contract needs re-verification.
    """
    path = Path(home) / ".agents" / ".skill-lock.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable skills lockfile %s: %s", path, e)
        return {}
    if isinstance(data, dict) and data.get("version") != 3:
        logger.warning(
            "skills lockfile version %s != 3; source checks may misfire. "
            "Re-verify the skills CLI contract.",
            data.get("version"),
        )
    return data


def install_pack(
    pack: Pack, home: Path, *, include_codex: bool, timeout: int = 600
) -> PackOpResult:
    """Install every skill of ``pack`` via ``npx skills add`` and verify on disk.

    Collision guard (confirmed user-data-loss: ``skills add`` clobbers a
    pre-existing user-owned ~/.claude/skills/<name> with no warning): before
    running anything, each skill is partitioned against the lockfile + disk.
    A skill is ELIGIBLE only when it has no trace on disk, or its lockfile entry
    records this pack's own source (a safe reinstall/refresh). Any other trace
    on disk (a user's own dir, or a same-named skill from a different source) is
    SKIPPED and never handed to the CLI. Skips do not fail the op, but are
    always surfaced loudly.
    """
    lock = read_lockfile(home)
    lock_skills = lock.get("skills", {}) if isinstance(lock, dict) else {}

    eligible: list[str] = []
    skipped: list[str] = []
    for name in pack.skills:
        entry = lock_skills.get(name)
        own_source = isinstance(entry, dict) and entry.get("source") == pack.source
        if not _skill_present(home, name) or own_source:
            eligible.append(name)
        else:
            skipped.append(name)

    skip_msg = ""
    if skipped:
        skip_msg = (
            f"Refusing to overwrite existing skill(s) not installed from "
            f"{pack.source}: {', '.join(skipped)}. Move or remove them first, "
            "then run `jacked packs update`."
        )

    # Nothing to install (all skills already exist from another owner). Skips
    # never fail the op, so report success while still surfacing them loudly.
    if not eligible:
        return PackOpResult(
            ok=True, skipped=skipped,
            message=skip_msg or f"Nothing to install for pack '{pack.display_name}'.",
        )

    npx = find_npx()
    if not npx:
        return PackOpResult(
            ok=False, missing=list(eligible), skipped=skipped,
            message=_join(_NPX_MISSING_MSG, skip_msg),
        )

    cmd = _add_command(npx, pack, eligible, include_codex=include_codex)
    ok, tail = _run_skills(cmd, home=home, timeout=timeout)
    if not ok:
        return PackOpResult(
            ok=False, missing=list(eligible), skipped=skipped,
            message=_join(tail or "npx skills add failed.", skip_msg),
        )

    installed = [s for s in eligible if _skill_installed(home, s)]
    missing = [s for s in eligible if s not in installed]
    if missing:
        return PackOpResult(
            ok=False, installed=installed, missing=missing, skipped=skipped,
            message=_join(_missing_after_install_msg(pack, installed, missing, eligible), skip_msg),
        )

    return PackOpResult(
        ok=True, installed=installed, skipped=skipped,
        message=_join(
            f"Installed {len(installed)} skill(s) for pack '{pack.display_name}'.",
            skip_msg,
        ),
    )


def update_packs(
    packs: list[Pack], home: Path, *, include_codex: bool, timeout: int = 600
) -> PackOpResult:
    """Update this pack-set's own skills, then verify + repair each pack.

    Only skills whose lockfile entry records the owning pack's source are passed
    to ``skills update`` -- we never update another source's same-named skill,
    and never touch an unmanaged directory. The scoped update runs once; then
    each pack is verified independently and, if it has any missing skill, gets
    ONE ``install_pack`` repair (which itself honors the collision guard) and is
    re-verified. Each pack's own outcome is recorded in ``per_pack``; the
    aggregate ``ok`` is the AND of every pack's ok.
    """
    npx = find_npx()
    if not npx:
        return PackOpResult(ok=False, message=_NPX_MISSING_MSG)

    lock = read_lockfile(home)
    lock_skills = lock.get("skills", {}) if isinstance(lock, dict) else {}

    # Scope the update: only CLI-managed skills whose recorded source matches
    # the pack that claims them. De-dup while preserving order.
    update_names: list[str] = []
    seen: set[str] = set()
    for pack in packs:
        for name in pack.skills:
            entry = lock_skills.get(name)
            if (
                isinstance(entry, dict)
                and entry.get("source") == pack.source
                and name not in seen
            ):
                update_names.append(name)
                seen.add(name)

    if update_names:
        ok, tail = _run_skills(
            [npx, "-y", SKILLS_CLI_SPEC, "update", *update_names, "-g", "-y"],
            home=home, timeout=timeout,
        )
        if not ok:
            # The update command failed wholesale: we cannot vouch for the
            # freshness of ANY pack, so every pack is reported failed.
            msg = tail or "npx skills update failed."
            per_pack = {p.name: PackOpResult(ok=False, message=msg) for p in packs}
            return PackOpResult(ok=False, message=msg, per_pack=per_pack)

    per_pack: dict[str, PackOpResult] = {}
    notes: list[str] = []
    all_installed: list[str] = []
    all_missing: list[str] = []
    overall_ok = True

    for pack in packs:
        missing = [s for s in pack.skills if not _skill_installed(home, s)]
        if not missing:
            res = PackOpResult(
                ok=True, installed=list(pack.skills),
                message=f"{pack.name}: up to date ({len(pack.skills)} skill(s))",
            )
        else:
            # One repair pass through the normal install path (collision-guarded).
            repair = install_pack(pack, home, include_codex=include_codex, timeout=timeout)
            installed = [s for s in pack.skills if _skill_installed(home, s)]
            still_missing = [s for s in pack.skills if s not in installed]
            if still_missing:
                res = PackOpResult(
                    ok=False, installed=installed, missing=still_missing,
                    skipped=repair.skipped,
                    message=f"{pack.name}: still missing after repair: {', '.join(still_missing)}",
                )
            else:
                res = PackOpResult(
                    ok=True, installed=installed, skipped=repair.skipped,
                    message=f"{pack.name}: repaired {', '.join(missing)}",
                )
        per_pack[pack.name] = res
        notes.append(res.message)
        all_installed.extend(res.installed)
        all_missing.extend(res.missing)
        overall_ok = overall_ok and res.ok

    msg = "Updated skill packs. " + "; ".join(notes) if notes else "Updated skill packs."
    return PackOpResult(
        ok=overall_ok, installed=all_installed, missing=all_missing,
        message=msg, per_pack=per_pack,
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

    cmd = [npx, "-y", SKILLS_CLI_SPEC, "remove", *to_remove, "-g", "-y"]
    ok, tail = _run_skills(cmd, home=home, timeout=timeout)
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
        installed = _skill_installed(home, name)
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


def _join(*parts: str) -> str:
    """Join non-empty message fragments with a single space."""
    return " ".join(p for p in parts if p)


def _missing_after_install_msg(
    pack: Pack, installed: list[str], missing: list[str], attempted: list[str]
) -> str:
    """End-user-facing message when the CLI exits 0 but skills are still absent."""
    return (
        f"Installed {len(installed)}/{len(attempted)} skills for pack "
        f"'{pack.name}'. Missing: {', '.join(missing)}. These may have been "
        f"renamed or removed upstream ({pack.source}). Run `jacked packs update` "
        "to retry; if it persists, upgrade jacked or report it at "
        "https://github.com/jackneil/claude-jacked/issues."
    )


def _add_command(npx: str, pack: Pack, names: list[str], *, include_codex: bool) -> list[str]:
    agents = ["claude-code"]
    if include_codex:
        agents.append("codex")
    return [
        npx, "-y", SKILLS_CLI_SPEC, "add", pack.source,
        "--skill", *names,
        "-g", "-y", "-a", *agents,
    ]


def _skill_installed(home: Path, name: str) -> bool:
    """A skill is installed (for the always-targeted claude-code agent) when its
    claude-side entry resolves to a SKILL.md.

    The skills CLI produces two on-disk layouts depending on the target and the
    prior state of ~/.claude/skills:
      - a real ~/.agents/skills/<name>/ canonical + a ~/.claude/skills/<name>
        symlink into it (universal / codex layout, or a fresh empty skills dir);
      - a real ~/.claude/skills/<name>/ directory with NO ~/.agents canonical
        (claude-code-only install into an already-populated skills dir -- what a
        normal `jacked install` produces, since it seeds ~/.claude/skills with
        the vendored suite first).
    Both are usable by Claude Code, which reads ~/.claude/skills/<name>/SKILL.md.
    ``Path.exists()`` follows the symlink, so this one check covers both layouts;
    requiring the ~/.agents canonical too (an artifact of only the first layout)
    was a false-negative that made every real install report the pack missing
    and re-run its install forever.
    """
    return (Path(home) / ".claude" / "skills" / name / "SKILL.md").exists()


def _skill_present(home: Path, name: str) -> bool:
    """True if any trace of the skill remains (dir, file, or dangling symlink)."""
    home = Path(home)
    claude_link = home / ".claude" / "skills" / name
    canonical = home / ".agents" / "skills" / name
    return (
        claude_link.exists() or claude_link.is_symlink()
        or canonical.exists() or canonical.is_symlink()
    )


# Control chars except tab/newline/carriage-return. npx stderr is upstream
# controlled; a compromised package could embed terminal escape/OSC sequences
# (screen-clear, OSC-52 clipboard write, OSC-8 hyperlink) that fire when the
# tail is later cat'd from the log. Strip them at the source so both the log
# sink and the console print carry inert text.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _tail(text: str, n: int = 500) -> str:
    return _CONTROL_CHARS_RE.sub("", (text or "").strip())[-n:]


def _run_skills(cmd: list[str], *, home: Path, timeout: int) -> tuple[bool, str]:
    """Run one npx-skills subprocess. Returns (ok, message_tail).

    ok is True only on a clean exit 0. On non-zero exit or timeout it returns
    (False, <last ~500 chars of stderr/stdout>). Never raises for flow control.

    stdin is /dev/null so a future interactive prompt fails fast instead of
    hanging to the timeout. HOME/USERPROFILE are overridden to ``home``: the
    skills CLI writes relative to $HOME, and jacked verifies against ``home``
    (which honors $JACKED_HOME), so pinning the child's HOME keeps the
    subprocess and our post-op verification pointed at the same tree.

    cwd is pinned to ``home`` too: npm exec prefers a cwd-local (or ancestor)
    ``node_modules/skills`` that satisfies the version specifier over the
    registry package, so running jacked from inside an untrusted checkout that
    committed a spoofed ``node_modules/skills`` would execute the spoof despite
    the SKILLS_CLI_SPEC pin (proven empirically). Pinning cwd to the user-owned
    home makes resolution deterministic regardless of launch directory.
    """
    logger.info("Running skills command: %s", " ".join(cmd))
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env, cwd=str(home),
        )
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
