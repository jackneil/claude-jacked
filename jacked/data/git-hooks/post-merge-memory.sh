#!/bin/sh
# jacked-memory-hook
# Post-merge hook: distills a landed merge into a candidate memory note.
# Installed by: jacked memory
# Cross-platform: Linux, macOS, Windows (git bash)

# The absolute jacked path is substituted in at install time (@JACKED_BIN@ ->
# the resolved binary, or "" when it could not be resolved). Prefer it so the
# hook keeps working even when PATH is stripped (git GUIs, IDEs).
JACKED_BIN="@JACKED_BIN@"

# Background the capture so the merge is never blocked, then always succeed.
# The capture itself is fail-open (jacked memory capture-merge always exits 0).
if [ -x "$JACKED_BIN" ]; then
    "$JACKED_BIN" memory capture-merge --repo "$PWD" >/dev/null 2>&1 &
elif command -v jacked >/dev/null 2>&1; then
    jacked memory capture-merge --repo "$PWD" >/dev/null 2>&1 &
else
    # jacked is not on PATH and the pinned path is gone (upgrade/uninstall): leave
    # a single breadcrumb so a silently-skipped capture is discoverable.
    printf '%s jacked not found; merge capture skipped\n' "$(date)" >> "$HOME/.claude/jacked-memory.log" 2>/dev/null
fi
exit 0
