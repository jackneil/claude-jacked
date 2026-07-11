"""Pure helpers for the agent-reach runner (hashing, parsing, atomic writes, hints).

Kept separate so ``agent_reach.py`` stays under the repo's per-file line guardrail
and holds only the runner orchestration.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_UV_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Upstream configure hints surfaced after a channel is enabled. jacked never runs
# the login/cookie step itself; it points the user at it. Cookie-based platforms
# risk account bans, so the copy steers users to a burner account + --from-browser.
_DEFAULT_CONFIGURE_HINT = (
    "Channel installed. Complete login/config yourself by running the upstream "
    "step, e.g. `agent-reach configure`. Use a dedicated burner account for "
    "cookie-based platforms (they risk bans), and prefer --from-browser / OpenCLI "
    "over pasting cookies (paste routes tokens through the agent transcript)."
)
_CHANNEL_CONFIGURE_HINTS: dict[str, str] = {
    "twitter": (
        "Twitter/X enabled. Authenticate with `agent-reach configure twitter` "
        "using a dedicated burner account (cookie auth risks account bans); prefer "
        "--from-browser over pasting cookies."
    ),
    "opencli": (
        "OpenCLI enabled. Install the Chrome extension manually (Chrome forbids "
        "auto-install) and complete `agent-reach configure opencli`. OpenCLI bridges "
        "live browser sessions -- keep it on a burner profile."
    ),
    "reddit": (
        "Reddit enabled. Complete `agent-reach configure reddit`; use a dedicated "
        "account and prefer --from-browser over cookie paste."
    ),
    "bilibili": (
        "Bilibili enabled. Complete `agent-reach configure bilibili` with a burner "
        "account; prefer --from-browser over cookie paste."
    ),
    "search": "Search enabled. No login required; run `agent-reach doctor` to confirm.",
}


def configure_hint(name: str) -> str:
    return _CHANNEL_CONFIGURE_HINTS.get(name, _DEFAULT_CONFIGURE_HINT)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


class ReachUserError(RuntimeError):
    """A user-correctable reach error (unknown channel, not installed, bad ref,
    missing ack) — the API maps it to 422; genuine execution failures stay 500."""


def channels_status(pin, enabled: list[str]) -> list[dict]:
    """The pin's channel table as UI-ready dicts with per-channel enabled flags."""
    enabled_set = set(enabled)
    return [
        {
            "name": name,
            "enabled": name in enabled_set,
            "backends": [
                {"kind": b.kind, "spec": b.spec, "note": b.note}
                for b in channel.backends
            ],
        }
        for name, channel in sorted(pin.channels.items())
    ]


def normalize_doctor(raw: object) -> dict | None:
    """Canonicalize `agent-reach doctor --json` output to {"channels": [...]}.

    Upstream (v1.5.0, verified live) emits a dict keyed by channel slug:
    {"github": {"active_backend", "backends", "message", "name", "status",
    "tier"}, ...}. Consumers (dashboard + CLI table) want a stable list shape;
    "name" upstream is a localized display name, so the slug becomes our "name"
    and theirs is kept as "display_name". "message" doubles as the fix hint.
    A payload that already carries a "channels" list passes through untouched.
    """
    if raw is None:
        return None
    if isinstance(raw, dict) and isinstance(raw.get("channels"), list):
        return raw
    if not isinstance(raw, dict):
        return {"channels": [], "raw": raw}
    channels = []
    for slug, info in raw.items():
        if not isinstance(info, dict):
            continue
        channels.append({
            "name": slug,
            "display_name": info.get("name"),
            "active_backend": info.get("active_backend"),
            "status": info.get("status"),
            "hint": info.get("message"),
            "tier": info.get("tier"),
            "backends": info.get("backends"),
        })
    channels.sort(key=lambda c: (c["tier"] if isinstance(c["tier"], int) else 99, c["name"]))
    return {"channels": channels}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def norm_hash(h: str | None) -> str | None:
    if h is None:
        return None
    h = h.strip().lower()
    return h.split(":", 1)[1] if h.startswith("sha256:") else h


def drift_entry(
    skill_dir: Path | None, name: str, status: str, expected: str | None, actual: str | None
) -> dict:
    return {
        "dir": str(skill_dir) if skill_dir is not None else None,
        "file": name,
        "status": status,
        "expected": expected,
        "actual": actual,
    }


def format_drift(drift: list[dict]) -> str:
    bad = [d for d in drift if d["status"] != "ok"]
    return "; ".join(f"{d['file']} in {d['dir']}: {d['status']}" for d in bad) or "no detail"


def parse_uv_version(text: str) -> tuple[int, int, int] | None:
    m = _UV_VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def stderr_excerpt(stderr: str | None, limit: int = 1200) -> str:
    if not stderr:
        return "(no stderr)"
    stderr = stderr.strip()
    return stderr if len(stderr) <= limit else "..." + stderr[-limit:]


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def installed_layout(skill_hashes: dict[str, str]) -> dict[str, set]:
    """Map the pin's SOURCE-tree hashes to the layout upstream actually installs.

    Upstream's ``_install_skill`` picks SKILL.md or SKILL_en.md by locale and
    writes the winner AS ``SKILL.md`` in the target dir; no file named
    ``SKILL_en.md`` is ever installed. So the installed ``SKILL.md`` must match
    EITHER source hash, and every other pin entry (references/*.md) must match
    its own hash by name.
    """
    expected: dict[str, set] = {}
    skill_variants = set()
    for name, digest in skill_hashes.items():
        if name in ("SKILL.md", "SKILL_en.md"):
            skill_variants.add(norm_hash(digest))
        else:
            expected[name] = {norm_hash(digest)}
    if skill_variants:
        expected["SKILL.md"] = skill_variants
    return expected


def verify_skill_hashes(home: Path, relpaths, skill_hashes: dict[str, str]) -> tuple[bool, list[dict]]:
    """Re-hash installed skill files in every existing skill dir against the pin.

    The pin records SOURCE-tree hashes; :func:`installed_layout` translates them
    to what upstream's installer actually writes (locale-selected SKILL.md).
    Returns (ok, per-file drift). ``ok`` is False on any mismatch/missing file, or
    if NO skill dir exists at all (the install placed nothing). Hash comparison
    tolerates a ``sha256:`` prefix on either side.
    """
    expected_map = installed_layout(skill_hashes)
    drift: list[dict] = []
    dirs_checked = 0
    ok = True
    for rel in relpaths:
        skill_dir = home / rel
        if not skill_dir.is_dir():
            continue
        dirs_checked += 1
        for name, accepted in expected_map.items():
            f = skill_dir / name
            expected_repr = "|".join(sorted(h for h in accepted if h))
            if not f.is_file():
                drift.append(drift_entry(skill_dir, name, "missing", expected_repr, None))
                ok = False
                continue
            actual = sha256_file(f)
            if norm_hash(actual) not in accepted:
                drift.append(drift_entry(skill_dir, name, "mismatch", expected_repr, actual))
                ok = False
            else:
                drift.append(drift_entry(skill_dir, name, "ok", expected_repr, actual))
    if dirs_checked == 0:
        drift.append(drift_entry(None, "*", "missing", None, None))
        ok = False
    return ok, drift
