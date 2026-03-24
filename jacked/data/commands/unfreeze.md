---
description: "Remove the edit restriction set by /freeze — allows edits anywhere again"
---

You are executing the `/unfreeze` command to remove the directory edit restriction.

## Steps

1. **Check if freeze is active**

   ```bash
   cat ~/.claude/jacked-freeze-dir.txt 2>/dev/null
   ```

   If the file doesn't exist or is empty, tell the user: "No freeze is currently active." and stop.

2. **Record the previous path** for the confirmation message.

3. **Remove the freeze file**

   ```bash
   rm ~/.claude/jacked-freeze-dir.txt
   ```

4. **Confirm**

   ```
   Freeze removed — edits are no longer restricted.
   Previously frozen to: $PREVIOUS_PATH
   ```
