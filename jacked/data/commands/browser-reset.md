---
description: Use when browser MCP tools are failing, stuck, or unresponsive. Diagnoses connection issues, kills stale processes, and tests connectivity.
---

You are executing the `/browser-reset` command to diagnose and recover from flaky browser MCP connections. Browser MCPs (Chrome DevTools, Playwright) frequently get stuck — stale processes, broken connections, port conflicts. This command systematically fixes them.

## Step 1: Diagnose Current State

Run these commands to understand what's running:

```bash
# Chrome processes with remote debugging
ps aux | grep -i 'chrome.*remote-debugging\|chromium.*remote-debugging' | grep -v grep
```

```bash
# Playwright browser processes
ps aux | grep -i 'playwright\|pw-browser' | grep -v grep
```

```bash
# MCP server processes (node processes running MCP servers)
ps aux | grep -i 'chrome-devtools-mcp\|playwright.*mcp\|agent-browser' | grep -v grep
```

```bash
# What's listening on common debugging ports
lsof -i :9222 -i :9223 -i :9229 2>/dev/null | head -20
```

Report what you find in a clear table:
```
Browser State:
- Chrome (remote debug): [running on port X / not running]
- Playwright browsers:   [N processes / none]
- MCP servers:           [chrome-devtools: running/not / playwright: running/not]
- Port 9222:             [in use by X / free]
```

**Diagnostic gate — is Chrome debuggable, or is the MCP the problem?** Run this early to point the fix at the right layer:
```bash
curl -s http://localhost:9222/json/version
```
- **Returns Chrome version JSON** → Chrome IS debuggable, so the fault is in the MCP layer. Don't relaunch Chrome — restart the MCP server / Claude Code (Step 3 onward).
- **Fails / connection refused** → Chrome itself isn't exposing the debug port. Enable remote debugging at `chrome://inspect/#remote-debugging`, or relaunch Chrome with `--remote-debugging-port=9222`. The MCP is probably fine; don't restart it first.

**Tab-count caution:** Chrome DevTools MCP force-loads every open tab (Chrome ≤149), so a browser with very many tabs can cause CDM connection timeouts during connect. If the user has dozens or hundreds of tabs open, have them close the excess before retrying.

## Step 1.5: Read the Error First (do this BEFORE killing anything)

The actual error string is the fastest path to the right fix — killing processes blind is a last resort, not the first move. Read the MCP server logs:

```bash
# Chrome DevTools MCP log (macOS)
tail -n 40 ~/Library/Logs/Claude/mcp-server-chrome-devtools.log 2>/dev/null
```

```bash
# Playwright MCP log (macOS)
tail -n 40 ~/Library/Logs/Claude/mcp-server-playwright.log 2>/dev/null
```

If the logs are empty or uninformative, restart the server with verbose logging to capture the real error, then reproduce the failure:
```bash
DEBUG=* npx chrome-devtools-mcp@latest --log-file=<scratchpad>/cdm.log
```
(Replace `<scratchpad>` with a writable temp path. For Playwright, run the server from a terminal — see Step 3.5 — to surface suppressed errors.)

**Error-string triage** — match what you find in the log to the targeted fix:

| Error string | Meaning | Fix |
|---|---|---|
| `Target closed` | A Chrome instance is already running, so the MCP couldn't start its own | Fully quit Chrome (`Cmd+Q` / kill all Chrome, not just the window), then retry |
| `Could not find DevToolsActivePort` / `Network.enable timed out` / `socket connection was closed unexpectedly` | `--autoConnect` can't reach a debuggable Chrome | Work the autoConnect checklist below |
| `ERR_MODULE_NOT_FOUND` | Wrong Node version or a corrupt npx cache | `rm -rf ~/.npm/_npx && npm cache clean --force`, confirm `node -v` ≥ 18, then retry |

**`--autoConnect` timeout checklist** (the most specific "browser stuck" case — work through all four):
1. Chrome 144+ is already RUNNING (`chrome://version` to confirm).
2. Remote debugging is enabled at `chrome://inspect/#remote-debugging`.
3. The connection prompt was actually accepted (CDM asks the first time).
4. Nothing else holds the debug port — re-check `lsof -i :9222` from Step 1.

After a targeted fix, verify with the single safest call — `mcp__chrome-devtools__list_pages` — rather than retrying the complex command that failed.

## Step 2: Kill Stale Processes

If you found stale or stuck processes:

**Kill stale Playwright browsers** (these are headless Chrome instances spawned by Playwright that outlive their session):
```bash
pkill -f 'pw-browser|playwright.*chromium' 2>/dev/null; echo "Killed stale Playwright browsers"
```

**Kill stale MCP node processes** (only if they're orphaned/stuck):
```bash
# Only kill chrome-devtools-mcp if it's not responding
pkill -f 'chrome-devtools-mcp' 2>/dev/null; echo "Killed stale chrome-devtools-mcp"
```

**Do NOT kill the user's main Chrome browser.** Only kill:
- Headless chromium instances from Playwright
- Node processes running MCP servers
- Chrome instances explicitly launched with `--remote-debugging-port` by automation tools

If Chrome was launched normally by the user with remote debugging enabled at chrome://inspect, do NOT kill it.

## Step 3: Test MCP Connections

After cleanup, test each available MCP:

**Chrome DevTools MCP:**
Try calling `mcp__chrome-devtools__list_pages`. Report the result:
- Success: "Chrome DevTools MCP: connected ([N] pages found)"
- Failure: "Chrome DevTools MCP: not responding"

**Playwright MCP:**
Try calling `mcp__plugin_playwright_playwright__browser_snapshot`. Report the result:
- Success: "Playwright MCP: connected"
- Failure: "Playwright MCP: not responding"

## Step 3.5: Isolate the Server Standalone

If an MCP still won't connect after cleanup, the fault is likely Node version, a missing browser binary, or a version skew — none of which process-killing can fix. Run the server directly to surface the real error:

**Chrome DevTools MCP:**
```bash
npx chrome-devtools-mcp@latest --help   # verifies the package installs and Node can run it
```

**Playwright MCP:**
```bash
npx playwright install   # installs missing browser binaries — the #1 Playwright MCP failure
node -v                  # must be >= 18
```

**Pin versions, don't float `@latest`.** A version mismatch between the MCP server and the client is a top cause of "No tools detected" / "only N tools available." If you see that symptom, pin the MCP to a known-good version in the MCP config rather than `@latest`.

## Step 4: Report and Fix

Present the final status:

```
Browser MCP Status After Reset:
- Chrome DevTools MCP: [connected / not responding]
- Playwright MCP:      [connected / not responding]
```

**If Chrome DevTools MCP is not responding**, provide specific fix steps:
```
Chrome DevTools MCP fix:

1. Make sure Chrome is running (just open it normally)

2. Enable remote debugging — pick ONE:
   a) In Chrome: go to chrome://inspect/#remote-debugging and enable it
   b) Or quit Chrome and relaunch with:
      /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

3. Verify: curl -s http://localhost:9222/json/version
   (should return Chrome version info)

4. The MCP server should auto-reconnect. If not, restart Claude Code.
```

**If the client is sandboxed** (Claude Desktop, macOS Seatbelt, or a container), the MCP cannot spawn Chrome itself. Start Chrome manually with the debug port, then point the MCP at it instead of auto-connect:
```bash
# 1. Launch Chrome with the debug port
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
# 2. In the MCP config args, attach via URL instead of auto-connect:
#    --browser-url=http://127.0.0.1:9222
```

**If only a limited subset of tools is visible** (e.g. ~9 Chrome DevTools tools instead of the full set), the client is in read-only / plan mode — exit it and the full toolset returns.

**If Playwright MCP is not responding:**
```
Playwright MCP fix:

1. The Playwright plugin should auto-restart. Try running a command that uses it.
2. To free a stuck or leaked browser context, call `mcp__plugin_playwright_playwright__browser_close`, then restart the MCP server to drop stale contexts. (`browser_close` also frees memory in long sessions.)
3. Missing browser binaries cause "No tools detected" — run `npx playwright install` (see Step 3.5) and confirm `node -v` >= 18.
4. If still broken, restart Claude Code — the plugin initializes on startup.
5. As a last resort: claude mcp remove plugin:playwright:playwright && restart Claude Code
```

**If both work:** "All browser connections are healthy. If you're still seeing issues, they may be intermittent — try the specific command that was failing."

## Tips for Preventing Flakiness

If the user asks, share these tips:
- **Prefer Chrome DevTools MCP** over Playwright — it connects to your existing browser instead of spawning new ones
- **Don't run both simultaneously** on the same page — they can interfere with each other
- **Chrome DevTools needs Chrome 144+** — check at `chrome://version`
- **Remote debugging** must be enabled at `chrome://inspect/#remote-debugging`
- If Chrome crashes, the MCP connection dies — just reopen Chrome and it should reconnect
