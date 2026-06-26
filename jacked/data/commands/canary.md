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
- `--failure-limit <N>` → consecutive failures of the same check before it ALERTs (default: 2). One bad sample is a logged blip, not an alert.
- `--journey <description>` → a critical user journey to smoke-test each interval (e.g. `--journey "click Sign In, assert dashboard heading appears"`). When omitted, falls back to a bare navigate.
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
     (() => {
       const lcp = performance.getEntriesByType('largest-contentful-paint');
       return JSON.stringify({
         timing: performance.getEntriesByType('navigation')[0]?.toJSON(),
         resources: performance.getEntriesByType('resource').length,
         fcp: performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null,
         lcp: lcp.length ? lcp[lcp.length - 1].startTime : null,
         memory: performance.memory ? {
           usedJSHeapSize: performance.memory.usedJSHeapSize,
           totalJSHeapSize: performance.memory.totalJSHeapSize
         } : 'not available'
       });
     })()
     ```
   - Count visible elements in the DOM snapshot
   - **Capture a build fingerprint** so monitoring can later confirm the NEW deploy is actually being served (not a stale CDN/cache). Take the first one that resolves, in order:
     1. The hash of the main JS/CSS asset from the network requests (e.g. `main.a1b2c3d4.js` → `a1b2c3d4`).
     2. A `<meta name="build">` / `<meta name="version">` content value:
        ```javascript
        document.querySelector('meta[name="build"],meta[name="version"]')?.content || null
        ```
     3. The body of a `/version` or `/healthz` endpoint (fetch it; record the version/build/commit field).
     Record the source and value. If none resolve, set `build_fingerprint` to `null` and note that build-verification will be skipped during monitoring.

3. **Save baseline:**
   Write a summary to `~/.claude/jacked-canary/baseline-latest.json` using the Write tool:
   ```json
   {
     "url": "<URL>",
     "timestamp": "<ISO timestamp>",
     "console_errors": ["<list of pre-existing errors>"],
     "element_count": <N>,
     "performance": { "<timing data>" },
     "resource_count": <N>,
     "build_fingerprint": { "source": "asset-hash|meta|version-endpoint|null", "value": "<hash/version or null>" }
   }
   ```

4. **Report:**
   ```
   Baseline captured for: <URL>
   - Console errors: N pre-existing
   - DOM elements: N
   - Resources loaded: N
   - FCP: Nms
   - Build fingerprint: <source>=<value> (or "none — build-verification will be skipped")

   Now deploy your changes, then run: /canary <URL>
   ```

## Monitoring Mode (default)

1. **Load baseline** (if available):
   ```bash
   cat ~/.claude/jacked-canary/baseline-latest.json 2>/dev/null
   ```
   If no baseline exists, that's OK — monitoring will still check for errors and crashes, just can't compare against a known-good state.

2. **Navigate** to the target URL.

### Severity tiers (the contract — decide this BEFORE looping)

Borrowed from Argo Rollouts' dryRun/abort distinction: some signals are abort-worthy, others only inform. Classify every check against this table and never let a WARN-tier signal be the *sole* reason you recommend a revert.

| Tier | Checks | Meaning | Drives revert? |
|------|--------|---------|----------------|
| **ALERT** | page down (A), NEW console error (B), render collapse >30% (C), NEW 5xx / success-rate floor breach (E), failed journey assertion (F), wrong build served (G) | abort-worthy regression | Yes — recommend revert |
| **WARN** | perf delta / latency drift (D), resource-count drift, memory growth | informational ("dryRun") | No — never on its own |

### Step 2.5: Pre-loop health gate (fail-fast)

Before spending the full monitoring budget, run **every APPLICABLE** check (A–E always; F if `--journey` given; G if a baseline fingerprint exists) exactly ONCE against the first load. If ANY ALERT-tier check is already tripped on this first sample — page is down, a NEW console error is present, a 5xx is already serving, the journey assertion already fails, OR the build fingerprint did NOT change from baseline (still serving the old artifact) — **fail fast**:

```
CANARY FAIL-FAST at [time] — the first load is already broken; not spending [duration] min monitoring.

Tripped: [which ALERT-tier check(s)]
Evidence: [specific error / status / fingerprint]

Recommended: revert now (git revert HEAD && git push), then re-run /canary after the fix.
```

Then stop — there is no value in monitoring a deploy that is already failing. (This mirrors Flagger's pre-rollout acceptance test.) If the first load is clean, record it as the first successful sample and proceed to the loop.

3. **Run monitoring loop:**

   Walk one check interval at a time for the configured duration. **Track a per-check-class consecutive-failure streak** (Argo's `failureLimit` / Flagger's `threshold`): a check only escalates to an ALERT once it has failed `--failure-limit` (default 2) intervals *in a row*. A single bad sample is logged as a blip and resets the streak on the next clean sample — this is what kills false alarms from a transient 5xx, a cold-start latency spike, or one flaky network sample. Maintain streak counters and the running sample count in the rolling status log (see "Waiting between checks" below) so streaks survive across turns.

   Each check below resolves to **pass** or **fail** for this interval. A fail increments that check's streak; a pass resets it to 0. A check only ALERTs when its streak reaches `--failure-limit`.

   ### Check A: Page loads successfully (ALERT-tier)
   - Navigate to the URL
   - If navigation fails (timeout, error) → **fail** (streak++). ALERT once the streak hits `--failure-limit`. (One timeout is a blip; two in a row is a real outage.)

   ### Check B: Console errors (ALERT-tier)
   - List console messages
   - Filter for errors (not warnings or info)
   - Compare against baseline errors (if available) — only flag NEW errors
   - A NEW console error present → **fail** (streak++). ALERT at `--failure-limit`.

   ### Check C: Visual/DOM check (ALERT-tier)
   - Take a DOM snapshot
   - Compare element count against baseline (if available)
   - Element count dropped by >30% → possible blank screen or broken render → **fail** (streak++). ALERT at `--failure-limit`.

   ### Check D: Performance / latency (WARN-tier)
   Check **both** an absolute ceiling and a baseline delta — *error rate alone misses performance degradations*, and a delta-only check misses a deploy that is uniformly slow when the baseline was already slow.
   - Run the performance measurement script
   - **Absolute ceiling** (applies even with no baseline): FCP > 3000ms OR LCP > 4000ms → **WARN**
   - **Baseline delta** (if available): FCP increased by >100% → **WARN**; resource count changed by >50% → **WARN**
   - WARN-tier: streak it and surface it, but it never alone drives a revert recommendation.

   ### Check E: Network errors & success rate (ALERT-tier)
   - List network requests
   - **Success-rate floor** (applies even with no baseline): if <99% of tracked requests completed non-failed, OR any 5xx is present → **fail** (streak++). ALERT at `--failure-limit`. For this rate, "failed" = 5xx and network-level failures (timeout, DNS, connection reset); pre-existing/expected 4xx do not count against the rate.
   - **New failures**: any 4xx/5xx not in baseline → **fail** (streak++).
   - Track the success rate as a number (non-failed / total) so the final report can state it. Pair this with Check D: assert **both** success-rate AND latency every interval.

   ### Check F: Critical user journey (ALERT-tier, only if `--journey` given)
   - A 200 with a clean console can still be a functionally dead page; an element count alone passes a half-broken render. Exercise one real flow instead.
   - From the `--journey` description, script: navigate → wait for a named selector → interact (click/fill) → assert a post-action element or text exists.
   - If the post-action assertion fails (selector never appears, expected text absent) → **fail** (streak++). ALERT at `--failure-limit`.
   - When `--journey` is omitted, this check is skipped and the bare navigate (Check A) is the only load signal.

   ### Check G: Build verification (ALERT-tier)
   - Only runs if the baseline captured a non-null `build_fingerprint`. If null, skip and note "build-verification skipped (no baseline fingerprint)".
   - Re-read the live fingerprint the same way the baseline did (asset hash / `<meta name=build>` / `/version` body).
   - If the live fingerprint **equals** the baseline fingerprint, the new deploy is NOT being served (stale CDN/cache) → **fail** (streak++). ALERT at `--failure-limit`. This catches the silent false-HEALTHY where every page/console/perf check passes against the *old* artifact.

   **Between checks:** Append status to the rolling log and report:
   ```
   Canary check [N/total] at [time]: OK / BLIP / ALERT
   - Page:        [ok / fail streak X/limit]
   - Console:     [clean / N new errors, streak X/limit]
   - DOM:         [N elements, stable / changed by X%, streak X/limit]
   - Performance: [FCP Nms (ceiling 3000), LCP Nms (ceiling 4000), stable / regressed by X%]
   - Network:     [success-rate Y%, N failed, streak X/limit]
   - Build:       [fingerprint changed ✓ / STALE — old build / skipped]
   - Journey:     [passed / FAILED at step "<assertion>" / skipped]
   ```

   **Waiting between checks:** the harness blocks foreground `sleep`, so do NOT collapse the interval into back-to-back calls — that defeats duration-based monitoring. Instead, between intervals run a background `Monitor`/until-loop that waits `--interval` minutes (e.g. poll a `date`-based until-condition), then take the next sample when it returns. Persist every interval's result — streak counters, success rate, sample count — by **appending one line per interval to a single rolling status log** (e.g. `~/.claude/jacked-canary/run-latest.log`). The rolling log is the source of truth for streaks and sample counts so they survive across turns.

4. **On ALERT:**
   ```
   CANARY ALERT at [time] — [what went wrong] (failed [streak]/[--failure-limit] consecutive checks)

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

   Result: HEALTHY / DEGRADED / FAILING / INCONCLUSIVE

   Summary:
   - Alerts: [N] ([list]) — ALERT-tier only
   - Warnings: [N] ([list]) — WARN-tier, informational
   - Successful samples: [N] (need ≥2 to draw a confident verdict)
   - Success rate: [Y% non-failed requests]
   - Console errors: [N new since baseline]
   - Performance: [FCP/LCP vs ceilings; stable / regressed by X%]
   - Build: [new fingerprint confirmed / stale / not verified]

   [If baseline was used: "Compared against baseline from [timestamp]"]
   ```

   **Deciding the verdict** (Argo Rollouts models Inconclusive on purpose so noisy data neither auto-promotes nor auto-aborts):
   - **HEALTHY** — ≥2 successful samples, no ALERT-tier check reached its failure limit, success rate ≥99%, build verified (or no fingerprint to verify).
   - **FAILING** — one or more ALERT-tier checks hit `--failure-limit`.
   - **DEGRADED** — no ALERT escalation, but WARN-tier thresholds tripped repeatedly (latency ceiling / perf delta / resource drift).
   - **INCONCLUSIVE** — not enough signal to trust either way: <2 successful samples collected, OR no baseline AND no absolute floor was tripped, OR results flapped (streaks that kept resetting just under the limit). Recommendation: *"Hold — do not auto-trust or auto-revert. Extend monitoring (`--duration`) or capture a baseline, then re-run."* Never report HEALTHY off sparse or flapping data.

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
- **Require consecutive failures** — one bad sample is a logged blip, never an alert; only `--failure-limit` consecutive failures of the SAME check escalate to an ALERT. Kills false alarms from transient blips.
- **Never stop on first alert** — complete the monitoring duration to catch cascading issues. The ONE exception is the pre-loop health gate (Step 2.5): if the very first load is already broken, fail fast and recommend revert rather than burn the full duration.
- **WARN never reverts alone** — only ALERT-tier signals drive a revert recommendation; WARN-tier (latency/perf/resource drift) is informational, à la Argo's dryRun.
- **Assert both success-rate AND latency** — error rate alone misses performance degradations; check absolute floors/ceilings, not just baseline deltas.
- **Verify the build** — confirm the live fingerprint changed from baseline so you aren't reporting HEALTHY against a stale cached artifact.
- **Distinguish new vs pre-existing** — only baseline-relative changes are actionable
- **Don't over-claim confidence** — with <2 successful samples or no baseline and no floor tripped, report INCONCLUSIVE and hold; never auto-trust or auto-revert on sparse/flapping data.
