#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs ruff linter before allowing push.
# Installed by: jacked lint-hook init

if ! command -v ruff >/dev/null 2>&1; then
    echo "[jacked] ruff not found — skipping lint check."
    echo "[jacked] Install ruff: pip install ruff"
    exit 0
fi

echo "[jacked] Running ruff check..."
ruff check .
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Lint errors found. Fix them before pushing."
    echo "[jacked] Auto-fix: ruff check --fix ."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
