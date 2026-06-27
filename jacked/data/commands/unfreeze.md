---
description: Use when done working in a frozen directory and ready to allow edits anywhere again. Removes the restriction set by /freeze. Also the reset path when the gatekeeper reports the freeze file is corrupt.
---

You are executing the `/unfreeze` command to remove the directory edit restriction set by `/freeze`. This is also the documented reset path when the gatekeeper **fails closed** because the freeze file became corrupt or unreadable.

## Steps

1. **Check if a freeze is active**

   ```bash
   cat ~/.claude/jacked-freeze.json 2>/dev/null
   cat ~/.claude/jacked-freeze-dir.txt 2>/dev/null   # legacy single-path format
   ```

   If neither file exists (or both are empty), tell the user: "No freeze is currently active." and stop.

   If `~/.claude/jacked-freeze.json` exists but is corrupt/unparseable, say so — this is exactly the state where the gatekeeper is failing closed and blocking all edits — and proceed to reset it in the steps below.

2. **Record the previous boundary and remove the freeze — in one pass.**

   Capture the boundary you're about to clear in the *same* read that removes it, so there's no read-then-remove gap and the no-op vs. success branches stay deterministic. Resolve the current project root first:
   ```bash
   ROOT="$(realpath "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null)"
   ```

   - **Per-project removal (default):** read `~/.claude/jacked-freeze.json` once. In that single pass, capture the boundary of the entry whose `project` matches `$ROOT` as `$PREVIOUS_BOUNDARY`, drop that entry, and write the remaining entries back with the Write tool — so a freeze you set in another repo stays put. If no entries remain, delete the file:
     ```bash
     rm ~/.claude/jacked-freeze.json
     ```
   - **Full reset** (`/unfreeze all`, or a corrupt/unparseable file — exactly the state where the gatekeeper is failing closed): capture whatever boundary text you can read for the message, then delete it outright:
     ```bash
     rm ~/.claude/jacked-freeze.json
     ```
   - Always also remove the legacy single-path file if present:
     ```bash
     rm ~/.claude/jacked-freeze-dir.txt 2>/dev/null
     ```

3. **Confirm**

   ```
   Freeze removed — edits are no longer restricted for this project.
   Previously frozen to: $PREVIOUS_BOUNDARY
   ```

   - If other projects still have active freezes, mention that they remain in effect.
   - **Re-freeze is one command away:** the gatekeeper hook stays installed for the session — clearing the state file just makes it a no-op, it doesn't tear the hook down. Tell the user they can run `/freeze <path>` again anytime to re-restrict, no session restart needed.
   - **Heads-up on global state:** the freeze state lives in `~/.claude/jacked-freeze.json`, which is shared machine-wide, not per-terminal. Clearing this project's entry lifts the boundary for *every* concurrent Claude Code session pointed at the same project — not just this one. Surface this if other sessions might be relying on the freeze.
