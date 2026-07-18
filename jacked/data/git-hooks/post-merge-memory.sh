#!/bin/sh
# jacked-memory-hook
# Post-merge hook: distills a landed merge into a candidate memory note.
# Installed by: jacked memory
# Cross-platform: Linux, macOS, Windows (git bash)

# If jacked is not installed, do nothing and never block the merge.
command -v jacked >/dev/null 2>&1 || exit 0

# Background the capture so the merge is never blocked, then always succeed.
# The capture itself is fail-open (jacked memory capture-merge always exits 0).
jacked memory capture-merge --repo "$PWD" >/dev/null 2>&1 &
exit 0
