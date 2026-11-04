#!/bin/bash
# The pre-release upgrade matrix (macOS): both real upgrade paths from a
# previous PyPI release to the new wheel, with a restore between them.
#
#   matrix.sh <previous_version> <new_version> <dist_dir>
#
# dist_dir holds the new wheel (uv build --wheel -o <dist_dir>). Each path
# starts from <previous_version> installed, registered and running:
#   uv tool install "claude-jacked[tray]==<previous>" --force --refresh
#   jacked install --force; jacked service restart
# then runs scripts/upgrade_gate/upgrade_gate.sh for that path. Exit code is
# the number of paths that failed. Run it for every previous version you
# still expect in the field (at least the one before the new release).
set -u
PREV="${1:-}"; NEW="${2:-}"; DIST="${3:-}"
if [ -z "$PREV" ] || [ -z "$NEW" ] || [ -z "$DIST" ]; then
    echo "usage: $0 <previous_version> <new_version> <dist_dir>" >&2; exit 64
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${TMPDIR:-/tmp}/jacked-upgrade-gate"; mkdir -p "$LOG_DIR"
restore() {
    (cd / && uv tool install "claude-jacked[tray]==$PREV" --force --refresh > "$LOG_DIR/restore-$PREV.log" 2>&1); rc=$?
    echo "restore $PREV: uv tool install rc=$rc"
    [ "$rc" = 0 ] || return 1
    jacked install --force > "$LOG_DIR/restore-$PREV-install.log" 2>&1 || { echo "restore $PREV: jacked install failed"; return 1; }
    jacked service restart > "$LOG_DIR/restore-$PREV-restart.log" 2>&1 || { echo "restore $PREV: jacked service restart failed"; return 1; }
    sleep 4
}
failed=0
for path in tray cli; do
    echo "##### $PREV -> $NEW, $path path #####"
    if ! restore; then echo "RESULT: restore failed, $path path not run"; failed=$((failed+1)); continue; fi
    bash "$HERE/upgrade_gate.sh" "$path" "$NEW" "$DIST" > "$LOG_DIR/gate-$path-from-$PREV.log" 2>&1; rc=$?
    grep -E "^=== before|EXIT:|^PASS|^FAIL|^RESULT" "$LOG_DIR/gate-$path-from-$PREV.log"
    [ "$rc" = 0 ] || failed=$((failed+1))
done
echo "MATRIX RESULT: $([ $failed = 0 ] && echo "ALL PASS" || echo "$failed path(s) FAIL") (logs in $LOG_DIR)"
exit $failed
