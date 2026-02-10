#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs go vet before allowing push.
# Installed by: jacked lint-hook init

if ! command -v go >/dev/null 2>&1; then
    echo "[jacked] go not found — skipping lint check."
    exit 0
fi

echo "[jacked] Running go vet..."
go vet ./...
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] go vet found issues. Fix them before pushing."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
