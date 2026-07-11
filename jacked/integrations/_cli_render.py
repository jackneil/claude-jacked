"""Render an ``AgentReachRunner.status()`` dict as human console text.

Pure formatting: no rich, no side effects, so ``jacked reach status`` stays a
thin verb and this stays trivially unit-testable. The ``--json`` path emits the
status dict verbatim; this module is only the human surface. All copy here is
user-facing, so it uses commas/colons/parens instead of em-dashes.
"""
from __future__ import annotations

# Keys that mark a per-channel doctor entry when there is no explicit
# ``channels`` container to iterate.
_CHANNEL_KEYS = ("active_backend", "backend", "ok", "status", "healthy")


def _ok_str(ok: object) -> str:
    if isinstance(ok, bool):
        return "yes" if ok else "no"
    if ok is None:
        return "-"
    return str(ok)


def _channel_row(name: object, info: dict) -> tuple[str, str, str]:
    backend = info.get("active_backend") or info.get("backend") or "-"
    ok = info.get("ok")
    if ok is None:
        ok = info.get("healthy")
    if ok is None:
        ok = info.get("status")
    return (str(name), str(backend), _ok_str(ok))


def _doctor_rows(doctor: object) -> list[tuple[str, str, str]]:
    """Normalize the upstream ``doctor --json`` blob into (channel, backend, ok).

    Best effort: the ``--json`` output is the source of truth, so we tolerate a
    ``channels`` list, a ``channels`` mapping, or a top-level channel-shaped
    mapping, and skip anything that does not look like a channel entry.
    """
    if not isinstance(doctor, dict):
        return []
    channels = doctor.get("channels")
    rows: list[tuple[str, str, str]] = []
    if isinstance(channels, list):
        for item in channels:
            if isinstance(item, dict):
                name = item.get("channel") or item.get("name") or "?"
                rows.append(_channel_row(name, item))
    elif isinstance(channels, dict):
        for name, info in channels.items():
            if isinstance(info, dict):
                rows.append(_channel_row(name, info))
    else:
        for name, info in doctor.items():
            if isinstance(info, dict) and any(k in info for k in _CHANNEL_KEYS):
                rows.append(_channel_row(name, info))
    return rows


def _render_table(rows: list[tuple[str, str, str]]) -> list[str]:
    header = ("channel", "active_backend", "ok")
    all_rows = [header, *rows]
    widths = [max(len(r[i]) for r in all_rows) for i in range(3)]
    out = []
    for i, row in enumerate(all_rows):
        cells = "  ".join(row[c].ljust(widths[c]) for c in range(3)).rstrip()
        out.append("    " + cells)
        if i == 0:
            out.append("    " + "  ".join("-" * widths[c] for c in range(3)).rstrip())
    return out


def render_status(status: dict) -> str:
    """Return a multi-line human summary of a runner status dict."""
    lines: list[str] = ["agent-reach integration"]

    installed = bool(status.get("installed"))
    lines.append(f"  Installed:     {'yes' if installed else 'no'}")

    pin = status.get("pin") or {}
    version = pin.get("version_label") or "?"
    short = pin.get("short_sha") or (status.get("pin_sha") or "")[:12] or "?"
    vetted = pin.get("vetted_at") or "?"
    lines.append(f"  Pinned:        {version} ({short}), vetted {vetted}")

    override = status.get("override") or {}
    if override.get("active"):
        ov_short = (override.get("sha") or "")[:12] or "?"
        lines.append(f"  Override:      UNVETTED OVERRIDE ACTIVE -> {ov_short}")
        if override.get("at"):
            lines.append(f"                 acknowledged {override.get('at')}")

    if installed:
        installed_short = (status.get("installed_sha") or "")[:12] or "?"
        match = " (matches authoritative pin)" if status.get("sha_matches") else \
            " (DRIFT: does not match the authoritative pin)"
        lines.append(f"  Installed SHA: {installed_short}{match}")

        drift = status.get("drift") or []
        bad = [d for d in drift if isinstance(d, dict) and d.get("status") != "ok"]
        if bad:
            lines.append(
                f"  Skill files:   {len(bad)} file(s) drifted or missing (possible tampering)"
            )
        else:
            lines.append("  Skill files:   clean (hashes match the vetted pin)")

        rows = _doctor_rows(status.get("doctor"))
        if rows:
            lines.append("  Channels (from doctor):")
            lines.extend(_render_table(rows))
        else:
            reason = status.get("doctor_error") or "no channel data reported"
            lines.append(f"  Channels:      unavailable ({reason})")
    else:
        lines.append("  Run 'jacked reach install' to install at the vetted pin.")

    return "\n".join(lines)
