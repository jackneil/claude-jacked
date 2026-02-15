#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs ruff linter before allowing push.
# Installed by: jacked lint-hook init
# Cross-platform: Linux, macOS, Windows (git bash)

find_ruff() {
    # 0. Project env configured by jacked
    if [ -f ".git/jacked/env" ]; then
        ENV_ROOT=$(head -1 .git/jacked/env | tr -d '\r\n' | sed 's/[[:space:]]*$//')
        case "$ENV_ROOT" in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) ENV_ROOT="" ;; esac
        case "$ENV_ROOT" in *..*) ENV_ROOT="" ;; esac
        if [ -n "$ENV_ROOT" ]; then
            for candidate in "$ENV_ROOT/bin/ruff" "$ENV_ROOT/Scripts/ruff.exe"; do
                if [ -f "$candidate" ]; then echo "$candidate"; return; fi
            done
        fi
    fi

    # 1. On PATH
    command -v ruff >/dev/null 2>&1 && { command -v ruff; return; }

    # 2. Windows: where.exe searches full system PATH
    if command -v where.exe >/dev/null 2>&1; then
        FOUND=$(where.exe ruff 2>/dev/null | head -1 | tr '\\' '/')
        if [ -n "$FOUND" ]; then echo "$FOUND"; return; fi
    fi

    HOME_DIR="${HOME:-$USERPROFILE}"

    # 3. Active conda env
    if [ -n "$CONDA_PREFIX" ]; then
        for candidate in "$CONDA_PREFIX/bin/ruff" "$CONDA_PREFIX/Scripts/ruff.exe"; do
            if [ -f "$candidate" ]; then echo "$candidate"; return; fi
        done
    fi

    # 4. All conda envs — direct path checks, no find traversal
    if [ -d "$HOME_DIR/.conda/envs" ]; then
        for env_dir in "$HOME_DIR/.conda/envs"/*/; do
            for candidate in "${env_dir}bin/ruff" "${env_dir}Scripts/ruff.exe"; do
                if [ -f "$candidate" ]; then echo "$candidate"; return; fi
            done
        done
    fi

    # 5. uv / pipx / pip --user installs
    for candidate in "$HOME_DIR/.local/bin/ruff" "$HOME_DIR/AppData/Roaming/Python/Scripts/ruff.exe"; do
        if [ -f "$candidate" ]; then echo "$candidate"; return; fi
    done

    return 1
}

RUFF=$(find_ruff)
if [ -z "$RUFF" ]; then
    echo "[jacked] ruff not found — skipping lint check."
    echo "[jacked] Install ruff: uv tool install ruff"
    exit 0
fi

# Validate binary is actually ruff
VERSION=$("$RUFF" --version 2>&1 | head -1)
case "$VERSION" in
    *ruff*|*Ruff*) ;;
    *) echo "[jacked] WARNING: $RUFF doesn't look like ruff ($VERSION) — skipping."
       exit 0 ;;
esac

echo "[jacked] Running ruff check... ($RUFF, $VERSION)"
"$RUFF" check .
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] Lint errors found. Fix them before pushing."
    echo "[jacked] Auto-fix: ruff check --fix ."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
