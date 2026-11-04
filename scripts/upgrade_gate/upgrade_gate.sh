#!/bin/bash
# Mixed-generation upgrade gate (macOS, launchd supervisor).
#
# Runs ONE real upgrade path from the jacked that is installed now to the
# wheel in a find-links directory (or to PyPI when no directory is given),
# then checks the live service. Run it from the PREVIOUS PyPI release before
# every release, once for each path. See scripts/upgrade_gate/matrix.sh for
# the full matrix with the restores between the paths.
#
#   upgrade_gate.sh cli  <expected_version> [find_links_dir]
#   upgrade_gate.sh tray <expected_version> [find_links_dir]
#
# cli:  the real `jacked upgrade`.
# tray: the real tray updater, `<tool-env python> -I -m jacked.service.updater
#       <tray pid> tray --target-version <v> --port 8321`. The tray spawns this
#       and exits; a short `sleep` stands in for the tray pid here.
#
# Checks (PASS/FAIL per line, then a RESULT line, exit code = number of FAILs):
#   the updater exits 0; the tool environment imports the expected version;
#   /api/version reports it; the launchd job runs; exactly one listener on
#   the port and it is the launchd pid; the same pid after 60 s; /api/health
#   is ok; no recovery file (~/.claude/jacked-update-failed.txt).
#
# The script never prints a token value or an account email.
set -u
MODE="${1:-}"; EXPECT="${2:-}"; LINKS="${3:-}"
if [ -z "$MODE" ] || [ -z "$EXPECT" ]; then
    echo "usage: $0 cli|tray <expected_version> [find_links_dir]" >&2; exit 64
fi
case "$MODE" in cli|tray) ;; *) echo "mode must be cli or tray" >&2; exit 64 ;; esac
UID_=$(id -u); LABEL=ai.hank.jacked; PORT="${JACKED_PORT:-8321}"
# The interpreter the real tray uses: the `jacked` entry point's shebang
# (bin/python3 in a uv tool env). The service contract records the runtime
# path. Before #293i each venv alias recorded its own name, so bin/python
# here computed a different generation than the launchd artifact and every
# native restart refused with owned_drift. Builds with the #293i fix map
# every alias to bin/python3, but this gate also drives the PREVIOUS release
# (the mixed-generation upgrade path), so keep reading the shebang: it is
# the exact interpreter the tray runs, in every build.
JACKED_BIN="$(command -v jacked || echo "$HOME/.local/bin/jacked")"
PY="$(head -1 "$(readlink -f "$JACKED_BIN" 2>/dev/null || echo "$JACKED_BIN")" | sed 's/^#!//')"
[ -x "$PY" ] || PY="$HOME/.local/share/uv/tools/claude-jacked/bin/python3"
RECOVERY="$HOME/.claude/jacked-update-failed.txt"
LOG_DIR="${TMPDIR:-/tmp}/jacked-upgrade-gate"; mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/$MODE-run.log"
fails=0
check() { if [ "$1" = 0 ]; then echo "PASS: $2"; else echo "FAIL: $2"; fails=$((fails+1)); fi; }
lpid() { launchctl print "gui/$UID_/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'; }
lstate() { launchctl print "gui/$UID_/$LABEL" 2>/dev/null | awk '/^[[:space:]]*state = /{print $3; exit}'; }
running() { curl -s "http://127.0.0.1:$PORT/api/version" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["current"])' 2>/dev/null; }
toolver() { (cd / && "$PY" -c 'import jacked; print(jacked.__version__)' 2>/dev/null); }

echo "=== before: running $(running) | tool env $(toolver) | launchd pid $(lpid) state $(lstate)"
rm -f "$RECOVERY"
t0=$(date +%s)
if [ "$MODE" = cli ]; then
    echo "=== jacked upgrade $( [ -n "$LINKS" ] && echo "(UV_FIND_LINKS=$LINKS)" || echo "(PyPI)" ) ==="
    if [ -n "$LINKS" ]; then UV_FIND_LINKS="$LINKS" jacked upgrade > "$RUN_LOG" 2>&1; rc=$?; else jacked upgrade > "$RUN_LOG" 2>&1; rc=$?; fi
else
    sleep 3 & TRAY=$!
    echo "=== tray updater: $PY -I -m jacked.service.updater $TRAY tray --target-version $EXPECT --port $PORT $( [ -n "$LINKS" ] && echo "(UV_FIND_LINKS=$LINKS)" || echo "(PyPI)" ) ==="
    if [ -n "$LINKS" ]; then UV_FIND_LINKS="$LINKS" "$PY" -I -m jacked.service.updater "$TRAY" tray --target-version "$EXPECT" --port "$PORT" > "$RUN_LOG" 2>&1; rc=$?; else "$PY" -I -m jacked.service.updater "$TRAY" tray --target-version "$EXPECT" --port "$PORT" > "$RUN_LOG" 2>&1; rc=$?; fi
fi
echo "$MODE EXIT:$rc in $(( $(date +%s) - t0 ))s (log: $RUN_LOG)"
grep -E "Target version|claude-jacked==|OK\]|handoff|ready|Error|error|Traceback|ImportError|REFUSED|Recovery" "$RUN_LOG" | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:,.]+//' | cut -c1-200 | head -20
check $rc "$MODE upgrade exits 0"
[ ! -f "$RECOVERY" ]; check $? "no recovery file written"

echo "=== after ==="
ver=$(toolver); [ "$ver" = "$EXPECT" ]; check $? "tool env imports jacked $EXPECT (got ${ver:-none})"
cur=""
for i in $(seq 1 60); do cur=$(running); [ "$cur" = "$EXPECT" ] && break; sleep 2; done
[ "$cur" = "$EXPECT" ]; check $? "/api/version reports current $EXPECT (got ${cur:-none} after $((i*2))s)"
pid=$(lpid); state=$(lstate)
[ -n "$pid" ] && [ "$state" = "running" ]; check $? "launchd job $LABEL is running under launchd (pid=${pid:-none} state=${state:-none})"
owners=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk '{print $2}' | sort -u | tr '\n' ' ')
[ "$(echo "$owners" | wc -w | tr -d ' ')" = "1" ]; check $? "exactly one listener on :$PORT (pids: $owners)"
echo "$owners" | grep -qw "${pid:-NOPID}"; check $? "the listener is the launchd-owned pid"
echo "--- crash-loop watch: 60s ---"; p0=$pid; sleep 60; p1=$(lpid)
[ -n "$p1" ] && [ "$p0" = "$p1" ]; check $? "same launchd pid after 60s ($p0 -> ${p1:-none}), no restart"
h=$(curl -s "http://127.0.0.1:$PORT/api/health"); echo "$h" | grep -q '"status":"ok"'; check $? "health ok ($h)"
jacked service status 2>&1 | sed -n '1,3p'
echo "RESULT: $([ $fails = 0 ] && echo ALL PASS || echo "$fails FAIL")"
exit $fails
