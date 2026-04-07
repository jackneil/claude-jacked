"""Cross-platform path resolution and project hash decoding for Claude Code.

Locates Claude Code project directories, decodes their encoded directory names
into readable project names, and finds recently-active JSONL conversation files.

No external dependencies — uses only pathlib, os, sys, time.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Segments that are stripped when building a short display name.
# Kept lowercase for case-insensitive matching.
_COMMON_PREFIXES = frozenset({
    "users", "home", "documents", "desktop", "downloads",
    "github", "repos", "projects", "code", "dev", "src",
    "conductor", "workspaces",
})


def get_claude_data_dirs() -> list[Path]:
    """Return all valid Claude Code project directories.

    Always includes ``~/.claude/projects/``.
    On Linux only, also includes ``~/.config/claude/projects/`` if it exists
    (XDG fallback).
    """
    home = Path.home()
    dirs: list[Path] = []

    primary = home / ".claude" / "projects"
    dirs.append(primary)

    if sys.platform.startswith("linux"):
        xdg = home / ".config" / "claude" / "projects"
        if xdg.is_dir():
            dirs.append(xdg)

    return dirs


def _probe_filesystem(root: str, dash_segments: list[str]) -> list[str]:
    """Greedily resolve dash-separated segments against the real filesystem.

    Claude Code encodes ``/`` as ``-`` but does NOT escape existing dashes, so
    the mapping is ambiguous.  We resolve this by probing the filesystem: start
    from *root* and greedily consume dash-segments, merging them with dashes
    when a single-segment directory doesn't exist but the merged version does.

    Parameters
    ----------
    root:
        The filesystem root prefix (e.g. ``/`` or ``C:/``).
    dash_segments:
        The dash-split parts of the hash (excluding drive and leading dash).

    Returns
    -------
    List of resolved path components (with dashes preserved where the
    filesystem confirms them).
    """
    resolved: list[str] = []
    i = 0
    current_path = Path(root)

    while i < len(dash_segments):
        # Try merging progressively more segments with dashes
        best_match = None
        for j in range(len(dash_segments), i, -1):
            candidate = "-".join(dash_segments[i:j])
            candidate_path = current_path / candidate
            if candidate_path.exists():
                best_match = (candidate, j)
                break

        if best_match:
            merged, end_idx = best_match
            resolved.append(merged)
            current_path = current_path / merged
            i = end_idx
        else:
            # No filesystem match — take single segment
            resolved.append(dash_segments[i])
            current_path = current_path / dash_segments[i]
            i += 1

    return resolved


def decode_project_hash(hash_name: str) -> dict:
    """Decode a Claude Code encoded directory name into a readable project name.

    Claude Code encodes filesystem paths by replacing ``/`` with ``-``.
    Existing dashes in directory names are NOT escaped, so the encoding is
    ambiguous.  This function probes the real filesystem to resolve ambiguity
    when possible, falling back to a naive ``-`` -> ``/`` split otherwise.

    Parameters
    ----------
    hash_name:
        The encoded directory name, e.g. ``-Users-jack-Github-claude-jacked``.

    Returns
    -------
    dict with keys ``name`` (short display name) and ``path`` (decoded
    filesystem path).
    """
    if not hash_name:
        return {"name": "", "path": ""}

    raw = hash_name

    # --- Windows drive letter: ``C---`` -> ``C:/`` -------------------------
    drive_prefix = ""
    fs_root = "/"
    if len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == "--":
        drive_prefix = f"{raw[0]}:/"
        fs_root = drive_prefix
        raw = raw[3:]

    # --- Handle double-dash worktree marker --------------------------------
    # ``--claude-worktrees-agent-a7c6b155`` -> ``.claude-worktrees/agent-...``
    # ``--worktrees-feat-name`` -> ``.worktrees/feat-name``
    worktree_suffix = ""
    double_dash_idx = raw.find("--")
    if double_dash_idx != -1:
        wt_raw = raw[double_dash_idx + 2:]  # after the ``--``
        raw = raw[:double_dash_idx]
        # "worktrees" marks the boundary between the hidden dir and the
        # worktree folder name.
        wt_parts = wt_raw.split("-")
        if "worktrees" in wt_parts:
            wt_idx = wt_parts.index("worktrees")
            hidden_dir = "-".join(wt_parts[: wt_idx + 1])
            rest = "-".join(wt_parts[wt_idx + 1:])
            worktree_suffix = f"/.{hidden_dir}/{rest}" if rest else f"/.{hidden_dir}"
        else:
            worktree_suffix = f"/.{wt_raw}"

    # --- Strip leading dash ------------------------------------------------
    if raw.startswith("-"):
        raw = raw[1:]

    if not raw and not drive_prefix:
        # Bare word like "unknown" with no leading dash
        return {"name": hash_name, "path": hash_name}

    # --- Resolve segments --------------------------------------------------
    dash_segments = raw.split("-") if raw else []

    # Try filesystem probing to disambiguate dashes-as-separators from
    # dashes-in-names.  Falls back to naive 1:1 mapping if probing fails.
    try:
        resolved = _probe_filesystem(fs_root, dash_segments)
    except Exception:
        resolved = dash_segments

    full_path = drive_prefix + "/".join(resolved) + worktree_suffix
    # Strip duplicate leading slash when drive_prefix is empty and root is /
    if not drive_prefix and full_path.startswith("//"):
        full_path = full_path[1:]

    # --- Build short display name ------------------------------------------
    # Walk resolved segments and skip common prefixes (Users, home, Github,
    # etc.) plus the first segment after Users/home which is typically a
    # username.  The goal is to surface just the project name.
    meaningful: list[str] = []
    skip_next_as_username = False
    for seg in resolved:
        lower = seg.lower()
        if not meaningful:
            if lower in _COMMON_PREFIXES:
                # Flag: if this was "users" or "home", the next segment is
                # a username and should also be skipped.
                if lower in ("users", "home"):
                    skip_next_as_username = True
                continue
            if skip_next_as_username:
                skip_next_as_username = False
                continue
        meaningful.append(seg)

    if not meaningful:
        meaningful = resolved[-1:] if resolved else [hash_name]

    # Use the last segment as the display name.  This is the deepest
    # directory — typically the project name (already with real dashes).
    name = meaningful[-1]

    return {"name": name, "path": full_path}


def find_active_jsonl_files(
    data_dirs: list[Path],
    max_age_seconds: int = 600,
) -> list[Path]:
    """Find JSONL files modified within *max_age_seconds*.

    Walks each project directory under *data_dirs*, checking ``.jsonl`` files
    at the project level and inside ``subagents/`` subdirectories.

    Parameters
    ----------
    data_dirs:
        List of base directories (each containing project subdirectories).
    max_age_seconds:
        Maximum file age in seconds.  Defaults to 600 (10 minutes).

    Returns
    -------
    List of Paths to recently-modified ``.jsonl`` files.
    """
    cutoff = time.time() - max_age_seconds
    results: list[Path] = []

    for base in data_dirs:
        try:
            if not base.is_dir():
                continue
            for project_dir in base.iterdir():
                if not project_dir.is_dir():
                    continue
                _collect_jsonl(project_dir, cutoff, results)
                # Also check subagents/ inside each project dir
                subagents = project_dir / "subagents"
                if subagents.is_dir():
                    _collect_jsonl(subagents, cutoff, results)
        except (PermissionError, OSError):
            continue

    return results


def _collect_jsonl(directory: Path, cutoff: float, out: list[Path]) -> None:
    """Append recent ``.jsonl`` files from *directory* to *out*."""
    try:
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix != ".jsonl":
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    out.append(entry)
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
