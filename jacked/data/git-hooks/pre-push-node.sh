#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs eslint before allowing push.
# Installed by: jacked lint-hook init
# Cross-platform: Linux, macOS, Windows (git bash)

find_npx() {
    # 0. Project env configured by jacked
    if [ -f ".git/jacked/env" ]; then
        ENV_ROOT=$(head -1 .git/jacked/env | tr -d '\r\n' | sed 's/[[:space:]]*$//')
        case "$ENV_ROOT" in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) ENV_ROOT="" ;; esac
        case "$ENV_ROOT" in *..*) ENV_ROOT="" ;; esac
        if [ -n "$ENV_ROOT" ]; then
            for candidate in "$ENV_ROOT/bin/npx" "$ENV_ROOT/npx" "$ENV_ROOT/npx.cmd"; do
                if [ -f "$candidate" ]; then echo "$candidate"; return; fi
            done
        fi
    fi

    # 1. On PATH
    command -v npx >/dev/null 2>&1 && { command -v npx; return; }

    # 2. Windows: where.exe searches full system PATH
    if command -v where.exe >/dev/null 2>&1; then
        FOUND=$(where.exe npx 2>/dev/null | head -1 | tr '\\' '/')
        if [ -n "$FOUND" ]; then echo "$FOUND"; return; fi
    fi

    HOME_DIR="${HOME:-$USERPROFILE}"

    # 3. nvm installs
    NVM="${NVM_DIR:-$HOME_DIR/.nvm}"
    if [ -d "$NVM/versions/node" ]; then
        for candidate in "$NVM/versions/node"/*/bin/npx; do
            if [ -f "$candidate" ]; then echo "$candidate"; return; fi
        done
    fi

    # 4. Common locations
    for candidate in "$HOME_DIR/.local/bin/npx" "$HOME_DIR/AppData/Roaming/npm/npx" "$HOME_DIR/AppData/Roaming/npm/npx.cmd"; do
        if [ -f "$candidate" ]; then echo "$candidate"; return; fi
    done

    return 1
}

NPX=$(find_npx)
if [ -z "$NPX" ]; then
    echo "[jacked] npx not found — skipping lint check."
    exit 0
fi

# Check for eslint config (supports ESLint 8 and 9+ flat config)
HAS_CONFIG=false
if [ -f node_modules/.bin/eslint ]; then HAS_CONFIG=true; fi
for f in .eslintrc .eslintrc.* eslint.config.js eslint.config.mjs eslint.config.cjs eslint.config.ts; do
    if [ -f "$f" ]; then HAS_CONFIG=true; break; fi
done

if [ "$HAS_CONFIG" = "false" ]; then
    echo "[jacked] eslint not configured — skipping lint check."
    exit 0
fi

# Validate binary is actually npx
VERSION=$("$NPX" --version 2>&1 | head -1)
case "$VERSION" in
    [0-9]*) ;;
    *) echo "[jacked] WARNING: $NPX doesn't look like npx ($VERSION) — skipping."
       exit 0 ;;
esac

echo "[jacked] Running eslint... ($NPX, npx v$VERSION)"
"$NPX" eslint .
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Lint errors found. Fix them before pushing."
    echo "[jacked] Auto-fix: npx eslint --fix ."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
