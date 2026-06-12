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

**If Playwright MCP is not responding:**
```
Playwright MCP fix:

1. The Playwright plugin should auto-restart. Try running a command that uses it.
2. If still broken, restart Claude Code — the plugin initializes on startup.
3. As a last resort: claude mcp remove plugin:playwright:playwright && restart Claude Code
```

**If both work:** "All browser connections are healthy. If you're still seeing issues, they may be intermittent — try the specific command that was failing."

## Tips for Preventing Flakiness

If the user asks, share these tips:
- **Prefer Chrome DevTools MCP** over Playwright — it connects to your existing browser instead of spawning new ones
- **Don't run both simultaneously** on the same page — they can interfere with each other
- **Chrome DevTools needs Chrome 144+** — check at `chrome://version`
- **Remote debugging** must be enabled at `chrome://inspect/#remote-debugging`
- If Chrome crashes, the MCP connection dies — just reopen Chrome and it should reconnect
