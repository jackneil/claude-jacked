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


def render(data: dict | None = None) -> str:
    """Build the recall brief for an already-parsed SessionStart payload.

    ``data`` supplies ``cwd``; anything falsy falls back to ``os.getcwd()``.
    Returns "" for a subagent session, a disabled/empty vault, or ANY failure
    (fail open). Split out of main() so the combined SessionStart hook
    (``jacked.data.hooks.session_start``) can run this step in-process, in a
    fixed order, without a second stdin read.
    """
    try:
        from jacked.memory import vault as _vault
        _vault.ensure_memory_file_logging()

        # A subagent session must not spend its context on the recall brief.
        if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get(
            "CLAUDE_CODE_AGENT_TYPE"
        ):
            return ""

        cwd = os.getcwd()
        if isinstance(data, dict) and data.get("cwd"):
            cwd = str(data["cwd"])

        from jacked.memory.recall import build_brief

        return build_brief(cwd)
    except BaseException:  # noqa: BLE001 -- a hook must never crash the session
        return ""


def main(argv=None):
    """Read the SessionStart payload from stdin and inject the brief. Never raises."""
    # Drain stdin so the hook never blocks; keep the raw for cwd parsing.
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""

    data = None
    if raw and raw.strip():
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                data = payload
        except Exception:
            pass

    try:
        brief = render(data)
        if brief:
            print(brief)
    except BaseException:  # noqa: BLE001 -- a hook must never crash the session
        return


if __name__ == "__main__":
    main()
