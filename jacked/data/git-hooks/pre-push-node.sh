#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs eslint before allowing push.
# Installed by: jacked lint-hook init

if ! command -v npx >/dev/null 2>&1; then
    echo "[jacked] npx not found — skipping lint check."
    exit 0
fi

if [ ! -f node_modules/.bin/eslint ] && ! ls .eslintrc* eslint.config.* >/dev/null 2>&1; then
    echo "[jacked] eslint not configured — skipping lint check."
    exit 0
fi

echo "[jacked] Running eslint..."
npx eslint .
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Lint errors found. Fix them before pushing."
    echo "[jacked] Auto-fix: npx eslint --fix ."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
