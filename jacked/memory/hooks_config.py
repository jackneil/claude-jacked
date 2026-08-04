"""Memory-vault hook entries for settings.json: pure add/remove on a dict.

No file I/O here. The CLI installer (``jacked/cli.py``) and the dashboard
Features route (``jacked/api/routes/features.py``, wired in M7) both call these
so the entry math lives in exactly one place and can't drift.

Identification anchors on the module-name substring in the entry command
(``memory_capture`` / ``memory_recall``), mirroring how the CLI's SessionStart
installer anchors on its own hook names (``cli._is_jacked_session_start_entry``).
That keeps us from ever touching a foreign hook, or the sibling jacked hooks
that also live under ``SessionStart``/``SessionEnd``.

Capture entries are async (``"async": True``): Claude Code is not blocked while
the SessionEnd/PreCompact triage runs. The recall entry is SYNCHRONOUS (no
``async`` key) because an async SessionStart hook's stdout is NOT injected into
the session context, and injecting the brief is the entire point. On a current
install there is no standalone recall entry at all: jacked's ONE combined
``session_start`` entry runs the brief in a fixed order with the other
session-start steps, so the injected preamble is byte-identical every session.
"""
from __future__ import annotations

# Capture fires on session end and on auto-compaction. Both async.
CAPTURE_EVENTS = [("SessionEnd", ""), ("PreCompact", "auto")]

# Recall is a single synchronous SessionStart hook. Installed in M4; the
# constant + ensure/remove helpers live here now so both call sites share them.
RECALL_EVENT = ("SessionStart", "")

_CAPTURE_ANCHOR = "memory_capture"
_RECALL_ANCHOR = "memory_recall"

# jacked's combined SessionStart entry (``jacked _hook session_start``) runs the
# recall brief itself, in a fixed order with the other session-start steps, so
# that recall never races a sibling entry. When it is present, recall is ALREADY
# wired: a second standalone entry would inject the brief twice and put the
# ordering race back. Enable therefore no-ops and detection reports installed.
_SESSION_START_ANCHOR = "session_start"


def _entry_has_anchor(entry: dict, anchor: str) -> bool:
    """True if any hook command inside ``entry`` contains ``anchor``."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []) or []:
        if isinstance(h, dict) and anchor in str(h.get("command", "")):
            return True
    return False


def _make_entry(matcher: str, command: str, *, is_async: bool) -> dict:
    """Build one settings.json hook entry (async key omitted when not async)."""
    hook: dict = {"type": "command", "command": command}
    if is_async:
        hook["async"] = True
    return {"matcher": matcher, "hooks": [hook]}


def _ensure(
    settings: dict,
    events: list[tuple[str, str]],
    command: str,
    anchor: str,
    *,
    is_async: bool,
) -> bool:
    """Ensure a jacked-owned entry exists for each (event, matcher) in ``events``.

    Match-then-update-in-place: an existing jacked entry (identified by
    ``anchor``) with a stale command is replaced (path/shim migration); a
    matching entry is left alone; a missing entry is appended. Foreign entries
    in the same event array are never inspected beyond their command anchor and
    never modified. Returns True if ``settings`` changed.
    """
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event_name, matcher in events:
        arr = hooks.setdefault(event_name, [])
        if not isinstance(arr, list):
            arr = []
            hooks[event_name] = arr

        idx = None
        for i, entry in enumerate(arr):
            if not isinstance(entry, dict):
                continue
            if entry.get("matcher", "") != matcher:
                continue
            if _entry_has_anchor(entry, anchor):
                idx = i
                break

        new_entry = _make_entry(matcher, command, is_async=is_async)
        if idx is None:
            arr.append(new_entry)
            changed = True
        elif arr[idx] != new_entry:
            arr[idx] = new_entry
            changed = True
    return changed


def _remove(settings: dict, anchor: str) -> bool:
    """Strip every jacked-owned entry (by ``anchor``) from every event array.

    Robust to a stale entry that somehow landed under an unexpected event.
    Never touches foreign entries. Returns True if anything was removed.
    """
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event_name, arr in list(hooks.items()):
        if not isinstance(arr, list):
            continue
        kept = [e for e in arr if not _entry_has_anchor(e, anchor)]
        if len(kept) != len(arr):
            hooks[event_name] = kept
            changed = True
    return changed


def ensure_capture_entries(settings: dict, command: str) -> bool:
    """Ensure the SessionEnd + PreCompact(auto) capture entries exist (async)."""
    return _ensure(settings, CAPTURE_EVENTS, command, _CAPTURE_ANCHOR, is_async=True)


def remove_capture_entries(settings: dict) -> bool:
    """Remove the jacked memory-capture entries. Returns True if changed."""
    return _remove(settings, _CAPTURE_ANCHOR)


def ensure_recall_entry(settings: dict, command: str) -> bool:
    """Ensure the SYNCHRONOUS SessionStart recall entry exists (no async key).

    No-op when the combined ``session_start`` entry is installed: it already
    runs the brief, and a second entry would both duplicate the injection and
    reintroduce the completion-order race between SessionStart entries.
    """
    if has_session_start_entry(settings):
        return False
    return _ensure(settings, [RECALL_EVENT], command, _RECALL_ANCHOR, is_async=False)


def remove_recall_entries(settings: dict) -> bool:
    """Remove the STANDALONE jacked memory-recall entries. Returns True if changed.

    The combined ``session_start`` entry is deliberately left alone: it serves
    the other session-start features too, and its recall step already no-ops
    while the vault is disabled. Only a full uninstall (the last feature out)
    removes that entry.
    """
    return _remove(settings, _RECALL_ANCHOR)


def has_capture_entry(settings: dict) -> bool:
    """True if any jacked memory-capture entry is present in ``settings``.

    Anchors on the same ``memory_capture`` command substring the installer uses,
    so the dashboard's installed-state detection shares one source of truth with
    ensure/remove and never re-implements the string math. Capture is the marker
    of "the feature is on": recall alone never installs without it.
    """
    return _has_entry(settings, _CAPTURE_ANCHOR)


def has_recall_entry(settings: dict) -> bool:
    """True if recall is wired: a standalone entry OR the combined one.

    Used by the status honesty cross-check: capture without recall means the
    SessionStart brief silently never injects, which is worth a warning. The
    combined ``session_start`` entry runs the brief itself, so it counts.
    """
    return _has_entry(settings, _RECALL_ANCHOR) or has_session_start_entry(settings)


def has_session_start_entry(settings: dict) -> bool:
    """True if jacked's combined SessionStart entry is present in ``settings``."""
    return _has_entry(settings, _SESSION_START_ANCHOR)


def _has_entry(settings: dict, anchor: str) -> bool:
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for arr in hooks.values():
        if not isinstance(arr, list):
            continue
        if any(_entry_has_anchor(entry, anchor) for entry in arr):
            return True
    return False
