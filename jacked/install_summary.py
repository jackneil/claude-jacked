"""Build + render the install/upgrade change-summary, and persist the record
the dashboard reads (.claude/jacked-last-install.json under jacked's home)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from jacked.atomic_io import atomic_write_text
from jacked.install_manifest import ManifestDiff

logger = logging.getLogger(__name__)


def last_install_path() -> Path:
    """Return the path of the last-install record, resolved at call time.

    The record lives under jacked's home, which honors ``$JACKED_HOME`` the
    same way ``jacked install`` resolves it when it writes the record.
    """
    from jacked.memory.vault import jacked_home

    return jacked_home() / ".claude" / "jacked-last-install.json"


_LABELS = [
    ("skills", "Skills"), ("commands", "Commands"), ("agents", "Agents"),
    ("lenses", "Lenses"), ("templates", "Templates"),
]
_SYM = {
    "added": ("+", "new", "green"),
    "changed": ("~", "updated", "yellow"),
    "removed": ("-", "removed", "red"),
}


def build_record(diff: ManifestDiff, from_version, to_version: str, now_iso: str) -> dict:
    return {
        "at": now_iso,
        "from_version": from_version,
        "to_version": to_version,
        "changes": diff.to_changes_dict(),
        "unchanged_count": diff.unchanged_count(),
    }


def _has_changes(record: dict) -> bool:
    return any(
        ch.get(k) for ch in record["changes"].values() for k in ("added", "changed", "removed")
    )


def render_terminal(record: dict) -> str:
    frm, to = record["from_version"], record["to_version"]
    changed = _has_changes(record)
    lines: list[str] = []

    if frm is None:
        lines.append(f"[bold]Jacked installed[/bold]  -  {to}")
    elif frm != to:
        lines.append(f"[bold]Jacked upgraded[/bold]   {frm} -> {to}")
    elif changed:
        lines.append(f"[bold]Jacked {to}[/bold] - files refreshed")
    else:
        return f"[green]Jacked {to}[/green] - already up to date ({record['unchanged_count']} artifacts unchanged)"

    lines.append("")
    for cat_key, label in _LABELS:
        ch = record["changes"].get(cat_key, {})
        for kind in ("added", "changed", "removed"):
            for name in ch.get(kind, []):
                sym, word, color = _SYM[kind]
                lines.append(f"  {label:<11}[{color}]{sym}[/{color}] {name:<28}{word}")
    if not changed:
        lines.append("  (no artifact changes)")
    lines.append(f"  {record['unchanged_count']} unchanged")
    lines.append("")
    lines.append("-> Restart Claude Code to load changes.")
    return "\n".join(lines)


def write_last_install(record: dict, path: Path | str | None = None) -> None:
    """Persist the record through the shared atomic writer.

    ``path`` defaults to ``last_install_path()``, resolved at call time.
    A fixed temp name would let `jacked install` and a dashboard upgrade in
    another process replace the record with each other's half-written file.
    """
    target = Path(last_install_path() if path is None else path)
    atomic_write_text(target, json.dumps(record, indent=2))


def read_last_install(path: Path | str | None = None) -> dict | None:
    """Return the last-install record, or None when no usable record exists.

    ``path`` defaults to ``last_install_path()``, resolved at call time. A
    missing file is the normal state before the first install. A file that
    cannot be read or parsed, or that does not hold a JSON object, also
    gives None. The log line for that case names only the exception class:
    the path names the user's home and the content is not ours to print.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "last.json"
    ...     before = read_last_install(p)
    ...     write_last_install({"to_version": "0.110.2"}, p)
    ...     before, read_last_install(p)
    (None, {'to_version': '0.110.2'})
    """
    target = Path(last_install_path() if path is None else path)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # ValueError: bad UTF-8 or bad JSON
        logger.debug("Could not read the last-install record: %s", type(exc).__name__)
        return None
    if not isinstance(record, dict):
        logger.debug(
            "The last-install record is not a JSON object: %s", type(record).__name__
        )
        return None
    return record
