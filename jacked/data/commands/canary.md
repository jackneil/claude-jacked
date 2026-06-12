---
description: Use after a production deploy to monitor for regressions. Takes periodic screenshots, checks console errors, and compares performance against baselines.
---

You are running post-deploy canary monitoring. You periodically check a live URL for anomalies — new console errors, visual regressions, performance degradation — and alert immediately if something looks wrong.

## Arguments

`$ARGUMENTS` controls behavior:
- A URL → monitor that URL
- `baseline <URL>` → capture baseline (run BEFORE deploying)
- `--duration <minutes>` → monitoring duration (default: 10, max: 30)
- `--interval <minutes>` → check interval (default: 2)
- Empty → use the URL from the most recent baseline, or ask

## Step 0: Browser Health Check

Before starting, verify browser tools are working. This prevents wasting time on a monitoring loop that can't actually check anything.

Try calling `mcp__chrome-devtools__list_pages`.

**If it works:** proceed with Chrome DevTools MCP.

**If it fails:** try `mcp__plugin_playwright_playwright__browser_snapshot`.
- If Playwright works: proceed with Playwright MCP, but note: "Using Playwright (Chrome DevTools preferred). Run `/browser-reset` if you'd prefer Chrome DevTools."
- If Playwright also fails: tell the user:
  ```
  No browser tools responding. Run /browser-reset to diagnose and fix.
  Cannot run canary monitoring without browser access.
  ```
  Stop.

## Browser Tool Mapping

Use whichever browser tool responded in Step 0. Here's the mapping:

| Action | Chrome DevTools MCP | Playwright MCP |
|--------|-------------------|----------------|
| Navigate | `mcp__chrome-devtools__navigate_page` | `mcp__plugin_playwright_playwright__browser_navigate` |
| Screenshot | `mcp__chrome-devtools__take_screenshot` | `mcp__plugin_playwright_playwright__browser_take_screenshot` |
| Snapshot (DOM) | `mcp__chrome-devtools__take_snapshot` | `mcp__plugin_playwright_playwright__browser_snapshot` |
| Console errors | `mcp__chrome-devtools__list_console_messages` | `mcp__plugin_playwright_playwright__browser_console_messages` |
| Run JS | `mcp__chrome-devtools__evaluate_script` | `mcp__plugin_playwright_playwright__browser_evaluate` |
| Network | `mcp__chrome-devtools__list_network_requests` | `mcp__plugin_playwright_playwright__browser_network_requests` |

## Baseline Mode (`baseline <URL>`)

Capture a reference state BEFORE deploying:

1. **Navigate** to the URL
2. **Capture baseline data:**
   - Take a screenshot
   - Take a DOM snapshot (accessibility tree)
   - Collect console messages (note any pre-existing errors)
   - Run performance measurement:
     ```javascript
     JSON.stringify({
       timing: performance.getEntriesByType('navigation')[0]?.toJSON(),
       resources: performance.getEntriesByType('resource').length,
       fcp: performance.getEntriesByName('first-contentful-paint')[0]?.startTime,
       lcp: new PerformanceObserver(() => {}).observe && 'supported',
       memory: performance.memory ? {
         usedJSHeapSize: performance.memory.usedJSHeapSize,
         totalJSHeapSize: performance.memory.totalJSHeapSize
       } : 'not available'
     })
     ```
   - Count visible elements in the DOM snapshot

3. **Save baseline:**
   Write a summary to `~/.claude/jacked-canary/baseline-latest.json` using the Write tool:
   ```json
   {
     "url": "<URL>",
     "timestamp": "<ISO timestamp>",
     "console_errors": ["<list of pre-existing errors>"],
     "element_count": <N>,
     "performance": { "<timing data>" },
     "resource_count": <N>
   }
   ```

4. **Report:**
   ```
   Baseline captured for: <URL>
   - Console errors: N pre-existing
   - DOM elements: N
   - Resources loaded: N
   - FCP: Nms

   Now deploy your changes, then run: /canary <URL>
   ```

## Monitoring Mode (default)

1. **Load baseline** (if available):
   ```bash
   cat ~/.claude/jacked-canary/baseline-latest.json 2>/dev/null
   ```
   If no baseline exists, that's OK — monitoring will still check for errors and crashes, just can't compare against a known-good state.

2. **Navigate** to the target URL.

3. **Run monitoring loop:**

   For each check interval (default every 2 minutes, for the configured duration):

   ### Check A: Page loads successfully
   - Navigate to the URL
   - If navigation fails (timeout, error), **ALERT IMMEDIATELY**

   ### Check B: Console errors
   - List console messages
   - Filter for errors (not warnings or info)
   - Compare against baseline errors (if available) — only flag NEW errors
   - New console errors → **ALERT**

   ### Check C: Visual/DOM check
   - Take a DOM snapshot
   - Compare element count against baseline (if available)
   - If element count dropped by >30% → possible blank screen or broken render → **ALERT**

   ### Check D: Performance
   - Run the performance measurement script
   - Compare against baseline (if available):
     - FCP increased by >100% → **WARN**
     - Resource count changed by >50% → **WARN**

   ### Check E: Network errors
   - List network requests
   - Check for any failed requests (4xx, 5xx)
   - New failures not in baseline → **ALERT**

   **Between checks:** Report status:
   ```
   Canary check [N/total] at [time]: OK
   - Console: [clean / N new errors]
   - DOM: [N elements, stable / changed by X%]
   - Performance: [FCP Nms, stable / regressed by X%]
   - Network: [clean / N failed requests]
   ```

4. **On ALERT:**
   ```
   CANARY ALERT at [time] — [what went wrong]

   Evidence:
   - [specific error messages or metrics]

   Recommended actions:
   1. Check the deployment logs
   2. Consider reverting: git revert HEAD && git push
   3. Run /canary again after fixing
   ```

   Continue monitoring after an alert — don't stop the loop. Multiple issues may emerge.

5. **Final report:**
   ```
   Canary monitoring complete — [duration] minutes, [N] checks

   Result: HEALTHY / DEGRADED / FAILING

   Summary:
   - Alerts: [N] ([list])
   - Warnings: [N] ([list])
   - Console errors: [N new since baseline]
   - Performance: [stable / regressed by X%]

   [If baseline was used: "Compared against baseline from [timestamp]"]
   ```

## Auto-Discovery (if no URL provided and no baseline)

Try to detect the production URL:

```bash
# Railway
railway status 2>/dev/null | grep -i 'url\|domain'
```

```bash
# Vercel
cat vercel.json 2>/dev/null | grep -i 'url\|alias'
```

```bash
# package.json homepage
grep '"homepage"' package.json 2>/dev/null
```

```bash
# CNAME file
cat CNAME 2>/dev/null
```

If found, confirm with the user before monitoring.

## Hard Rules
- **READ-ONLY** — this command monitors and reports, never edits code
- **Always check browser health first** — don't start a monitoring loop with broken tools
- **Never stop on first alert** — complete the monitoring duration to catch cascading issues
- **Distinguish new vs pre-existing** — only baseline-relative changes are actionable
