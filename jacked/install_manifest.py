"""Manifest of jacked-installed artifacts, for change-summaries + safe pruning.

Records, per jacked version, the content hash of every artifact jacked ships
(skills/commands/agents/lenses/templates). Diffing the current source against
the prior manifest yields added/changed/removed/unchanged — correct in both
copy and editable/symlink installs (it compares source-now vs source-at-last-install).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = Path.home() / ".claude" / "jacked-manifest.json"


@dataclass(frozen=True)
class Category:
    key: str
    src_glob: str          # relative to data_root
    dest_subpath: str      # under ~/.claude
    is_skill_dir: bool = False

    def name_of(self, src_file: Path) -> str:
        return src_file.parent.name if self.is_skill_dir else src_file.name

    def prune_target(self, name: str, home: Path) -> Path:
        base = home / ".claude" / self.dest_subpath
        return base / name  # skill dir or file; both live directly under base


CATEGORIES = [
    Category("skills", "skills/*/SKILL.md", "skills", is_skill_dir=True),
    Category("commands", "commands/*.md", "commands"),
    Category("agents", "agents/*.md", "agents"),
    Category("lenses", "lenses/*.md", "lenses"),
    Category("templates", "templates/*.html", "jacked-templates"),
]


@dataclass
class CategoryDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass
class ManifestDiff:
    by_category: dict  # key -> CategoryDiff

    def unchanged_count(self) -> int:
        return sum(len(c.unchanged) for c in self.by_category.values())

    def has_changes(self) -> bool:
        return any(c.added or c.changed or c.removed for c in self.by_category.values())

    def to_changes_dict(self) -> dict:
        return {
            k: {"added": c.added, "changed": c.changed, "removed": c.removed}
            for k, c in self.by_category.items()
        }


def _sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


# Sidecar-aware protection. `artifacts.skills` keeps its original meaning (the
# SKILL.md hash) for backward compatibility; `artifacts.skills_dirs` is a
# parallel map of FULL-DIR hashes, recorded from the dirs jacked actually wrote.
# A manifest without skills_dirs predates the feature: the guards then fall back
# to the SKILL.md check alone, so old installs upgrade without spurious backups.
SKILLS_DIRS_KEY = "skills_dirs"
MANIFEST_FORMAT = 2

# Editor/OS droppings and byte-code caches are never jacked's content, and a
# .DS_Store appearing after install must not make a dir look user-modified.
_HASH_IGNORE_NAMES = frozenset({".DS_Store", "Thumbs.db"})


def _hashable_files(d: Path) -> list:
    return sorted(
        p for p in d.rglob("*")
        if p.is_file()
        and p.name not in _HASH_IGNORE_NAMES
        and p.suffix != ".pyc"
        and "__pycache__" not in p.parts
    )


def skill_content_hash(skill_dir) -> Optional[str]:
    """Full-dir hash of a skill (relative paths + file bytes, stable order).

    Mirrors the Codex installer's `_sha_dir`, so a modified SIDECAR changes the
    hash even when SKILL.md is untouched. None if the dir is missing/unreadable."""
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        return None
    h = hashlib.sha256()
    try:
        for f in _hashable_files(skill_dir):
            h.update(f.relative_to(skill_dir).as_posix().encode())
            h.update(f.read_bytes())
    except OSError as e:
        logger.warning("Could not hash %s: %s", skill_dir, e)
        return None
    return "sha256:" + h.hexdigest()


def hash_installed_skill_dirs(home, names) -> dict:
    """Full-dir hashes of the INSTALLED ~/.claude/skills/<name> dirs.

    Hashing what was written (not the source) keeps the value exact: install
    merges files into an existing dir, so a sidecar an older jacked version
    shipped can still be there, and a source-derived hash would then read as
    "the user modified this" on the next run."""
    base = Path(home) / ".claude" / "skills"
    out: dict = {}
    for name in names:
        if not _is_safe_name(name):
            continue
        digest = skill_content_hash(base / name)
        if digest:
            out[name] = digest
    return out


def hash_source(data_root) -> dict:
    data_root = Path(data_root)
    out: dict = {}
    for cat in CATEGORIES:
        names: dict = {}
        for src in sorted(data_root.glob(cat.src_glob)):
            try:
                names[cat.name_of(src)] = _sha256_file(src)
            except OSError as e:
                logger.warning("Could not hash %s: %s", src, e)
        out[cat.key] = names
    return out


def _is_safe_name(name) -> bool:
    """True iff `name` is a single, safe path component (no separators, no
    traversal). Manifest-supplied names are joined onto real dirs during
    prune/uninstall; ``../foo`` or ``a/b`` must never drive a delete."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return Path(name).name == name


def skill_dir_hash(skill_dir) -> Optional[str]:
    """Hash of an INSTALLED skill dir's ``SKILL.md``, comparable to a manifest
    entry (manifest skill hashes are the source ``SKILL.md`` hash, and install
    copies/symlinks that file verbatim). None if it is missing/unreadable.

    Sidecar files are deliberately out of scope: the manifest records one hash
    per skill (its SKILL.md), so that is the granularity the guards can check."""
    p = Path(skill_dir) / "SKILL.md"
    try:
        return _sha256_file(p)
    except OSError:
        return None


def _recorded(manifest: Optional[dict], key: str, name: str) -> Optional[str]:
    value = ((manifest or {}).get("artifacts", {}).get(key) or {}).get(name)
    return value if isinstance(value, str) else None


def is_jacked_skill_dir(skill_dir, name: str, manifest: Optional[dict]) -> bool:
    """True iff `skill_dir` is still jacked's own copy of skill `name`.

    Requires the ``SKILL.md`` hash to match what `manifest` recorded AND, when a
    full-dir hash was recorded for it, the whole dir to match too — so an edited
    SIDECAR also marks the dir as the user's. Only a format-1 manifest (which
    never recorded dir hashes) is owned on the SKILL.md match alone.

    False when there is no ``skills`` entry at all: pre-manifest installs are
    treated as potentially user-owned. This is a HASH judgement only; the
    uninstall/install callers additionally accept a dir that is still consistent
    with the packaged source (`is_source_subset`)."""
    recorded_md = _recorded(manifest, "skills", name)
    if recorded_md is None or skill_dir_hash(skill_dir) != recorded_md:
        return False
    recorded_dir = _recorded(manifest, SKILLS_DIRS_KEY, name)
    if recorded_dir is not None:
        return skill_content_hash(skill_dir) == recorded_dir
    # No dir hash for this skill. Two DIFFERENT causes, and only one is benign:
    #   format < 2 : the manifest predates dir hashes entirely, so the SKILL.md
    #                match is all the evidence that exists -> accept it, which is
    #                what lets old installs upgrade without spurious backups.
    #   format >= 2: this manifest DOES record dir hashes, so a missing entry
    #                means jacked never recorded writing this dir (install error,
    #                hand-edited manifest, ...) -> provenance unknown, not ours.
    #                `is_source_subset` is then the only route to ownership.
    return _manifest_format(manifest) < 2


def _manifest_format(manifest: Optional[dict]) -> int:
    try:
        return int((manifest or {}).get("format", 1) or 1)
    except (TypeError, ValueError):
        return 1


def is_source_subset(skill_dir, src_dir) -> bool:
    """True iff EVERY file in `skill_dir` byte-matches the same relative path in
    `src_dir` (extra files in the source are fine, extra files here are not).

    Install merges files into the dir and never deletes, so a dir jacked owns is
    always file-subset-consistent with some state of the source. Comparing
    against the CURRENT source is the practical test, and it is what stops an
    editable/dev install from backing up jacked's OWN dir after the source moved
    (a `git pull` changes SKILL.md and the file set, so the recorded hashes both
    miss). Any user file, or any user edit, breaks the subset and returns False."""
    skill_dir, src_dir = Path(skill_dir), Path(src_dir)
    if not (skill_dir.is_dir() and src_dir.is_dir()):
        return False
    try:
        for f in _hashable_files(skill_dir):
            peer = src_dir / f.relative_to(skill_dir)
            if not peer.is_file() or peer.read_bytes() != f.read_bytes():
                return False
    except OSError as e:
        logger.warning("Could not compare %s with %s: %s", skill_dir, src_dir, e)
        return False
    return True


def backups_root(skill_dir: Path) -> Path:
    """Where preserved dirs go: ``<skills-parent>/jacked-backups/skills``.

    Out of the live skills tree on purpose. A backup left INSIDE ~/.claude/skills
    (or ~/.agents/skills) is a discoverable duplicate skill the agent can load,
    and those copies accumulate forever."""
    return skill_dir.parents[1] / "jacked-backups" / "skills"


def backup_dir_for(skill_dir: Path, name: str, now=None) -> Path:
    """First unused ``<backups_root>/<name>-<UTC timestamp>`` path, so an earlier
    preserved copy is never clobbered."""
    from datetime import datetime, timezone

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    base = backups_root(skill_dir) / f"{name}-{stamp}"
    backup, n = base, 2
    while backup.exists() or backup.is_symlink():
        backup = base.with_name(f"{base.name}-{n}")
        n += 1
    return backup


def preserve_user_skill_dir(
    skill_dir, name: str, src_dir, manifest: Optional[dict]
) -> Optional[Path]:
    """Never destroy a user's OWN ~/.claude/skills/<name> on a name collision.

    Before install overwrites `skill_dir`, move it aside to
    ``~/.claude/jacked-backups/skills/<name>-<timestamp>`` unless it is jacked's
    own copy (per `manifest`) or consistent with `src_dir`, the source about to
    be installed. Returns the backup path when it moved one, else None."""
    skill_dir = Path(skill_dir)
    if not (skill_dir.exists() or skill_dir.is_symlink()):
        return None
    if is_jacked_skill_dir(skill_dir, name, manifest):
        return None
    if src_dir is not None and is_source_subset(skill_dir, src_dir):
        return None  # nothing here that the source doesn't already carry
    backup = backup_dir_for(skill_dir, name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_dir), str(backup))
    logger.warning("preserved your existing skill %s as %s", name, backup)
    return backup


def skill_removal_decision(
    skill_dir, name: str, manifest: Optional[dict], src_dir
) -> tuple:
    """Uninstall gate: ``(remove?, reason_to_keep)`` for one skill dir.

    Removes a dir jacked owns per the manifest, or (when the manifest is absent,
    corrupt, or silent about it) one whose every file still matches the packaged
    source. Anything else is kept, with a reason the caller can print."""
    if is_jacked_skill_dir(skill_dir, name, manifest):
        return True, ""
    if src_dir is not None and is_source_subset(skill_dir, src_dir):
        return True, ""
    if manifest is None:
        return False, (
            f"no install manifest found for {name}; kept. "
            "Remove it manually if it is not yours."
        )
    return False, "it no longer matches what jacked installed"


def load_with_status(path=DEFAULT_MANIFEST_PATH) -> tuple:
    """``(manifest, status)`` where status is ``ok``, ``missing`` or ``corrupt``.

    Uninstall needs the distinction: "never installed here" and "the manifest is
    damaged" both yield no manifest, but only the second deserves a warning."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable manifest %s: %s", path, e)
        return None, "corrupt"


def load(path=DEFAULT_MANIFEST_PATH) -> Optional[dict]:
    return load_with_status(path)[0]


def diff(prior: Optional[dict], current: dict) -> ManifestDiff:
    prior_arts = (prior or {}).get("artifacts", {})
    by_cat: dict = {}
    for cat in CATEGORIES:
        cur = current.get(cat.key, {})
        old = prior_arts.get(cat.key, {})
        cd = CategoryDiff()
        for name in sorted(cur):
            if name not in old:
                cd.added.append(name)
            elif old[name] != cur[name]:
                cd.changed.append(name)
            else:
                cd.unchanged.append(name)
        for name in sorted(old):
            if name not in cur:
                cd.removed.append(name)
        by_cat[cat.key] = cd
    return ManifestDiff(by_cat)


def write(path, version: str, current_hashes: dict, now_iso: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": version,
        "format": MANIFEST_FORMAT,
        "written_at": now_iso,
        "artifacts": current_hashes,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _move_aside_flat(target: Path, key: str, now=None) -> Path:
    """Move a flat artifact to ``~/.claude/jacked-backups/<key>/<stem>-<ts><ext>``.

    Same contract as `backup_dir_for`: outside the live tree, never clobbers an
    earlier backup."""
    from datetime import datetime, timezone

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    root = target.parents[1] / "jacked-backups" / key
    base = root / f"{target.stem}-{stamp}{target.suffix}"
    backup, n = base, 2
    while backup.exists() or backup.is_symlink():
        backup = base.with_name(f"{target.stem}-{stamp}-{n}{target.suffix}")
        n += 1
    root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(backup))
    return backup


def prune_removed(
    d: ManifestDiff, home, prior: Optional[dict] = None
) -> tuple[list[str], list[str]]:
    """Delete artifacts jacked shipped before but no longer ships.

    Returns ``(pruned, preserved)``. When `prior` (the manifest that recorded
    them) is supplied, a skill DIR is deleted only if its content still matches
    what jacked installed; a dir the user modified or recreated is left in place
    and logged. A FLAT artifact (command/agent/lens/template) whose bytes no
    longer match the recorded hash was edited by the user: it is moved into
    ``~/.claude/jacked-backups/<category>/`` instead of unlinked, and reported
    in `preserved`."""
    home = Path(home)
    pruned: list[str] = []
    preserved: list[str] = []
    cat_by_key = {c.key: c for c in CATEGORIES}
    for key, cd in d.by_category.items():
        cat = cat_by_key.get(key)
        if not cat:
            continue
        for name in cd.removed:
            if not _is_safe_name(name):
                continue
            target = cat.prune_target(name, home)
            if (cat.is_skill_dir and prior is not None and target.is_dir()
                    and not target.is_symlink()
                    and not is_jacked_skill_dir(target, name, prior)):
                logger.warning(
                    "leaving skill dir %s in place: it no longer matches what "
                    "jacked installed (you likely modified or recreated it)", target,
                )
                continue
            try:
                if target.is_symlink():
                    target.unlink()
                    pruned.append(f"{key}/{name}")
                elif cat.is_skill_dir and target.is_dir():
                    shutil.rmtree(target)
                    pruned.append(f"{key}/{name}")
                elif target.exists():
                    # Positive proof only: delete when the bytes still match the
                    # recorded hash; anything else (edited file, unreadable file,
                    # malformed manifest value) is moved aside, never unlinked.
                    recorded = _recorded(prior, key, name)
                    try:
                        current = _sha256_file(target)
                    except OSError:
                        current = None
                    if recorded is not None and current == recorded:
                        target.unlink()
                        pruned.append(f"{key}/{name}")
                    else:
                        backup = _move_aside_flat(target, key)
                        logger.warning(
                            "preserved your modified %s as %s (jacked no longer "
                            "ships it)", target, backup,
                        )
                        preserved.append(f"{key}/{name}")
            except OSError as e:
                logger.warning("Could not prune %s: %s", target, e)
    return pruned, preserved


def remove_or_preserve_flat(target, key, name, prior: Optional[dict], src=None) -> Optional[str]:
    """Uninstall's gate for one flat artifact: ``"removed"``, ``"preserved"``,
    or None when nothing exists at the path (or it could not be removed).

    Mirrors `skill_removal_decision` for files: a symlink (jacked's own
    editable-install mechanism) is unlinked; a real file is unlinked only when
    its bytes match the recorded manifest hash OR the packaged source `src`
    (the pre-manifest-install fallback). Anything else was edited by the user
    and is moved into ``~/.claude/jacked-backups/<category>/`` instead."""
    target = Path(target)
    if not (target.exists() or target.is_symlink()):
        return None
    try:
        if target.is_symlink():
            target.unlink()
            return "removed"
        current = _sha256_file(target)
        if _recorded(prior, key, name) == current:
            target.unlink()
            return "removed"
        if src is not None:
            try:
                if current == _sha256_file(Path(src)):
                    target.unlink()
                    return "removed"
            except OSError:
                pass
        backup = _move_aside_flat(target, key)
        logger.warning("preserved your modified %s as %s", target, backup)
        return "preserved"
    except OSError as e:
        logger.warning("Could not remove %s: %s", target, e)
        return None


def migrated_skill_names(data_root) -> set[str]:
    """Names of shipped skills that absorbed a former command body.

    The ``<!-- ENGINE -->`` marker line IS the producer's record of the
    migration, so the set is derived from the data tree rather than restated as
    a list that could drift."""
    out: set[str] = set()
    for skill_md in Path(data_root).glob("skills/*/SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(line.strip() == "<!-- ENGINE -->" for line in text.splitlines()):
            out.add(skill_md.parent.name)
    return out


def sweep_migrated_commands(home, skill_names, prior: Optional[dict]) -> tuple[list[str], list[str]]:
    """Belt-and-braces cleanup for commands that became skills.

    A ``~/.claude/commands/<name>.md`` whose name is one of the MIGRATED skills
    (`migrated_skill_names`) is usually a leftover from an older dual-shipped
    install. With a usable prior manifest the diff-driven prune already handled
    it; without one (missing or corrupt manifest) `diff` reports nothing
    removed, the file survives forever, and the freshly written manifest —
    which no longer lists it — makes it unreachable by every later prune AND by
    uninstall. This sweep closes that hole with positive proof only: the file
    (or symlink target) is deleted when its bytes still match the recorded
    manifest hash. Anything unproven — no manifest, edited bytes, a user's own
    file or dotfile symlink that merely shares the name — is LEFT IN PLACE and
    reported in `stale` so the caller can tell the user about it (the shipped
    skill shadows it in Claude Code, so it is inert; touching it without proof
    would be worse than the clutter). Returns ``(removed, stale)``."""
    home = Path(home)
    removed: list[str] = []
    stale: list[str] = []
    for name in sorted(skill_names):
        if not _is_safe_name(name):
            continue
        target = home / ".claude" / "commands" / f"{name}.md"
        if not (target.exists() or target.is_symlink()):
            continue
        try:
            recorded = _recorded(prior, "commands", f"{name}.md")
            # _sha256_file reads THROUGH a symlink, so jacked's own
            # editable-install links match their recorded source hash too.
            if recorded is not None and _sha256_file(target) == recorded:
                target.unlink()
                removed.append(f"commands/{name}.md")
            else:
                stale.append(f"commands/{name}.md")
        except OSError as e:
            logger.warning("Could not sweep %s: %s", target, e)
            stale.append(f"commands/{name}.md")
    return removed, stale
