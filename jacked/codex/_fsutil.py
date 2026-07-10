"""Low-level filesystem, hashing, and Codex path primitives for the installer."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping, Optional

from .credentials import codex_home


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path` via a sibling temp file + os.replace.

    Mirrors cli._write_settings_atomic: write to a temp file, flush + os.fsync,
    then os.replace onto the target so a process killed mid-write can never leave
    a half-written Codex file (AGENTS.md, config.toml, hooks.json, manifest, or
    a restore-to-original). Cleans up the temp file if anything fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write `text` to `path` as UTF-8 (see `_atomic_write_bytes`)."""
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
    return shutil.which("codex") is not None or codex_home(env).exists()


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


def _sha_solo_skill(content: str) -> str:
    """The `_sha_dir` value a solo skill dir (only ``SKILL.md`` holding `content`)
    will hash to once written, computed without touching disk. Mirrors `_sha_dir`
    for a single UTF-8 ``SKILL.md`` so a pre-write byte-identity check is exact."""
    h = hashlib.sha256()
    h.update(b"SKILL.md")
    h.update(content.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def _copy_tree(src: Path, dst: Path) -> None:
    """Replace dst with an exact copy of src (no stale sidecars left behind)."""
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.copytree(src, dst)


def _write_solo_skill(skill_dir: Path, content: str) -> None:
    """Replace skill_dir with a single-file skill (only SKILL.md).

    Mirrors _copy_tree's replace semantics so a prior pointer-wrapper copy (and
    any of its sidecars) is wiped, leaving nothing stale behind."""
    if skill_dir.exists() or skill_dir.is_symlink():
        if skill_dir.is_dir() and not skill_dir.is_symlink():
            shutil.rmtree(skill_dir)
        else:
            skill_dir.unlink()
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
