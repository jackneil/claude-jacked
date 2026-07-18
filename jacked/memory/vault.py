"""Memory vault core: paths, state, vault.json mapping, notes, search, git ops.

The vault is jacked-owned: its own git repo under ``$JACKED_VAULT_DIR`` (or
``<jacked_home>/jacked-vault``), holding typed atomic memory notes grouped by
repo. This module is the M1 surface -- everything the ``jacked memory`` CLI
group (init/status/add/search) leans on. Later milestones add capture, recall,
rollup and migration alongside it.

Design conventions this file mirrors (from ``jacked/packs.py``): a home that
honors ``$JACKED_HOME`` (packs.py:93), and a versioned + tolerant + atomic
state file (packs.py:200-294). Retrieval is lexical (grep + index) -- no
embeddings, no yaml dependency (the frontmatter parser/serializer is
hand-rolled here).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VAULT_DIR_ENV = "JACKED_VAULT_DIR"
STATE_PATH_NAME = "jacked-memory.json"          # <home>/.claude/jacked-memory.json
VAULT_CONFIG_NAME = "vault.json"
STATE_VERSION = 1
VAULT_CONFIG_VERSION = 1

# The single note-type enum, validated on write.
VALID_TYPES = ("decision", "convention", "vision", "reference", "progress")

# Default triage model (user-overridable via state). "haiku" is a locate/cheap
# tier; capture milestones read it.
DEFAULT_TRIAGE_MODEL = "haiku"
DEFAULT_DRIFT_THRESHOLD = 15

# Git identity fallback used ONLY when the user has no git identity configured
# (so vault commits still work in CI and on fresh machines). A configured
# user.name / user.email always wins -- see ``_commit``.
_GIT_FALLBACK_NAME = "jacked-memory"
_GIT_FALLBACK_EMAIL = "memory@jacked.local"
_GIT_TIMEOUT = 30

# Control chars except tab/newline/carriage-return, stripped from any text we
# print through Rich (search excerpts, git error tails): a note body or a
# remote-derived string could carry a terminal escape/OSC sequence.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# --------------------------------------------------------------------------- #
# Home / vault resolution
# --------------------------------------------------------------------------- #

def jacked_home() -> Path:
    """Resolve jacked's home dir, honoring ``$JACKED_HOME``.

    Mirrors ``cli._jacked_home`` / ``packs.jacked_home`` so every memory
    surface resolves the same home in tests and prod.
    """
    return Path(os.getenv("JACKED_HOME") or Path.home())


def vault_dir() -> Path:
    """Resolve the vault directory.

    ``$JACKED_VAULT_DIR`` wins (tests + unusual setups); otherwise
    ``<jacked_home>/jacked-vault``.
    """
    return Path(os.getenv(VAULT_DIR_ENV) or jacked_home() / "jacked-vault")


def state_path(home: Path | None = None) -> Path:
    home = jacked_home() if home is None else Path(home)
    return home / ".claude" / STATE_PATH_NAME


def vault_config_path(vault: Path | None = None) -> Path:
    vault = vault_dir() if vault is None else Path(vault)
    return vault / VAULT_CONFIG_NAME


def is_initialized(vault: Path | None = None) -> bool:
    """A vault is initialized once its ``vault.json`` exists."""
    return vault_config_path(vault).exists()


# --------------------------------------------------------------------------- #
# State file (<home>/.claude/jacked-memory.json) -- versioned, tolerant, atomic
# --------------------------------------------------------------------------- #

def _default_state() -> dict:
    return {
        "version": STATE_VERSION,
        "enabled": False,
        "vault_dir": None,
        "triage_model": DEFAULT_TRIAGE_MODEL,
        "drift_added": 0,
        "drift_threshold": DEFAULT_DRIFT_THRESHOLD,
        "last_rollup": None,
        "last_groomed": None,
        "last_sync": None,
        "last_sync_error": None,
        "retry_queue": [],
        "processed_merges": {},
    }


def load_state(home: Path | None = None) -> dict:
    """Load memory state, normalized to the current v1 shape.

    Missing file -> a valid default state. Corrupt file -> a warning plus the
    default (a broken state file must never crash the CLI or a hook). A file
    written by a NEWER jacked is preserved forward-compatibly: unknown keys are
    kept across the load->mutate->save round-trip and the true (newer) version
    label is retained.
    """
    path = state_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable memory state %s: %s", path, e)
        return _default_state()
    return _normalize_state(raw)


def _normalize_state(raw: object) -> dict:
    """Coerce any on-disk state shape to the current v1 shape.

    Preserves unknown top-level keys (forward compat). Fills any missing known
    key with its default. Defensively coerces the fields consumers mutate or
    iterate (drift counters, queue, merge map) so a hand-edited or partial file
    can't crash a ``+= 1`` or an iteration.
    """
    base = _default_state()
    if not isinstance(raw, dict):
        return base

    version = raw.get("version")
    newer = isinstance(version, int) and version > STATE_VERSION
    if newer:
        logger.warning(
            "memory state version %s is newer than this build understands (%s); "
            "preserving unknown fields",
            version, STATE_VERSION,
        )

    out = dict(raw)  # keep unknown keys
    for key, default in base.items():
        out.setdefault(key, default)

    # Defensive coercion of the load-bearing fields.
    if not isinstance(out.get("drift_added"), int) or isinstance(out.get("drift_added"), bool):
        out["drift_added"] = 0
    if not isinstance(out.get("drift_threshold"), int) or isinstance(out.get("drift_threshold"), bool):
        out["drift_threshold"] = DEFAULT_DRIFT_THRESHOLD
    if not isinstance(out.get("retry_queue"), list):
        out["retry_queue"] = []
    if not isinstance(out.get("processed_merges"), dict):
        out["processed_merges"] = {}
    if not isinstance(out.get("enabled"), bool):
        out["enabled"] = bool(out.get("enabled"))

    out["version"] = version if newer else STATE_VERSION
    return out


def save_state(home: Path | None, state: dict) -> None:
    """Atomically write memory state via a writer-unique tmp + os.replace.

    The tmp name is writer-unique (prefix keyed off the target name) because the
    CLI and the dashboard service are separate processes that can both write the
    same home; a shared tmp path lets one os.replace the other's half-written
    file away.
    """
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def bump_drift(home: Path | None = None, n: int = 1) -> int:
    """Increment the drift counter (notes added since the last librarian run).

    Returns the new value. The recall milestone nudges a librarian pass once
    this crosses ``drift_threshold``.
    """
    state = load_state(home)
    state["drift_added"] = int(state.get("drift_added", 0)) + n
    save_state(home, state)
    return state["drift_added"]


def mark_groomed(home: Path | None = None) -> dict:
    """Record a completed librarian groom: reset the drift counter and stamp
    ``last_groomed`` (ISO). Returns the saved state. The ``memory-librarian``
    skill calls this (via ``jacked memory mark-groomed``) when it finishes a
    grooming pass, so the SessionStart drift nudge quiets down again.
    """
    state = load_state(home)
    state["drift_added"] = 0
    state["last_groomed"] = _now_iso()
    save_state(home, state)
    return state


def set_enabled(home: Path | None, enabled: bool, *, vault: Path | None = None) -> dict:
    """Record the feature-level enabled flag (and the vault path when enabling)."""
    state = load_state(home)
    state["enabled"] = bool(enabled)
    if enabled and vault is not None:
        state["vault_dir"] = str(vault)
    save_state(home, state)
    return state


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    """UTC date, ``YYYY-MM-DD`` -- the value written to created/updated."""
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# Name / slug / identity sanitizing
# --------------------------------------------------------------------------- #

def slugify(text: str) -> str:
    """Path-safe slug: lowercase, non-alphanumerics collapsed to single hyphens.

    Traversal-proof by construction: no path separators and no dots survive, so
    ``../../etc/passwd`` becomes ``etc-passwd`` and ``..`` becomes ``note``.
    Never returns empty.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "note"


# Group / repo names double as directory components, so they get the same
# path-safe treatment as slugs.
_sanitize_name = slugify


def normalize_remote(url: str) -> str:
    """Normalize a git remote URL to a stable repo identity.

    Strips scheme, credentials and a trailing ``.git``; lowercased. Handles
    both scp-style (``git@github.com:owner/repo.git``) and URL-style
    (``https://user:pass@github.com/owner/repo.git``) remotes. Returns
    ``host/owner/repo`` (e.g. ``github.com/jackneil/claude-jacked``). Empty in,
    empty out.
    """
    u = (url or "").strip()
    if not u:
        return ""

    # scp-like: [user@]host:path
    m = re.match(r"^[^@/]+@([^:/]+):(.+)$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m2 = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://(.+)$", u)
        rest = m2.group(1) if m2 else u
        rest = re.sub(r"^[^@/]+@", "", rest)   # strip credentials
        if "/" in rest:
            host, path = rest.split("/", 1)
        else:
            host, path = rest, ""

    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    identity = f"{host}/{path}" if path else host
    return identity.lower()


def _git_remote_url(repo: Path) -> str:
    """The origin remote URL for ``repo``, or "" if none / not a repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def repo_identity(path: Path) -> str:
    """The identity of the repo at ``path``: normalized origin remote, else the
    sanitized directory name."""
    p = Path(path)
    if (p / ".git").exists():
        url = _git_remote_url(p)
        if url:
            ident = normalize_remote(url)
            if ident:
                return ident
    return _sanitize_name(p.name)


def _identity_parts(identity: str) -> tuple[str | None, str]:
    """(org, shortname) for an identity. ``host/org/repo`` -> (org, repo); a
    bare dir-name identity -> (None, name)."""
    if "/" in identity:
        parts = [seg for seg in identity.split("/") if seg]
        short = parts[-1] if parts else identity
        org = parts[-2] if len(parts) >= 2 else None
        return org, short
    return None, identity


def group_for_identity(identity: str) -> str:
    """The solo-group name for an unmapped repo: its short name, sanitized."""
    _, short = _identity_parts(identity)
    return _sanitize_name(short)


# --------------------------------------------------------------------------- #
# Group suggestion (init)
# --------------------------------------------------------------------------- #

def _unique_name(base: str, used: set) -> str:
    if base not in used:
        return base
    i = 2
    while f"{base}-{i}" in used:
        i += 1
    return f"{base}-{i}"


def suggest_groups(discovered: list[tuple[Path, str]]) -> "OrderedDict[str, list[str]]":
    """Cluster discovered repos into suggested groups.

    Signal: shared remote org AND a shared leading name token (the part before
    the first ``-``/``_``). Two or more repos that share both cluster under that
    token; anything else becomes a solo group named after the repo. Returns an
    ordered ``{group_name: [identity, ...]}`` with unique names.
    """
    clusters: "OrderedDict[tuple, list[str]]" = OrderedDict()
    for _path, identity in discovered:
        org, short = _identity_parts(identity)
        token = re.split(r"[-_]", short)[0] if short else short
        clusters.setdefault((org, token), []).append(identity)

    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    used: set = set()
    for (_org, token), members in clusters.items():
        if len(members) >= 2 and token:
            name = _unique_name(_sanitize_name(token), used)
            used.add(name)
            groups[name] = list(members)
        else:
            for ident in members:
                _, short = _identity_parts(ident)
                name = _unique_name(_sanitize_name(short), used)
                used.add(name)
                groups[name] = [ident]
    return groups


def scan_roots(roots: list[Path]) -> list[tuple[Path, str]]:
    """Discover git repos: immediate child dirs of each root that contain a
    ``.git``. Returns ``[(path, identity), ...]`` sorted by path, deduped."""
    found: list[tuple[Path, str]] = []
    seen: set = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            try:
                if not child.is_dir() or not (child / ".git").exists():
                    continue
            except OSError:
                continue
            rp = child.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            found.append((child, repo_identity(child)))
    return found


# --------------------------------------------------------------------------- #
# Frontmatter parse / serialize / validate (hand-rolled; no yaml dep)
# --------------------------------------------------------------------------- #

_LIST_KEYS = ("repos", "tags")


def serialize_frontmatter(meta: dict) -> str:
    """Serialize note metadata to the fixed block-style frontmatter.

    List keys (repos, tags) render as block lists when non-empty and inline
    ``[]`` when empty. Scalar keys render inline.
    """
    lines = ["---"]
    lines.append(f"type: {meta.get('type', '')}")
    lines.append(_render_list("repos", meta.get("repos", [])))
    lines.append(f"group: {meta.get('group', '')}")
    lines.append(f"created: {meta.get('created', '')}")
    lines.append(f"updated: {meta.get('updated', '')}")
    lines.append(_render_list("tags", meta.get("tags", [])))
    lines.append("---")
    return "\n".join(lines)


def _render_list(key: str, values) -> str:
    values = list(values or [])
    if not values:
        return f"{key}: []"
    block = [f"{key}:"]
    block.extend(f"  - {v}" for v in values)
    return "\n".join(block)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tolerant frontmatter parse. Returns ``(meta, body)``.

    Accepts block lists (``repos:\\n  - a``), inline lists (``tags: [a, b]``),
    scalars, missing optional keys, blank/comment lines. No frontmatter (no
    leading ``---``) yields ``({}, text)``.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fm = lines[1:end]
    body = "\n".join(lines[end + 1:])
    body = body[1:] if body.startswith("\n") else body

    meta: dict = {}
    i = 0
    while i < len(fm):
        line = fm[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            # Block list: gather following "  - item" lines.
            items: list[str] = []
            j = i + 1
            while j < len(fm):
                m = re.match(r"^\s*-\s+(.*)$", fm[j])
                if not m:
                    break
                items.append(m.group(1).strip())
                j += 1
            meta[key] = items
            i = j
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            meta[key] = [_unquote(x.strip()) for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[key] = _unquote(val)
        i += 1
    return meta, body


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def validate_note(meta: dict) -> None:
    """Enforce the note schema on write. Raises ValueError on any violation."""
    t = meta.get("type")
    if t not in VALID_TYPES:
        raise ValueError(
            f"invalid note type {t!r}; must be one of {', '.join(VALID_TYPES)}"
        )
    repos = meta.get("repos")
    if not isinstance(repos, list) or not repos:
        raise ValueError("note requires a non-empty 'repos' list")
    if not meta.get("group"):
        raise ValueError("note requires a 'group'")
    if not meta.get("created") or not meta.get("updated"):
        raise ValueError("note requires 'created' and 'updated' dates")
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("'tags' must be a list")


def render_note(meta: dict, body: str) -> str:
    return serialize_frontmatter(meta) + "\n\n" + (body or "").strip() + "\n"


def read_note(path: Path) -> tuple[dict, str]:
    return parse_frontmatter(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# vault.json config
# --------------------------------------------------------------------------- #

def _empty_config(roots: list[str] | None = None) -> dict:
    return {
        "version": VAULT_CONFIG_VERSION,
        "roots": list(roots or []),
        "groups": {},
        "repo_map": {},
    }


def load_vault_config(vault: Path | None = None) -> dict:
    """Load vault.json, tolerant of missing/corrupt (returns an empty config)."""
    path = vault_config_path(vault)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_config()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable vault config %s: %s", path, e)
        return _empty_config()
    if not isinstance(raw, dict):
        return _empty_config()
    cfg = _empty_config(raw.get("roots") if isinstance(raw.get("roots"), list) else [])
    cfg.update(raw)
    if not isinstance(cfg.get("groups"), dict):
        cfg["groups"] = {}
    if not isinstance(cfg.get("repo_map"), dict):
        cfg["repo_map"] = {}
    return cfg


def save_vault_config(vault: Path, cfg: dict) -> None:
    """Atomically write vault.json (writer-unique tmp + os.replace)."""
    path = vault_config_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def register_repo(cfg: dict, identity: str, group: str) -> dict:
    """Map ``identity`` -> ``group`` in ``cfg`` (in place) and record the repo
    under the group. Idempotent."""
    groups = cfg.setdefault("groups", {})
    entry = groups.setdefault(group, {"repos": []})
    repos = entry.setdefault("repos", [])
    if identity and identity not in repos:
        repos.append(identity)
    cfg.setdefault("repo_map", {})[identity] = group
    return cfg


def resolve_group(vault: Path, identity: str) -> str | None:
    """The group mapped to ``identity`` in vault.json, or None if unmapped."""
    cfg = load_vault_config(vault)
    grp = cfg.get("repo_map", {}).get(identity)
    return grp if isinstance(grp, str) else None


# --------------------------------------------------------------------------- #
# Group skeleton
# --------------------------------------------------------------------------- #

def group_dir(vault: Path, group: str) -> Path:
    return Path(vault) / "groups" / group


def ensure_group(vault: Path, group: str) -> Path:
    """Create the group skeleton (index.md + hot.md) if absent. Never clobbers
    existing files. Returns the group dir."""
    gdir = group_dir(vault, group)
    gdir.mkdir(parents=True, exist_ok=True)
    index = gdir / "index.md"
    if not index.exists():
        index.write_text(
            f"# Index: {group}\n\n"
            "One line per note, newest appended at the bottom.\n\n",
            encoding="utf-8",
        )
    hot = gdir / "hot.md"
    if not hot.exists():
        hot.write_text(
            f"# Hot: {group}\n\n"
            "Current progress slice. Overwritten by the librarian, never appended forever.\n",
            encoding="utf-8",
        )
    return gdir


# --------------------------------------------------------------------------- #
# Vault git ops (subprocess `git -C`)
# --------------------------------------------------------------------------- #

def _run_git(vault: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        tail = _CONTROL_CHARS_RE.sub("", ((proc.stderr or "") + (proc.stdout or "")).strip())
        raise RuntimeError(f"git {' '.join(args)} failed: {tail}")
    return proc


def _git_config_value(vault: Path, key: str) -> str:
    try:
        proc = _run_git(vault, ["config", "--get", key], check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _commit(vault: Path, message: str) -> None:
    """Stage everything and commit. A configured git identity is honored;
    otherwise a jacked fallback identity is injected via ``-c`` so commits work
    in CI. A "nothing to commit" is treated as a no-op, not an error.
    """
    _run_git(vault, ["add", "-A"])
    extra: list[str] = []
    if not _git_config_value(vault, "user.name"):
        extra += ["-c", f"user.name={_GIT_FALLBACK_NAME}"]
    if not _git_config_value(vault, "user.email"):
        extra += ["-c", f"user.email={_GIT_FALLBACK_EMAIL}"]
    proc = _run_git(vault, [*extra, "commit", "-q", "-m", message], check=False)
    if proc.returncode != 0:
        blob = ((proc.stdout or "") + (proc.stderr or "")).lower()
        if "nothing to commit" in blob or "no changes added" in blob:
            return
        tail = _CONTROL_CHARS_RE.sub("", ((proc.stderr or "") + (proc.stdout or "")).strip())
        raise RuntimeError(f"git commit failed: {tail}")


def commit_all(vault: Path, message: str) -> None:
    """Public commit wrapper for callers (add/init) that mutate the vault."""
    _commit(vault, message)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

_README = """# jacked memory vault

This is jacked's cross-repo memory. It is a plain git repo full of markdown that
agents read with normal file tools (grep + the per-group `index.md`). There is no
database, no embeddings, and no server.

## Layout

```
vault/
  README.md                 this file
  vault.json                repo -> group mapping (version 1)
  groups/<group>/
    index.md                one line per note, with a hook phrase
    hot.md                  current progress slice (overwritten, not appended)
    decision/*.md           typed atomic notes, one fact per file
    convention/*.md  vision/*.md  reference/*.md  progress/*.md
    episodic/<repo>/        session history (added by later milestones)
  strategy/                 cross-group notes
  archive/                  retired notes
```

## Notes

Every note is one fact in its own file with validated frontmatter:

```
---
type: decision            # decision | convention | vision | reference | progress
repos:
  - github.com/owner/repo
group: <group>
created: 2026-07-18
updated: 2026-07-18
tags:
  - candidate             # marks an unreviewed auto-capture
---

The fact, written in the vocabulary a future query would use.
```

## Finding things

Start at the group's `hot.md`, then its `index.md`, then grep the note bodies.
`jacked memory search <query>` does exactly this, in that order.
"""


def init_vault(vault: Path, roots: list[str], groups: "OrderedDict[str, list[str]] | dict") -> dict:
    """Create and commit a fresh vault. Assumes the caller already confirmed the
    vault is not initialized. Returns a summary dict.
    """
    vault = Path(vault)
    vault.mkdir(parents=True, exist_ok=True)

    # git init (idempotent; -q keeps it quiet). Only init if not already a repo.
    if not (vault / ".git").exists():
        _run_git(vault, ["init", "-q"])

    (vault / "README.md").write_text(_README, encoding="utf-8")
    (vault / "strategy").mkdir(exist_ok=True)
    (vault / "archive").mkdir(exist_ok=True)

    cfg = _empty_config(roots)
    for group, identities in groups.items():
        for ident in identities:
            register_repo(cfg, ident, group)
        ensure_group(vault, group)
    # A vault with no discovered repos still needs a home; nothing to force here.
    save_vault_config(vault, cfg)

    _commit(vault, "Initialize memory vault")
    return {
        "vault_dir": str(vault),
        "groups": {g: cfg["groups"].get(g, {}).get("repos", []) for g in cfg["groups"]},
        "roots": list(roots),
    }


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #

def _unique_slug(gdir: Path, note_type: str, base_slug: str) -> str:
    """A slug unique within the type subdir: base, then base-2, base-3, ..."""
    type_dir = gdir / note_type
    candidate = base_slug
    i = 2
    while (type_dir / f"{candidate}.md").exists():
        candidate = f"{base_slug}-{i}"
        i += 1
    return candidate


def _first_sentence(body: str) -> str:
    """First sentence of the body, whitespace-collapsed, for the index hook."""
    text = (body or "").strip()
    if not text:
        return ""
    m = re.search(r"[.!?](\s|$)", text)
    first = text[: m.start() + 1] if m else text.splitlines()[0]
    return " ".join(first.split())


def _append_index_line(vault: Path, group: str, title: str, note_type: str, slug: str, hook: str) -> None:
    index = group_dir(vault, group) / "index.md"
    safe_title = " ".join((title or "").split())
    # Plain hyphen separator: index lines get echoed verbatim by `memory search`,
    # making them user-facing CLI output (the no-em-dash rule applies).
    line = f"- [{safe_title}]({note_type}/{slug}.md) - {hook} (updated {today()})\n"
    with index.open("a", encoding="utf-8") as fh:
        fh.write(line)


def add_note(
    vault: Path,
    home: Path | None,
    *,
    note_type: str,
    title: str,
    group: str,
    repos: list[str],
    tags: list[str] | None,
    body: str,
    commit: bool = True,
) -> dict:
    """Write one note, update the group index, bump drift, and commit the vault.

    Assumes ``group`` already exists (the CLI ensures/creates it, including the
    on-the-fly solo group for an unmapped repo, before calling). Returns a
    summary dict with the written path (relative to the vault) and the slug.

    ``commit=False`` writes the note + index line + drift bump but defers the
    vault git commit to the caller. The capture hook uses this to fold several
    writes (episodic entries + at most one semantic note per session) into a
    single vault commit per run instead of one commit per note.
    """
    vault = Path(vault)
    group = _sanitize_name(group)
    gdir = ensure_group(vault, group)

    date = today()
    meta = {
        "type": note_type,
        "repos": list(repos),
        "group": group,
        "created": date,
        "updated": date,
        "tags": list(tags or []),
    }
    validate_note(meta)

    base_slug = slugify(title)
    slug = _unique_slug(gdir, note_type, base_slug)

    type_dir = gdir / note_type
    type_dir.mkdir(parents=True, exist_ok=True)
    note_path = type_dir / f"{slug}.md"

    # Containment belt-and-suspenders: the resolved note path must stay inside
    # the group dir (slugify already forbids separators/traversal).
    if not _within(note_path, gdir):
        raise ValueError("refusing to write a note outside its group directory")

    note_path.write_text(render_note(meta, body), encoding="utf-8")
    _append_index_line(vault, group, title, note_type, slug, _first_sentence(body))

    bump_drift(home, 1)
    if commit:
        _commit(vault, f"memory: add {note_type} '{slug}' to {group}")

    return {
        "path": str(note_path.relative_to(vault)),
        "abs_path": str(note_path),
        "group": group,
        "slug": slug,
        "type": note_type,
    }


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# search (tiered: hot -> index -> note bodies -> episodic)
# --------------------------------------------------------------------------- #

def _group_names(vault: Path) -> list[str]:
    groups_root = Path(vault) / "groups"
    if not groups_root.is_dir():
        return []
    return sorted(d.name for d in groups_root.iterdir() if d.is_dir())


def _scan_file(vault: Path, path: Path, needle: str, out: list, limit: int) -> None:
    if len(out) >= limit:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    rel = str(path.relative_to(vault))
    for lineno, line in enumerate(text.splitlines(), 1):
        if needle in line.lower():
            # Vault content is echoed to the terminal; a note body could carry
            # an escape/OSC sequence, so strip control chars at the source.
            out.append((rel, lineno, _CONTROL_CHARS_RE.sub("", line.strip())))
            if len(out) >= limit:
                return


def search(
    vault: Path,
    query: str,
    *,
    group: str | None = None,
    note_type: str | None = None,
    limit: int = 20,
) -> list[tuple[str, int, str]]:
    """Case-insensitive substring search over the vault, tiered.

    Order: every group's ``hot.md``, then ``index.md``, then note bodies, then
    episodic. ``group`` scopes to one group. ``note_type`` scopes the note-body
    tier to that type subdir (the other tiers are not typed). Stops at ``limit``.
    Returns ``[(relpath, lineno, line), ...]``.
    """
    vault = Path(vault)
    needle = (query or "").lower()
    results: list[tuple[str, int, str]] = []
    if not needle or not (vault / "groups").is_dir():
        return results

    groups = [group] if group else _group_names(vault)
    groups = [g for g in groups if group_dir(vault, g).is_dir()]
    types = (note_type,) if note_type else VALID_TYPES

    # Tier 1: hot.md
    for g in groups:
        _scan_file(vault, group_dir(vault, g) / "hot.md", needle, results, limit)
    # Tier 2: index.md
    for g in groups:
        _scan_file(vault, group_dir(vault, g) / "index.md", needle, results, limit)
    # Tier 3: note bodies
    for g in groups:
        if len(results) >= limit:
            break
        for t in types:
            tdir = group_dir(vault, g) / t
            if not tdir.is_dir():
                continue
            for note in sorted(tdir.glob("*.md")):
                _scan_file(vault, note, needle, results, limit)
                if len(results) >= limit:
                    break
    # Tier 4: episodic
    for g in groups:
        if len(results) >= limit:
            break
        edir = group_dir(vault, g) / "episodic"
        if not edir.is_dir():
            continue
        for note in sorted(edir.rglob("*.md")):
            _scan_file(vault, note, needle, results, limit)
            if len(results) >= limit:
                break

    return results[:limit]


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def note_counts(vault: Path, group: str) -> dict:
    """Count of notes per type in a group."""
    gdir = group_dir(vault, group)
    counts = {}
    for t in VALID_TYPES:
        tdir = gdir / t
        counts[t] = len(list(tdir.glob("*.md"))) if tdir.is_dir() else 0
    return counts


def status(vault: Path | None = None, home: Path | None = None) -> dict:
    """Assemble the status snapshot the CLI renders."""
    vault = vault_dir() if vault is None else Path(vault)
    state = load_state(home)
    initialized = is_initialized(vault)

    groups: dict = {}
    if initialized:
        cfg = load_vault_config(vault)
        cfg_groups = cfg.get("groups", {})
        # Enumerate real group dirs on disk (a group created via an explicit
        # --group is not repo-mapped in vault.json), unioned with any config
        # groups that have no notes yet.
        names = set(_group_names(vault)) | set(cfg_groups)
        for name in sorted(names):
            groups[name] = {
                "repos": cfg_groups.get(name, {}).get("repos", []),
                "counts": note_counts(vault, name),
            }

    return {
        "vault_dir": str(vault),
        "initialized": initialized,
        "enabled": bool(state.get("enabled")),
        "triage_model": state.get("triage_model", DEFAULT_TRIAGE_MODEL),
        "groups": groups,
        "drift_added": int(state.get("drift_added", 0)),
        "drift_threshold": int(state.get("drift_threshold", DEFAULT_DRIFT_THRESHOLD)),
        "last_rollup": state.get("last_rollup"),
        "last_sync": state.get("last_sync"),
        "last_sync_error": state.get("last_sync_error"),
        "retry_pending": len(state.get("retry_queue", []) or []),
    }
