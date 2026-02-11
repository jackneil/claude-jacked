#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs cargo clippy before allowing push.
# Installed by: jacked lint-hook init
# Cross-platform: Linux, macOS, Windows (git bash)

find_cargo() {
    # 0. Project env configured by jacked
    if [ -f ".git/jacked/env" ]; then
        ENV_ROOT=$(head -1 .git/jacked/env | tr -d '\r\n' | sed 's/[[:space:]]*$//')
        case "$ENV_ROOT" in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) ENV_ROOT="" ;; esac
        case "$ENV_ROOT" in *..*) ENV_ROOT="" ;; esac
        if [ -n "$ENV_ROOT" ]; then
            for candidate in "$ENV_ROOT/bin/cargo" "$ENV_ROOT/cargo.exe"; do
                if [ -f "$candidate" ]; then echo "$candidate"; return; fi
            done
        fi
    fi

    # 1. On PATH
    command -v cargo >/dev/null 2>&1 && { command -v cargo; return; }

    # 2. Windows: where.exe searches full system PATH
    if command -v where.exe >/dev/null 2>&1; then
        FOUND=$(where.exe cargo 2>/dev/null | head -1 | tr '\\' '/')
        if [ -n "$FOUND" ]; then echo "$FOUND"; return; fi
    fi

    # 3. CARGO_HOME (default ~/.cargo)
    HOME_DIR="${HOME:-$USERPROFILE}"
    CARGO="${CARGO_HOME:-$HOME_DIR/.cargo}"
    for candidate in "$CARGO/bin/cargo" "$CARGO/bin/cargo.exe"; do
        if [ -f "$candidate" ]; then echo "$candidate"; return; fi
    done

    return 1
}

CARGO=$(find_cargo)
if [ -z "$CARGO" ]; then
    echo "[jacked] cargo not found — skipping lint check."
    exit 0
fi

# Validate binary is actually cargo
VERSION=$("$CARGO" --version 2>&1 | head -1)
case "$VERSION" in
    *cargo*|*Cargo*) ;;
    *) echo "[jacked] WARNING: $CARGO doesn't look like cargo ($VERSION) — skipping."
       exit 0 ;;
esac

echo "[jacked] Running cargo clippy... ($CARGO, $VERSION)"
"$CARGO" clippy --all-targets --all-features -- -D warnings
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Clippy warnings found. Fix them before pushing."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
