#!/bin/sh
# jacked-lint-hook
# Pre-push hook: runs go vet before allowing push.
# Installed by: jacked lint-hook init
# Cross-platform: Linux, macOS, Windows (git bash)

find_go() {
    # 0. Project env configured by jacked
    if [ -f ".git/jacked/env" ]; then
        ENV_ROOT=$(head -1 .git/jacked/env | tr -d '\r\n' | sed 's/[[:space:]]*$//')
        case "$ENV_ROOT" in /*|[A-Za-z]:/*|[A-Za-z]:\\*) ;; *) ENV_ROOT="" ;; esac
        case "$ENV_ROOT" in *..*) ENV_ROOT="" ;; esac
        if [ -n "$ENV_ROOT" ]; then
            for candidate in "$ENV_ROOT/bin/go" "$ENV_ROOT/go.exe"; do
                if [ -f "$candidate" ]; then echo "$candidate"; return; fi
            done
        fi
    fi

    # 1. On PATH
    command -v go >/dev/null 2>&1 && { command -v go; return; }

    # 2. Windows: where.exe searches full system PATH
    if command -v where.exe >/dev/null 2>&1; then
        FOUND=$(where.exe go 2>/dev/null | head -1 | tr '\\' '/')
        if [ -n "$FOUND" ]; then echo "$FOUND"; return; fi
    fi

    # 3. GOROOT / GOPATH
    if [ -n "$GOROOT" ] && [ -f "$GOROOT/bin/go" ]; then echo "$GOROOT/bin/go"; return; fi
    if [ -n "$GOPATH" ] && [ -f "$GOPATH/bin/go" ]; then echo "$GOPATH/bin/go"; return; fi

    # 4. Common install locations
    for candidate in /usr/local/go/bin/go "$HOME/go/bin/go" "C:/Program Files/Go/bin/go.exe"; do
        if [ -f "$candidate" ]; then echo "$candidate"; return; fi
    done

    return 1
}

GO=$(find_go)
if [ -z "$GO" ]; then
    echo "[jacked] go not found — skipping lint check."
    exit 0
fi

# Validate binary is actually go
VERSION=$("$GO" version 2>&1 | head -1)
case "$VERSION" in
    *go*) ;;
    *) echo "[jacked] WARNING: $GO doesn't look like go ($VERSION) — skipping."
       exit 0 ;;
esac

echo "[jacked] Running go vet... ($GO, $VERSION)"
"$GO" vet ./...
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "[jacked] go vet found issues. Fix them before pushing."
    exit 1
fi

echo "[jacked] Lint check passed."
exit 0
