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

Each step is guarded on its own: a failure in one NEVER stops the ones after
it, and nothing here may crash a session start (every path exits 0). Steps 2
and 3 no-op quietly when their feature is off (the chain-of-command skill file
is absent / the memory vault is disabled), so this single entry stays correct
whatever the user has toggled.
"""

import json
import logging
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


# The emitting steps, in the order their output must appear. Order is the
# whole point of this module. The (silent) tracker step is not listed: main()
# starts it first and joins it last, so its DB write overlaps these renders.
STEPS = (
    ("chain of command", _emit_chain_of_command),
    ("memory recall", _emit_memory_recall),
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
