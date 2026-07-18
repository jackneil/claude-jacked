"""Migrate the third-party ``remember`` plugin's ``.remember/`` history into the
jacked memory vault, and retire the plugin -- the data-integrity milestone.

The one binding rule here is that the source ``.remember/`` dirs are treated as
READ-ONLY, forever. Migration only ever *reads* them (no write, chmod, rename or
delete touches a source), copies their history into
``groups/<group>/episodic/<repo-short>/``, and VERIFIES the imported entry counts
against the source before anything lands in the vault. Any per-file mismatch
fails that repo loud: its staging is discarded, nothing for it lands in the
vault, and the overall run reports a nonzero failure.

The flow per discovered ``(repo, .remember)`` (see the design doc, section 4):

1. SOURCE COUNT  -- count episodic entries per file with the plugin's own header
   regexes (``consolidate.py:32`` + the 12h form from ``save-session.sh:192``).
   A file with content but no matching header counts as one opaque blob (it still
   migrates verbatim).
2. BUILD (staging) -- copy today/recent/archive/core-memories/identity through
   VERBATIM (malformed history included -- migration never "fixes" it) into a
   temp dir; fold a non-empty ``now.md`` into the current day's today file under a
   ``## migrated-now`` section; turn each ``core-memories.md`` entry into a
   candidate decision note. Never overwrite existing vault content -- a name
   collision gets a ``.migrated-<n>`` suffix.
3. VERIFY -- re-count the staged files with the same regexes; staged must equal
   source per file (the now.md fold-in and the candidate notes are ADDITIONS
   tracked separately in the report, not replacements). Any mismatch fails the
   repo.
4. COMMIT -- only after verify passes, move staging into the vault and register
   the repo/group. One vault commit at the end covers every successful repo.

Plugin retirement disables ``remember@claude-plugins-official`` in
``<home>/.claude/settings.json`` using the same explicit ``enabledPlugins[id] =
False`` contract as ``features.toggle_claude_plugin`` -- never ``.pop`` (an absent
key reads as "enabled by default"). Sources are never removed by retirement.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from jacked.memory import vault as vault_mod

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# The remember plugin's marketplace id, in the ``<plugin>@<marketplace>`` shape
# that jacked's own plugin toggle writes into ``enabledPlugins`` (see
# features.toggle_claude_plugin + tests/unit/test_plugin_toggle.py).
REMEMBER_PLUGIN_ID = "remember@claude-plugins-official"

# The single-file memory artifacts the plugin maintains (besides the dated
# ``today-*.md`` staging files). Presence of any of these -- or a today file --
# marks a real ``.remember`` dir worth migrating.
_SINGLETON_FILES = ("now.md", "recent.md", "archive.md", "core-memories.md", "identity.md")

# The plugin's own episodic entry-header regexes, kept verbatim so migration
# counts entries the exact way the plugin does:
#   consolidate.py:32   ^## (\d{2}:\d{2}|Week of |\d{4}-\d{2}-\d{2})
#   save-session.sh:192 12h form  ^## (\d{1,2}:\d{2} (AM|PM)) \|
# A time-range header (``## 08:48-09:22 | branch``) matches on its leading time.
_ENTRY_HEADER_RE = re.compile(
    r"^## (?:\d{2}:\d{2}|\d{1,2}:\d{2} (?:AM|PM)|Week of |\d{4}-\d{2}-\d{2})"
)

# now.md is not copied as its own file; its live buffer folds into the current
# day's today file under this section marker. The marker is not an episodic
# entry header, so it never inflates a count.
_MIGRATED_NOW_MARKER = "## migrated-now"

# One verification unit: recount ``staged_path`` (optionally only a region of it)
# and require it to equal ``expected`` (a source-derived count).
_Verify = namedtuple("_Verify", "source_name staged_path region expected")


# --------------------------------------------------------------------------- #
# Entry counting (source + staged share the core counter)
# --------------------------------------------------------------------------- #

def _count_entries(text: str) -> int:
    """Episodic entries in ``text`` by the plugin's header regexes.

    A file with content but zero matching headers counts as one opaque blob (it
    still migrates verbatim); an empty/whitespace file counts as zero.
    """
    if not text or not text.strip():
        return 0
    n = sum(1 for line in text.splitlines() if _ENTRY_HEADER_RE.match(line))
    return n if n > 0 else 1


def _staged_entry_count(path: Path, *, region: str | None = None) -> int:
    """Re-count entries in a STAGED file -- the independent verify-pass counter.

    ``region`` isolates part of a combined today file: ``"today"`` counts only
    the entries before the ``## migrated-now`` marker (the today file's own),
    ``"now"`` counts only the folded-in now.md entries after it, ``None`` counts
    the whole file. This is the seam a tamper test monkeypatches to prove a
    corrupted staging is caught.
    """
    text = _read_text(path)
    if region == "today":
        text = _split_on_marker(text)[0]
    elif region == "now":
        text = _split_on_marker(text)[1]
    return _count_entries(text)


def _split_on_marker(text: str) -> tuple[str, str]:
    """``(before, after)`` split of ``text`` on the first ``## migrated-now``
    line (the marker line itself belongs to neither side). No marker -> all
    ``before``."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _MIGRATED_NOW_MARKER:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return text, ""


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _recognized_files(remember_dir: Path) -> list[Path]:
    """Recognizable memory files inside a ``.remember`` dir: the singletons plus
    every ``today-*.md`` (which also matches the ``today-*.done.md`` markers)."""
    found: list[Path] = []
    for name in _SINGLETON_FILES:
        p = remember_dir / name
        if p.is_file():
            found.append(p)
    for p in sorted(remember_dir.glob("today-*.md")):
        if p.is_file():
            found.append(p)
    return found


def _enclosing_repo(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` (inclusive) that holds a ``.git``, or
    None -- so a cwd outside the configured roots still gets its repo migrated."""
    try:
        p = Path(start).resolve()
    except OSError:
        return None
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def discover_remember_dirs(roots) -> list[tuple[Path, Path]]:
    """Discover ``(repo_path, remember_dir)`` for every migratable ``.remember``.

    Scans each ``<root>/<repo>/.remember/`` (immediate children of the roots)
    plus the cwd's enclosing repo (so a repo outside the roots still migrates).
    Only dirs that actually contain a recognizable memory file are returned;
    deduped by resolved repo path and sorted for determinism.
    """
    out: list[tuple[Path, Path]] = []
    seen: set = set()

    def _consider(repo_path: Path) -> None:
        remember_dir = repo_path / ".remember"
        try:
            if not remember_dir.is_dir() or not _recognized_files(remember_dir):
                return
            key = repo_path.resolve()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        out.append((repo_path, remember_dir))

    for root in roots or []:
        root = Path(root)
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_dir():
                    _consider(child)
            except OSError:
                continue

    repo = _enclosing_repo(Path.cwd())
    if repo is not None:
        _consider(repo)

    out.sort(key=lambda t: str(t[0]))
    return out


# --------------------------------------------------------------------------- #
# Preview (read-only; what a migrate would import)
# --------------------------------------------------------------------------- #

def preview(vault: Path, *, roots) -> list[dict]:
    """A read-only summary of what ``migrate`` would import, per repo:
    ``{repo, group, repo_short, files (count), entries (count)}``. Used by the
    CLI's pre-confirmation table and by ``memory status``."""
    rows: list[dict] = []
    for repo_path, remember_dir in discover_remember_dirs(roots):
        _identity, group, repo_short = _resolve_target(vault, repo_path)
        files = _recognized_files(remember_dir)
        entries = 0
        for f in files:
            entries += _count_entries(_read_text(f))
        rows.append({
            "repo": str(repo_path),
            "group": group,
            "repo_short": repo_short,
            "files": len(files),
            "entries": entries,
        })
    return rows


# --------------------------------------------------------------------------- #
# Target resolution (pure -- NO vault mutation during the build pass)
# --------------------------------------------------------------------------- #

def _resolve_target(vault: Path, repo_path: Path) -> tuple[str, str, str]:
    """``(identity, group, repo_short)`` for a repo, WITHOUT touching the vault.

    Mirrors ``capture._resolve_group_and_repo`` but omits its ``ensure_group`` /
    ``register_repo`` side effects, so a repo that later fails verification never
    leaves a group skeleton or vault.json entry behind. Registration is deferred
    to the commit pass (``_materialize``), which only runs for repos that pass.
    """
    identity = vault_mod.repo_identity(repo_path)
    repo_short = vault_mod.group_for_identity(identity)
    group = vault_mod.resolve_group(vault, identity) or vault_mod.group_for_identity(identity)
    return identity, group, repo_short


# --------------------------------------------------------------------------- #
# core-memories.md -> candidate decision notes
# --------------------------------------------------------------------------- #

def _title_from_line(line: str) -> str:
    return line.lstrip("#").strip() or "core memory"


def _core_memory_entries(text: str) -> list[tuple[str, str]]:
    """``[(title, body), ...]`` for the entries in a ``core-memories.md``.

    Entries are the ``## `` sections (title from the section's first line). A file
    with no ``## `` sections but with content is one entry (title from its first
    non-empty line). The file itself is still copied verbatim; these become
    additional candidate notes, tracked separately from the file's entry count.
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    idxs = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    if not idxs:
        first = next((ln for ln in lines if ln.strip()), "core memory")
        return [(_title_from_line(first), text.strip())]
    entries: list[tuple[str, str]] = []
    for k, start in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        block = "\n".join(lines[start:end]).strip()
        entries.append((_title_from_line(lines[start]), block))
    return entries


# --------------------------------------------------------------------------- #
# Collision-safe target names (never overwrite existing vault content)
# --------------------------------------------------------------------------- #

def _noncolliding_name(target_dir: Path, base_name: str) -> str:
    """``base_name`` if free in ``target_dir``, else ``<stem>.migrated-<n>.<ext>``
    (n from 2). The vault's existing episodic files are never overwritten."""
    if not (target_dir / base_name).exists():
        return base_name
    stem, _dot, ext = base_name.rpartition(".")
    if not stem:  # dotfile with no extension -- unreachable for our names, safe
        stem, ext = base_name, ""
    i = 2
    while True:
        candidate = f"{stem}.migrated-{i}.{ext}" if ext else f"{stem}.migrated-{i}"
        if not (target_dir / candidate).exists():
            return candidate
        i += 1


# --------------------------------------------------------------------------- #
# Per-repo migration (source count -> build -> verify)
# --------------------------------------------------------------------------- #

def _today_local() -> str:
    """Current local date ``YYYY-MM-DD`` -- matches ``capture._write_episodic``'s
    today-file naming so the now.md fold-in lands in the right day file."""
    return datetime.now().strftime("%Y-%m-%d")


def _migrate_one(vault: Path, repo_path: Path, remember_dir: Path, staging_root: Path) -> dict:
    """Source-count, build (into a temp dir) and verify one repo.

    Returns ``{"report": <per-repo report>, "plan": <commit plan or None>}``. The
    plan (staged moves + candidate-note specs + target group) is present only
    when the repo passed verification; the caller materializes it into the vault.
    """
    identity, group, repo_short = _resolve_target(vault, repo_path)
    vault_episodic = vault_mod.group_dir(vault, group) / "episodic" / repo_short
    stage_dir = Path(tempfile.mkdtemp(prefix="repo-", dir=staging_root))

    files_report: dict = {}
    moves: list[tuple[Path, Path]] = []
    verifications: list[_Verify] = []
    candidate_specs: list[dict] = []

    def _base_report(status: str, reason: str | None, candidates: int) -> dict:
        return {
            "repo": str(repo_path),
            "identity": identity,
            "group": group,
            "repo_short": repo_short,
            "files": files_report,
            "candidates_created": candidates,
            "status": status,
            "reason": reason,
        }

    current_today_name = f"today-{_today_local()}.md"

    # now.md: read once; it folds into the current day's today file (not copied
    # as its own file). Only a non-empty buffer folds in.
    now_text = ""
    now_entries = 0
    now_file = remember_dir / "now.md"
    if now_file.is_file():
        now_text = now_file.read_bytes().decode("utf-8", "replace")
        if now_text.strip():
            now_entries = _count_entries(now_text)
    now_folds = now_entries > 0

    staged_current_today: Path | None = None
    current_today_source_entries = 0

    # today-*.md (and .done.md) then the singletons -- all copied VERBATIM.
    today_files = sorted(p for p in remember_dir.glob("today-*.md") if p.is_file())
    singletons = [remember_dir / n for n in _SINGLETON_FILES if n != "now.md"]
    verbatim = [*today_files, *(s for s in singletons if s.is_file())]

    for src in verbatim:
        src_bytes = src.read_bytes()
        src_text = src_bytes.decode("utf-8", "replace")
        src_entries = _count_entries(src_text)
        name = _noncolliding_name(vault_episodic, src.name)
        staged_path = stage_dir / name
        staged_path.write_bytes(src_bytes)

        files_report[src.name] = {"source_entries": src_entries, "staged_entries": None}
        moves.append((staged_path, vault_episodic / name))

        if src.name == current_today_name:
            # Verification deferred: whether we check the whole file or only the
            # pre-``migrated-now`` region depends on the now.md fold-in below.
            staged_current_today = staged_path
            current_today_source_entries = src_entries
        else:
            verifications.append(_Verify(src.name, staged_path, None, src_entries))

        if src.name == "core-memories.md":
            for c_title, c_body in _core_memory_entries(src_text):
                candidate_specs.append({
                    "note_type": "decision",
                    "title": c_title,
                    "group": group,
                    "repos": [identity],
                    "tags": ["candidate", "from-core-memories"],
                    "body": c_body,
                })

    # Fold now.md into the current day's today file (create it if the source has
    # no today file for today).
    if now_folds:
        if staged_current_today is None:
            name = _noncolliding_name(vault_episodic, current_today_name)
            staged_current_today = stage_dir / name
            staged_current_today.write_text(
                f"{_MIGRATED_NOW_MARKER}\n{now_text}", encoding="utf-8"
            )
            moves.append((staged_current_today, vault_episodic / name))
        else:
            base = staged_current_today.read_text(encoding="utf-8")
            combined = base.rstrip("\n") + "\n\n" + _MIGRATED_NOW_MARKER + "\n" + now_text
            staged_current_today.write_text(combined, encoding="utf-8")
            # The today file's own entries are verified excluding the folded-in
            # region, so its per-file staged count still equals its source count.
            verifications.append(
                _Verify(current_today_name, staged_current_today, "today", current_today_source_entries)
            )
        verifications.append(_Verify("now.md", staged_current_today, "now", now_entries))
        files_report["now.md"] = {
            "source_entries": now_entries,
            "staged_entries": None,
            "folded_into": staged_current_today.name,
        }
    elif staged_current_today is not None:
        verifications.append(
            _Verify(current_today_name, staged_current_today, None, current_today_source_entries)
        )

    # Nothing with content to migrate (e.g. a lone empty now.md).
    if not moves and not candidate_specs:
        return {"report": _base_report("skipped", "no content to migrate", 0), "plan": None}

    # VERIFY: independently re-count the staged files; any mismatch fails the repo.
    mismatches: list[str] = []
    for v in verifications:
        actual = _staged_entry_count(v.staged_path, region=v.region)
        files_report[v.source_name]["staged_entries"] = actual
        if actual != v.expected:
            mismatches.append(f"{v.source_name}: expected {v.expected}, staged {actual}")

    if mismatches:
        return {"report": _base_report("failed", "; ".join(mismatches), 0), "plan": None}

    plan = {
        "identity": identity,
        "group": group,
        "repo_short": repo_short,
        "moves": moves,
        "candidate_specs": candidate_specs,
    }
    return {"report": _base_report("migrated", None, len(candidate_specs)), "plan": plan}


def _materialize(vault: Path, home: Path | None, plan: dict) -> None:
    """Move a verified repo's staged files into the vault and register it.

    Runs only for repos that passed verification. Registers the group + repo in
    vault.json (if unmapped), creates the group skeleton + episodic dir, moves
    each staged file to its collision-free final path, and stages each candidate
    note (``commit=False`` -- the caller does the single vault commit).
    """
    identity = plan["identity"]
    group = plan["group"]

    if not vault_mod.resolve_group(vault, identity):
        cfg = vault_mod.load_vault_config(vault)
        vault_mod.register_repo(cfg, identity, group)
        vault_mod.save_vault_config(vault, cfg)
    vault_mod.ensure_group(vault, group)

    for staged_path, final_path in plan["moves"]:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_path), str(final_path))

    for spec in plan["candidate_specs"]:
        # A candidate note is a convenience extraction, not the migrated episodic
        # data (which is already verified + moved). A single malformed entry must
        # never abort a verified migration mid-commit -- fail open and log, as
        # capture._write_semantic does.
        try:
            vault_mod.add_note(vault, home, commit=False, **spec)
        except (ValueError, RuntimeError):
            logger.debug("memory migrate: candidate note write failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Public: migrate
# --------------------------------------------------------------------------- #

def migrate(vault: Path, home: Path | None, *, roots) -> dict:
    """Migrate every discovered ``.remember`` dir into the vault, verifying counts.

    Sources are read-only throughout. Each repo is built into a temp staging dir
    and verified independently; only repos that pass are materialized into the
    vault, and a single commit covers them all. Returns the report dict:

    ``{repos: {repo_path: {files, candidates_created, status, reason, ...}},
       repos_migrated, repos_failed, total_files, total_entries,
       candidates_created}``.
    """
    report = {
        "repos": {},
        "repos_migrated": 0,
        "repos_failed": 0,
        "total_files": 0,
        "total_entries": 0,
        "candidates_created": 0,
    }

    discovered = discover_remember_dirs(roots)
    if not discovered:
        return report

    staging_root = Path(tempfile.mkdtemp(prefix="jacked-migrate-"))
    try:
        plans: list[dict] = []
        for repo_path, remember_dir in discovered:
            result = _migrate_one(vault, repo_path, remember_dir, staging_root)
            rep = result["report"]
            report["repos"][str(repo_path)] = rep
            if rep["status"] == "failed":
                report["repos_failed"] += 1
            elif result["plan"] is not None:
                plans.append(result["plan"])

        # COMMIT PASS: materialize only the verified repos, then one commit.
        for plan in plans:
            _materialize(vault, home, plan)
            report["repos_migrated"] += 1

        for rep in report["repos"].values():
            if rep["status"] == "migrated":
                report["total_files"] += len(rep["files"])
                report["total_entries"] += sum(
                    f["source_entries"] for f in rep["files"].values()
                )
                report["candidates_created"] += rep["candidates_created"]

        if report["repos_migrated"] > 0:
            vault_mod.commit_all(
                vault,
                f"memory: migrate .remember history ({report['repos_migrated']} repos)",
            )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return report


# --------------------------------------------------------------------------- #
# Public: plugin retirement
# --------------------------------------------------------------------------- #

def _settings_path(home: Path | None) -> Path:
    home = vault_mod.jacked_home() if home is None else Path(home)
    return home / ".claude" / "settings.json"


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict) -> None:
    """Atomically write settings.json (writer-unique tmp + os.replace), matching
    the vault/features atomic-write pattern."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def retire_remember_plugin(home: Path | None = None) -> dict:
    """Disable the ``remember`` plugin in ``<home>/.claude/settings.json``.

    Uses the same contract as ``features.toggle_claude_plugin``: an explicit
    ``enabledPlugins[id] = False`` (never ``.pop`` -- Claude Code treats an absent
    key as enabled-by-default). Every other setting is preserved untouched, and
    no ``.remember`` source is removed. Returns what changed.
    """
    path = _settings_path(home)
    settings = _read_settings(path)
    plugins = settings.get("enabledPlugins")
    if not isinstance(plugins, dict):
        plugins = {}
        settings["enabledPlugins"] = plugins
    previous = plugins.get(REMEMBER_PLUGIN_ID)
    plugins[REMEMBER_PLUGIN_ID] = False
    _write_settings(path, settings)
    return {
        "plugin": REMEMBER_PLUGIN_ID,
        "settings_path": str(path),
        "previous": previous,
        "enabled": False,
        "changed": previous is not False,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""
