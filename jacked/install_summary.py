"""Build + render the install/upgrade change-summary, and persist the record
the dashboard reads (~/.claude/jacked-last-install.json)."""
from __future__ import annotations

import json
from pathlib import Path

from jacked.install_manifest import ManifestDiff

DEFAULT_LAST_INSTALL_PATH = Path.home() / ".claude" / "jacked-last-install.json"

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


def write_last_install(record: dict, path=DEFAULT_LAST_INSTALL_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)
