#!/usr/bin/env python3
"""Memory-vault capture dispatch shim (auto-registered by filename).

Routes the hook event to the capture engine:
  SessionEnd -> jacked.memory.capture.session_end
  PreCompact -> jacked.memory.capture.pre_compact
  (anything else -> no-op)

Registered async in settings.json, so Claude Code is not blocked; the work runs
in-process (no daemon thread needed). Produces NO stdout. Fails open: any error
returns quietly with exit 0 semantics -- capture must never block a session from
ending or compacting.
"""
import json
import sys


def main(argv=None):
    """Read the hook payload from stdin and dispatch. Never raises."""
    try:
        from jacked.memory import vault as _vault
        _vault.ensure_memory_file_logging()

        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        event = payload.get("hook_event_name", "")
        if event not in ("SessionEnd", "PreCompact"):
            return
        from jacked.memory import capture
        if event == "SessionEnd":
            capture.session_end(payload)
        else:
            capture.pre_compact(payload)
    except BaseException:  # noqa: BLE001 -- a hook must never crash the session
        return


if __name__ == "__main__":
    main()
