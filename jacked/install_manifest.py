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


def load(path=DEFAULT_MANIFEST_PATH) -> Optional[dict]:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable manifest %s: %s", path, e)
        return None


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
    data = {"version": version, "written_at": now_iso, "artifacts": current_hashes}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def prune_removed(d: ManifestDiff, home) -> list[str]:
    home = Path(home)
    pruned: list[str] = []
    cat_by_key = {c.key: c for c in CATEGORIES}
    for key, cd in d.by_category.items():
        cat = cat_by_key.get(key)
        if not cat:
            continue
        for name in cd.removed:
            target = cat.prune_target(name, home)
            try:
                if target.is_symlink():
                    target.unlink()
                    pruned.append(f"{key}/{name}")
                elif cat.is_skill_dir and target.is_dir():
                    shutil.rmtree(target)
                    pruned.append(f"{key}/{name}")
                elif target.exists():
                    target.unlink()
                    pruned.append(f"{key}/{name}")
            except OSError as e:
                logger.warning("Could not prune %s: %s", target, e)
    return pruned
