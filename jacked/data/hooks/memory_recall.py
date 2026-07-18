#!/usr/bin/env python3
"""Memory-vault recall dispatch shim (auto-registered by filename).

SYNCHRONOUS SessionStart hook: a synchronous SessionStart hook's stdout is
injected into the session context, so this prints the group-scoped memory brief
for the repo the session opened in (``jacked.memory.recall.build_brief``).

Contract:
  - Drain stdin first so the hook never blocks (payload is parsed only for cwd,
    falling back to os.getcwd()).
  - Print the brief to stdout when non-empty; print NOTHING when empty.
  - A subagent session (CLAUDE_CODE_PARENT_SESSION_ID / CLAUDE_CODE_AGENT_TYPE)
    prints nothing -- a subagent must not burn its context window on the brief.
  - Fail open: any error returns quietly (exit 0). Recall must NEVER break a
    session from starting.
"""
import json
import os
import sys


def main(argv=None):
    """Read the SessionStart payload from stdin and inject the brief. Never raises."""
    try:
        from jacked.memory import vault as _vault
        _vault.ensure_memory_file_logging()

        # Drain stdin so the hook never blocks; keep the raw for cwd parsing.
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""

        # A subagent session must not spend its context on the recall brief.
        if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get(
            "CLAUDE_CODE_AGENT_TYPE"
        ):
            return

        cwd = os.getcwd()
        if raw and raw.strip():
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("cwd"):
                    cwd = str(payload["cwd"])
            except Exception:
                pass

        from jacked.memory.recall import build_brief

        brief = build_brief(cwd)
        if brief:
            print(brief)
    except BaseException:  # noqa: BLE001 -- a hook must never crash the session
        return


if __name__ == "__main__":
    main()
