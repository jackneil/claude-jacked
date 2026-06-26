---
description: Use when debugging in a focused area or working on sensitive code to prevent accidental edits outside the target directory.
---

You are executing the `/freeze` command to restrict Claude's file editing to one or more directory boundaries. This prevents scope creep during focused work — especially valuable when debugging, working in sensitive areas (auth, billing, multi-tenancy), or doing a targeted fix.

The boundary supports **multiple included paths**, optional **sub-path excludes**, and is **scoped to the current project** so a freeze in one repo never governs a repo opened in another terminal.

## Steps

1. **Parse the arguments**

   `$ARGUMENTS` may contain one or more paths to freeze edits to, plus an optional `except` clause listing sub-paths to exclude. Syntax:

   ```
   /freeze <path> [<path> ...] [except <subpath> ...]
   ```

   - Everything before the `except` keyword is an **included** path.
   - Everything after `except` is an **excluded** sub-path (a file is editable only if it's under an included path AND not under an excluded one).
   - If `$ARGUMENTS` is empty, use the current working directory as the single included path and confirm: "Freezing edits to the current directory: `$CWD`. Run `/freeze <path> [<path> ...] [except <subpath>]` to specify a different boundary."

   Resolve every path to an absolute path. Verify each **included** path exists:
   ```bash
   realpath "<path>" 2>/dev/null
   ```
   If an included path doesn't exist, tell the user and stop. (Excluded sub-paths need not exist yet.)

2. **Determine the project root**

   Resolve the project root the gatekeeper keys on:
   ```bash
   realpath "${CLAUDE_PROJECT_DIR:-$CWD}" 2>/dev/null
   ```
   This becomes the freeze entry's `project` so the boundary applies only to this repo.

3. **Verify the gatekeeper hook is actually installed**

   The freeze is enforced by the jacked security gatekeeper (a `PreToolUse` hook). If that hook isn't wired into settings, writing a freeze file does nothing — a silent no-op. Confirm it's registered:
   ```bash
   grep -l "security_gatekeeper" ~/.claude/settings.json 2>/dev/null
   ```
   If there is **no** match (the hook is not installed), **warn the user prominently** that the freeze will NOT be enforced until jacked hooks are installed, and tell them to run the jacked installer (e.g. `jacked install`). Still write the freeze file so it takes effect once hooks are wired, but make the warning unmissable.

4. **Read any existing freeze and merge (do not stomp)**

   ```bash
   cat ~/.claude/jacked-freeze.json 2>/dev/null
   cat ~/.claude/jacked-freeze-dir.txt 2>/dev/null   # legacy single-path format
   ```

   The boundary lives in `~/.claude/jacked-freeze.json` with this shape:
   ```json
   {
     "freezes": [
       {
         "project": "/abs/project/root",
         "include": ["/abs/included/path"],
         "exclude": ["/abs/excluded/subpath"],
         "since": "2026-06-26T14:30:00Z"
       }
     ]
   }
   ```

   - If an entry for **this project root** already exists, **append** the new included/excluded paths to it (deduplicate) — do NOT replace the existing scope. Preserve its original `since`.
   - If no entry exists for this project, create a new one with `since` set to the current UTC timestamp (ISO 8601, e.g. `date -u +%Y-%m-%dT%H:%M:%SZ`).
   - Leave freeze entries for **other** projects untouched.
   - If a legacy `~/.claude/jacked-freeze-dir.txt` exists, fold its path into a freeze entry, then delete the legacy file (the gatekeeper prefers the JSON format; removing the stale text file avoids confusion).

5. **Write the freeze file**

   Use the Write tool to write the merged JSON to `~/.claude/jacked-freeze.json`. Keep it valid JSON — the gatekeeper **fails closed** on a corrupt/unreadable freeze file (it blocks all edits and tells the user to run `/unfreeze`), so a malformed write would block editing entirely rather than silently disabling the guard.

6. **Confirm**

   Report to the user (fill in the real values):

   ```
   Freeze active — edits restricted to:
   - <included path 1>
   - <included path 2>
   Excluded sub-paths (edits still blocked here):
   - <excluded subpath>     # omit this block if there are no excludes
   Project: <project root>   |   Active since: <since timestamp>

   What's blocked:
   - Edit, Write, and NotebookEdit operations on files outside the included paths
     (or inside an excluded sub-path) — for THIS project only
   - The security gatekeeper enforces this on every tool call (<1ms overhead)
   - If the freeze file ever becomes corrupt, the gatekeeper FAILS CLOSED — it
     blocks all edits and tells you to run /unfreeze (a deliberate guard never
     silently evaporates)

   What's NOT blocked:
   - Read, Grep, Glob, Bash, and all other tools work normally everywhere
   - Edits to files inside an included path (and not inside an excluded one)
   - File writes made via Bash subprocesses (e.g. `sed -i`, shell redirects like
     `echo > ../x`, or `python`/`node` scripts that open files for writing) are
     NOT caught by this boundary — it only gates the Edit/Write/NotebookEdit tools.
     For true isolation against subprocess writes, enable the OS sandbox or add a
     Bash deny rule; open the workspace at the frozen path so `../` can't reach siblings.

   Run /unfreeze to remove the restriction.
   ```
