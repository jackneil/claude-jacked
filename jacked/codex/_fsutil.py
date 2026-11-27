"""Low-level filesystem, hashing, and Codex path primitives for the installer."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Mapping, Optional

from jacked.atomic_io import atomic_write_bytes
from jacked.findbin import find_bin

from .credentials import codex_home


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path`, fsynced, through the shared writer.

    A process killed mid-write never leaves a half-written Codex file
    (AGENTS.md, config.toml, hooks.json, manifest, or a restore-to-original).
    """
    atomic_write_bytes(path, data, fsync=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write `text` to `path` as UTF-8 bytes (see `_atomic_write_bytes`).

    Bytes, not text mode: the installer compares file hashes with hashes of
    the text it wrote, so Windows must not translate the line endings.
    """
    _atomic_write_bytes(path, text.encode("utf-8"))


def _is_safe_name(name: str) -> bool:
    """True iff `name` is a single, safe path component (no separators, no
    traversal). Manifest-supplied artifact names are joined onto real dirs during
    prune/uninstall; a name like ``../foo`` or ``a/b`` must never be honored."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return Path(name).name == name


def _marker_line_count(text: str, marker: str) -> int:
    """How many lines of `text` are EXACTLY `marker` (stripped). Whole-line
    matching so user prose that merely embeds the marker substring never counts."""
    return sum(1 for line in text.splitlines() if line.strip() == marker)


def _extract_block(
    text: str, begin: str, end: str
) -> Optional[tuple[str, str, str]]:
    """Split `text` around jacked's whole-line-delimited ``begin``..``end`` block.

    Returns ``(pre, block, post)`` where `block` is the marker-to-marker text
    (markers inclusive, verbatim with line endings) and `pre`/`post` are the text
    before/after it. Returns None when `begin`/`end` are not each present EXACTLY
    once as their own lines, or `end` precedes `begin` - the caller then warns and
    skips, so a marker embedded in user prose (or a duplicated/half marker) can
    never trigger an edit that clobbers user content."""
    if _marker_line_count(text, begin) != 1 or _marker_line_count(text, end) != 1:
        return None
    lines = text.splitlines(keepends=True)
    bi = next(i for i, ln in enumerate(lines) if ln.strip() == begin)
    ei = next(i for i, ln in enumerate(lines) if ln.strip() == end)
    if ei < bi:
        return None
    return "".join(lines[:bi]), "".join(lines[bi:ei + 1]), "".join(lines[ei + 1:])


# ---------------------------------------------------------------------------
# Detection + paths
# ---------------------------------------------------------------------------

def codex_present(env: Optional[Mapping[str, str]] = None) -> bool:
    """True if Codex looks installed (binary on PATH, or a CODEX_HOME exists)."""
    return find_bin("codex") is not None or codex_home(env).exists()


def agents_skills_dir(agents_home: Optional[Path] = None) -> Path:
    return (agents_home or Path.home() / ".agents") / "skills"


def codex_prompts_dir(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "prompts"


def codex_agents_dir(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "agents"


def codex_agents_md(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "AGENTS.md"


def codex_hooks_json(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "hooks.json"


def codex_config_toml(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "config.toml"


def manifest_path(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "jacked-codex-manifest.json"


# ---------------------------------------------------------------------------
# Hashing + copy helpers
# ---------------------------------------------------------------------------

def _sha_file(f: Path) -> str:
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()


def _sha_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def _sha_dir(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def _solo_skill_files(
    content: str, extras: Optional[Mapping[str, str]] = None,
) -> list[tuple[Path, bytes]]:
    """The files of a generated skill dir, in the order that `_sha_dir` reads them.

    Each item is ``(relative path, bytes)``. ``SKILL.md`` holds `content`. Each
    key of `extras` is a relative POSIX path, for example
    ``agents/openai.yaml``, and its value is the text of that file.

    Each part of an extra path must pass `_is_safe_name`. An absolute path, an
    empty part, ``.``, ``..``, a backslash or a second ``SKILL.md`` raises
    ValueError. The sort uses `Path` objects, as `_sha_dir` does, because that
    order is different on Windows (it ignores case there)."""
    files = {Path("SKILL.md"): content.encode("utf-8")}
    for rel, text in (extras or {}).items():
        parts = rel.split("/") if isinstance(rel, str) else []
        if not parts or not all(_is_safe_name(part) for part in parts):
            raise ValueError(f"unsafe extra path in a generated skill: {rel!r}")
        path = Path(*parts)
        if path in files:
            raise ValueError(f"duplicate path in a generated skill: {rel!r}")
        files[path] = text.encode("utf-8")
    return sorted(files.items())


def _sha_solo_skill(content: str, extras: Optional[Mapping[str, str]] = None) -> str:
    """The `_sha_dir` value of a generated skill dir once it is written,
    computed without a disk access.

    The dir holds ``SKILL.md`` with `content` and each file of `extras`
    (see `_solo_skill_files`). With no extras the value is the same as for a
    dir that holds only ``SKILL.md``, so the hashes that a present install
    recorded do not change."""
    h = hashlib.sha256()
    for rel, data in _solo_skill_files(content, extras):
        h.update(rel.as_posix().encode())
        h.update(data)
    return "sha256:" + h.hexdigest()


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy src, sidecars included, into the NEW dir dst.

    This is the copy step of the shared skill transaction
    (`jacked.cli._install_skill_dir_transactional`), which gives it a new
    private staging dir. It never deletes anything: the publish rename of the
    transaction replaces the old skill dir as a whole, so no stale sidecar is
    left. A dst that exists raises FileExistsError and stays as it is."""
    shutil.copytree(src, dst)


def _write_solo_skill(
    skill_dir: Path, content: str, extras: Optional[Mapping[str, str]] = None,
) -> None:
    """Write a generated skill (SKILL.md and each file of `extras`) into the
    NEW dir skill_dir.

    Like `_copy_tree`, this is a copy step of the shared skill transaction:
    the publish rename replaces a prior pointer-wrapper dir as a whole, so
    none of its sidecars is left. A skill_dir that exists raises
    FileExistsError and stays as it is. An unsafe extra path raises
    ValueError before the dir is made (see `_solo_skill_files`). Each file is
    written as bytes, so Windows does not add a carriage return that
    `_sha_solo_skill` does not have."""
    files = _solo_skill_files(content, extras)
    skill_dir.mkdir(parents=True)
    for rel, data in files:
        dest = skill_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
