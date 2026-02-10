#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs cargo clippy before allowing push.
# Installed by: jacked lint-hook init

if ! command -v cargo >/dev/null 2>&1; then
    echo "[jacked] cargo not found — skipping lint check."
    exit 0
fi

echo "[jacked] Running cargo clippy..."
cargo clippy --all-targets --all-features -- -D warnings
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Clippy warnings found. Fix them before pushing."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
