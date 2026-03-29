---
description: Use when debugging in a focused area or working on sensitive code to prevent accidental edits outside the target directory.
---

You are executing the `/freeze` command to restrict Claude's file editing to a specific directory boundary. This prevents scope creep during focused work — especially valuable when debugging, working in sensitive areas (auth, billing, multi-tenancy), or doing a targeted fix.

## Steps

1. **Parse the path argument**

   `$ARGUMENTS` contains the path to freeze edits to. If empty, use the current working directory and confirm with the user: "Freezing edits to the current directory: `$CWD`. Run `/freeze <path>` to specify a different boundary."

   Resolve to an absolute path and verify it exists:
   ```bash
   realpath "$ARGUMENTS" 2>/dev/null
   ```

   If the path doesn't exist, tell the user and stop.

2. **Check for existing freeze**

   ```bash
   cat ~/.claude/jacked-freeze-dir.txt 2>/dev/null
   ```

   If a freeze is already active, tell the user: "Freeze already active at `$EXISTING_PATH`. Replacing with `$NEW_PATH`."

3. **Write the freeze file**

   Use the Write tool to write the resolved absolute path to `~/.claude/jacked-freeze-dir.txt`. The file should contain only the absolute path, nothing else.

4. **Confirm**

   Report to the user:

   ```
   Freeze active — edits restricted to: $RESOLVED_PATH

   What's blocked:
   - Edit, Write, and NotebookEdit operations on files outside this directory
   - The security gatekeeper enforces this on every tool call (<1ms overhead)

   What's NOT blocked:
   - Read, Grep, Glob, Bash, and all other tools work normally everywhere
   - Edits to files inside the frozen directory work normally

   Run /unfreeze to remove the restriction.
   ```
