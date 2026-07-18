"""Deterministic episodic rollup: today files -> recent.md -> archive.md.

This is pure code -- NO LLM anywhere. It runs from the ``jacked memory rollup``
CLI command and as a tail-call at the end of a SessionEnd capture. It walks every
``groups/<group>/episodic/<repo>/`` dir and does two things, in order:

1. Every ``today-YYYY-MM-DD.md`` whose date is BEFORE today (UTC) is folded into
   ``recent.md`` as one ``## YYYY-MM-DD`` day section (its ``## HH:MM | branch``
   entries demoted to ``### HH:MM | branch`` so the day nests cleanly). The
   processed file is then RENAMED to ``today-YYYY-MM-DD.done.md`` -- the remember
   plugin's "processed" marker, which migration recognizes. It is never deleted.
2. Every ``## YYYY-MM-DD`` section in ``recent.md`` older than the archive
   boundary is moved into ``archive.md`` under a ``## Week of <monday>`` section
   (the day section demoted one heading level so week > day > time nests).

The whole thing is idempotent: content is recomputed and written only when the
bytes actually change, and the single vault commit happens only when something
moved. Malformed content is TOLERATED, never dropped: a today file with no
``##`` entries, or a stray non-day ``##`` section in recent.md, is left exactly
where it is and counted as ``skipped_unparseable``.

Format shapes are preserved from the remember plugin (recent.md = ``# Recent`` +
``## YYYY-MM-DD`` sections; archive.md = ``# Archive`` + ``## Week of YYYY-MM-DD``
sections) so migration and human readers keep continuity.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jacked.memory import vault as vault_mod

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants (real constraints, not arbitrary caps)
# --------------------------------------------------------------------------- #

# A recent.md day section moves to the archive once it is OLDER than this many
# days. Boundary pinned by a test: a section exactly 7 days old STAYS in recent;
# 8 days old MOVES. So the move condition is ``age_in_days > 7``.
_ARCHIVE_AFTER_DAYS = 7

_RECENT_HEADER = "# Recent"
_ARCHIVE_HEADER = "# Archive"

# ``today-YYYY-MM-DD.md`` exactly -- the ``.done.md`` marker files never match
# (their name carries ``.done`` between the date and ``.md``).
_TODAY_FILE_RE = re.compile(r"^today-(\d{4}-\d{2}-\d{2})\.md$")
_DAY_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
_WEEK_HEADER_RE = re.compile(r"^##\s+Week of\s+(\d{4}-\d{2}-\d{2})\s*$")
# Any ATX heading of level 1-5 (level 6 is left alone -- can't demote past 6).
_DEMOTABLE_HEADING_RE = re.compile(r"^#{1,5}\s")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def rollup(vault: Path, home: Path | None = None) -> dict:
    """Roll episodic history forward across every episodic dir in the vault.

    Returns a summary dict:
    ``{days_rolled, sections_archived, skipped_unparseable, dirs, changed}``.
    A no-op run (nothing new) returns zeros and ``changed=False`` and makes no
    vault commit. An uninitialized vault is a clean no-op (the CLI prints the
    notice; the capture tail-call already guards initialization upstream).
    """
    vault = Path(vault)
    summary = {
        "days_rolled": 0,
        "sections_archived": 0,
        "skipped_unparseable": 0,
        "dirs": 0,
        "changed": False,
    }
    if not vault_mod.is_initialized(vault):
        return summary

    today = _today()
    for edir in _episodic_dirs(vault):
        summary["dirs"] += 1
        _rollup_dir(edir, today, summary)

    if summary["changed"]:
        vault_mod.commit_all(vault, "memory: rollup episodic history")

    _record_last_rollup(home)
    return summary


# --------------------------------------------------------------------------- #
# Per-dir rollup
# --------------------------------------------------------------------------- #

def _rollup_dir(edir: Path, today: date, summary: dict) -> None:
    recent_path = edir / "recent.md"
    archive_path = edir / "archive.md"

    recent_existed = recent_path.exists()
    preamble, sections = _split_h2(_read_text(recent_path))
    if not recent_existed:
        preamble = _RECENT_HEADER

    archive_existed = archive_path.exists()
    apre, asections = _split_h2(_read_text(archive_path))
    if not archive_existed:
        apre = _ARCHIVE_HEADER

    renamed: list[Path] = []

    # Step 1: past-day today files -> recent day sections.
    for date_str, path in _past_today_files(edir, today):
        day = _day_section_from_today(date_str, _read_text(path))
        if day is None:
            # No parseable ## entries: leave the file untouched, never drop it.
            summary["skipped_unparseable"] += 1
            continue
        _merge_day_section(sections, day)
        renamed.append(path)
        summary["days_rolled"] += 1

    # Step 2: recent day sections older than the boundary -> archive weeks.
    kept: list[tuple[str, str]] = []
    for header, body in sections:
        d = _day_header_date(header)
        if d is None:
            # A stray non-day ## section (doubled header, Identity Candidates,
            # etc.): keep it exactly in place, count it, never archive it.
            summary["skipped_unparseable"] += 1
            kept.append((header, body))
            continue
        if (today - d).days > _ARCHIVE_AFTER_DAYS:
            demoted = _demote_all_headings(_section_text(header, body))
            _append_to_week(asections, _monday_of(d), demoted)
            summary["sections_archived"] += 1
        else:
            kept.append((header, body))
    sections = kept

    # Serialize and write only when the bytes actually change (idempotency).
    new_recent = _render(preamble, sections)
    new_archive = _render(apre, asections)

    wrote = False
    if (recent_existed or sections) and _write_if_differs(recent_path, new_recent):
        wrote = True
    if (archive_existed or asections) and _write_if_differs(archive_path, new_archive):
        wrote = True

    # Rename the processed today files only after recent.md reflects them
    # (write-then-mark: a crash costs at most a duplicate next run, never data).
    for path in renamed:
        _rename_done(path)
        wrote = True

    if wrote:
        summary["changed"] = True


# --------------------------------------------------------------------------- #
# today-file discovery + day-section building
# --------------------------------------------------------------------------- #

def _past_today_files(edir: Path, today: date) -> list[tuple[str, Path]]:
    """``[(date_str, path), ...]`` for ``today-YYYY-MM-DD.md`` files dated before
    ``today``, sorted ascending. ``.done.md`` markers and today's own file are
    excluded."""
    out: list[tuple[str, Path]] = []
    for p in sorted(edir.glob("today-*.md")):
        m = _TODAY_FILE_RE.match(p.name)
        if not m:
            continue
        d = _parse_date(m.group(1))
        if d is not None and d < today:
            out.append((m.group(1), p))
    return sorted(out)


def _day_section_from_today(date_str: str, text: str) -> tuple[str, str] | None:
    """Build a ``(## YYYY-MM-DD, body)`` recent section from a today file, or
    None when the file has no ``##`` entries (unparseable -> skip + count)."""
    if not _has_h2(text):
        return None
    body = _demote_h2(text).strip("\n")
    return (f"## {date_str}", body)


def _rename_done(path: Path) -> None:
    """Rename ``today-X.md`` -> ``today-X.done.md`` (the processed marker). Never
    clobbers an existing marker; never deletes."""
    base = path.name[: -len(".md")]
    done = path.with_name(f"{base}.done.md")
    if done.exists():
        i = 2
        while path.with_name(f"{base}.done-{i}.md").exists():
            i += 1
        done = path.with_name(f"{base}.done-{i}.md")
    path.rename(done)


# --------------------------------------------------------------------------- #
# recent.md day sections
# --------------------------------------------------------------------------- #

def _merge_day_section(sections: list[tuple[str, str]], day: tuple[str, str]) -> None:
    """Append ``day`` to ``sections``; if a section for the same date already
    exists, fold the new body into it instead of duplicating the day header."""
    header, body = day
    target = _day_header_date(header)
    for i, (h, b) in enumerate(sections):
        if target is not None and _day_header_date(h) == target:
            existing = (b or "").strip("\n")
            addition = (body or "").strip("\n")
            sections[i] = (h, f"{existing}\n\n{addition}" if existing else addition)
            return
    sections.append((header, body))


def _day_sections(text: str) -> list[tuple[date, str, str]]:
    """``[(date, header, body), ...]`` for the valid ``## YYYY-MM-DD`` sections of
    a recent.md-shaped text (small + unit-testable)."""
    out: list[tuple[date, str, str]] = []
    _, sections = _split_h2(text)
    for header, body in sections:
        d = _day_header_date(header)
        if d is not None:
            out.append((d, header, body))
    return out


# --------------------------------------------------------------------------- #
# archive.md week sections
# --------------------------------------------------------------------------- #

def _monday_of(d: date) -> date:
    """The Monday of ``d``'s ISO week."""
    return d - timedelta(days=d.weekday())


def _append_to_week(asections: list[tuple[str, str]], monday: date, demoted_day: str) -> None:
    """Append a demoted day section to the ``## Week of <monday>`` archive
    section, creating it if absent, merging into it when present."""
    body_to_add = demoted_day.strip("\n")
    for i, (h, b) in enumerate(asections):
        if _week_header_monday(h) == monday:
            existing = (b or "").strip("\n")
            asections[i] = (h, f"{existing}\n\n{body_to_add}" if existing else body_to_add)
            return
    asections.append((f"## Week of {monday.isoformat()}", body_to_add))


# --------------------------------------------------------------------------- #
# H2 section splitting / rendering (shared by recent + archive)
# --------------------------------------------------------------------------- #

def _is_h2(line: str) -> bool:
    """True for a level-2 ATX heading (``## ...``) exactly -- not ``### ...``."""
    return line.startswith("## ")


def _has_h2(text: str) -> bool:
    return any(_is_h2(line) for line in text.splitlines())


def _split_h2(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split ``text`` on level-2 headings.

    Returns ``(preamble, [(header_line, body), ...])`` where ``preamble`` is
    everything before the first ``## `` line (the ``# Recent`` / ``# Archive``
    header and any leading prose) and each section runs from its header to the
    next ``## `` line or EOF. Bodies are the joined lines between headers.
    """
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    header: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if _is_h2(line):
            if header is not None:
                sections.append((header, body))
            header = line
            body = []
        elif header is None:
            preamble.append(line)
        else:
            body.append(line)
    if header is not None:
        sections.append((header, body))
    return "\n".join(preamble), [(h, "\n".join(b)) for h, b in sections]


def _section_text(header: str, body: str) -> str:
    b = (body or "").strip("\n")
    return header.rstrip("\n") + (f"\n{b}" if b else "")


def _render(preamble: str, sections: list[tuple[str, str]]) -> str:
    """Serialize ``(preamble, sections)`` back to canonical markdown with exactly
    one blank line between blocks and a single trailing newline. A fixed point:
    ``_render(_split_h2(x))`` re-rendered is byte-stable, which is what makes a
    second rollup run a true no-op."""
    chunks: list[str] = []
    pre = (preamble or "").strip("\n")
    if pre:
        chunks.append(pre)
    for header, body in sections:
        chunks.append(_section_text(header, body))
    return "\n\n".join(chunks) + "\n"


# --------------------------------------------------------------------------- #
# Heading demotion
# --------------------------------------------------------------------------- #

def _demote_h2(text: str) -> str:
    """Turn each level-2 heading into level-3 (``## `` -> ``### ``). Only ``## ``
    lines are touched, so a stray ``# `` or ``### `` in a body is left verbatim
    (never promoting a body line into a section boundary)."""
    return "\n".join(
        ("#" + line) if _is_h2(line) else line
        for line in text.splitlines()
    )


def _demote_all_headings(text: str) -> str:
    """Demote every heading (levels 1-5) by one so a whole recent day section
    nests one level deeper under an archive ``## Week of`` header."""
    return "\n".join(
        ("#" + line) if _DEMOTABLE_HEADING_RE.match(line) else line
        for line in text.splitlines()
    )


# --------------------------------------------------------------------------- #
# Header date parsing
# --------------------------------------------------------------------------- #

def _day_header_date(header: str) -> date | None:
    m = _DAY_HEADER_RE.match(header or "")
    return _parse_date(m.group(1)) if m else None


def _week_header_monday(header: str) -> date | None:
    m = _WEEK_HEADER_RE.match(header or "")
    return _parse_date(m.group(1)) if m else None


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# IO helpers
# --------------------------------------------------------------------------- #

def _episodic_dirs(vault: Path) -> list[Path]:
    """Every ``groups/<group>/episodic/<repo>/`` dir, sorted deterministically."""
    dirs: list[Path] = []
    groups_root = vault / "groups"
    if not groups_root.is_dir():
        return dirs
    for g in sorted(groups_root.iterdir()):
        edir = g / "episodic"
        if not edir.is_dir():
            continue
        for repo in sorted(edir.iterdir()):
            if repo.is_dir():
                dirs.append(repo)
    return dirs


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _write_if_differs(path: Path, new_text: str) -> bool:
    """Write ``new_text`` only when it differs from what's on disk. Returns True
    if a write happened (so a no-op run neither rewrites nor commits)."""
    old = _read_text(path) if path.exists() else None
    if old == new_text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True


def _record_last_rollup(home: Path | None) -> None:
    """Stamp ``state.last_rollup`` via a fresh load/save so a concurrent writer's
    drift/queue is not clobbered."""
    state = vault_mod.load_state(home)
    state["last_rollup"] = _now_iso()
    vault_mod.save_state(home, state)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
