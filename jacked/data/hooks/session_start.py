#!/usr/bin/env python3
"""The ONE SessionStart hook: every jacked session-start step, in a fixed order.

Claude Code runs each SessionStart hook ENTRY concurrently and concatenates the
entries' stdout in COMPLETION order. jacked used to register three separate
entries (session tracker, chain-of-command, memory recall), so the emitted
blocks landed in a RANDOM order per session (proven by byte-capture of real
requests). A shuffled preamble is a different prefix every time, which defeats
the inference-side prompt prefix cache. This module is the fix: one synchronous
entry that runs the steps sequentially in-process, so the preamble is
byte-identical from session to session.

Step order is FIXED -- do not reorder, the cache keys on these exact bytes:
  1. session-account tracker (silent; daemon thread, same as its own main())
  2. chain-of-command policy   -> stdout
  3. memory-vault recall brief -> stdout
  4. open work (GitHub issues) -> stdout (from a cache; changes with the queue)
  5. git reality block         -> stdout (changes with the repo, so after 2-4)
  6. load budget warnings      -> stdout (only when a file nears its cap)
  7. integrity tripwire        -> stdout (LAST: only on drift or a quarantine)

Each step is guarded on its own: a failure in one NEVER stops the ones after
it, and nothing here may crash a session start (every path exits 0). Steps 2
and 3 no-op quietly when their feature is off (the chain-of-command skill file
is absent / the memory vault is disabled), so this single entry stays correct
whatever the user has toggled. Step 4 reads the issue queue cache that the
jacked service writes; it starts no subprocess and makes no network call, and
it prints nothing when the cache is missing or 24 hours old, so a machine
without gh adds zero bytes. Step 5 does not depend on the vault: it prints
in every git work tree and prints nothing anywhere else, so a session start
outside a git repo adds zero bytes. Step 6 prints only when a file that Claude
Code loads at session start (MEMORY.md, a CLAUDE.md) is over 90% of its load
cap, so a healthy machine gets zero bytes from it. Step 7 prints only when
settings.json or a hook script changed outside jacked, or when it moved a
suspected injected line out of MEMORY.md (``jacked.integrity``), so a healthy
machine gets zero bytes from it too. It goes last because its text changes
whenever something outside jacked changes.
"""

import json
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# How long we wait on the tracker's DB write before moving on. Same bound the
# tracker's own main() uses -- the thread is a daemon, so a slow write is
# abandoned, never blocking the session past this.
TRACKER_JOIN_TIMEOUT = 2.0


def _read_hook_input() -> dict | None:
    """Read the SessionStart payload from stdin ONCE. None when unusable.

    Empty or invalid stdin returns None and the whole hook stays silent, which
    mirrors ``session_account_tracker.main``.

    >>> callable(_read_hook_input)
    True
    """
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return None
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _start_session_tracker(data: dict) -> threading.Thread | None:
    """Step 1: record which account this session uses. Emits NOTHING.

    Starts the tracker's SessionStart work on a daemon thread and RETURNS the
    thread without joining it -- main() joins after the emitting steps, so the
    DB write overlaps the render work instead of blocking ahead of it. The old
    standalone entry was ``"async": true`` (Claude Code never waited on it);
    joining last keeps the observable wait at ~zero in the common case, with
    the same bounded worst case as the tracker's own main(). A payload without
    a session_id is skipped, the same guard the tracker's main() applies.
    """
    from jacked.data.hooks import session_account_tracker as tracker

    session_id = data.get("session_id", "")
    if not session_id:
        return None

    t = threading.Thread(
        target=tracker._handle_event,
        args=("SessionStart", session_id, data.get("cwd")),
        daemon=True,
    )
    t.start()
    return t


def _emit_chain_of_command(data: dict) -> None:
    """Step 2: print the chain-of-command dispatch policy (or nothing)."""
    from jacked.data.hooks import chain_of_command_context

    block = chain_of_command_context.render()
    if block:
        print(block)


def _emit_memory_recall(data: dict) -> None:
    """Step 3: print the group-scoped memory-vault brief (or nothing)."""
    from jacked.data.hooks import memory_recall

    brief = memory_recall.render(data)
    if brief:
        print(brief)


def _emit_issue_queue(data: dict) -> None:
    """Step 4: print the open GitHub issues of the session repo (or nothing).

    The block comes from the cache that the jacked service refreshes every 10
    minutes, so this step starts no subprocess (see
    ``jacked.data.hooks.issue_queue``). The cwd rule is the recall rule. A
    subagent session gets nothing, the same as the recall brief.
    """
    if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get("CLAUDE_CODE_AGENT_TYPE"):
        return
    from jacked.data.hooks import git_reality, issue_queue

    block = issue_queue.emit_issue_queue(git_reality.payload_cwd(data))
    if block:
        print(block)


def _emit_git_reality(data: dict) -> None:
    """Step 5: print the git reality block for the session cwd (or nothing).

    The cwd is the payload's ``cwd``, else the process cwd (the recall rule).
    Each git call is time-bounded, and the block itself never raises (see
    ``jacked.data.hooks.git_reality``).
    """
    from jacked.data.hooks import git_reality

    block = git_reality.emit_git_reality(git_reality.payload_cwd(data))
    if block:
        print(block)


def _emit_load_budget(data: dict) -> None:
    """Step 6: warn about each file Claude Code loads at session start
    that is over 90% of its load cap (or print nothing).

    The cwd is the payload's ``cwd``, else the process cwd (the recall rule).
    A subagent session gets nothing, the same as the recall brief. The step
    only measures; it never edits a file (see ``jacked.memory.load_budget``).
    """
    if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get("CLAUDE_CODE_AGENT_TYPE"):
        return
    from jacked.memory import load_budget, vault

    cwd = str(data.get("cwd") or os.getcwd())
    block = load_budget.render_block(load_budget.warnings(cwd, vault.jacked_home()))
    if block:
        print(block)


def _emit_integrity(data: dict) -> None:
    """Step 7 (LAST): warn about changes that jacked did not make (or print nothing).

    Moves suspected injected lines out of the session's MEMORY.md into a dated
    quarantine file beside it, then compares settings.json and the hook
    scripts with the baseline. It never blocks and never deletes. A subagent
    session gets nothing, the same as the recall brief.
    """
    if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get("CLAUDE_CODE_AGENT_TYPE"):
        return
    from jacked import integrity
    from jacked.memory import vault

    cwd = str(data.get("cwd") or os.getcwd())
    block = integrity.session_start_block(cwd, vault.jacked_home())
    if block:
        print(block)


# The emitting steps, in the order their output must appear. Order is the
# whole point of this module. The (silent) tracker step is not listed: main()
# starts it first and joins it last, so its DB write overlaps these renders.
STEPS = (
    ("chain of command", _emit_chain_of_command),
    ("memory recall", _emit_memory_recall),
    ("open work", _emit_issue_queue),
    ("git reality", _emit_git_reality),
    ("load budget", _emit_load_budget),
    ("integrity", _emit_integrity),
)


def _guarded(label, fn, *args):
    """Run one step; a failure is logged and NEVER stops the steps after it."""
    try:
        return fn(*args)
    except BaseException:  # noqa: BLE001 -- one bad step must not stop the rest
        logger.debug("session_start: %s step failed", label, exc_info=True)
        return None


def main():
    """Run every jacked SessionStart step in order, from ONE stdin read.

    Silent on empty/invalid stdin. Every step is individually guarded, so a
    failing step is logged at debug level and the remaining steps still run.
    The tracker thread starts first and is joined last (bounded), so the
    session start only ever waits on it when the DB write outlives the renders.

    >>> callable(main)
    True
    """
    data = _read_hook_input()
    if data is None:
        return

    tracker_thread = _guarded("session tracker", _start_session_tracker, data)
    for label, step in STEPS:
        _guarded(label, step, data)
    if tracker_thread is not None:
        _guarded("session tracker join", tracker_thread.join, TRACKER_JOIN_TIMEOUT)


if __name__ == "__main__":
    main()
