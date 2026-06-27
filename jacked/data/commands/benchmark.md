---
description: Use before and after performance-sensitive changes. Captures web performance metrics, compares against baselines, flags regressions.
---

You are running performance benchmarking on a web application. You capture real browser performance metrics, compare against baselines, and flag regressions with specific thresholds.

## Arguments

`$ARGUMENTS` controls behavior:
- `<URL>` → benchmark that URL (capture + compare if baseline exists)
- `baseline <URL>` → capture baseline only
- `compare <URL>` → compare against existing baseline
- `--pages <URL1> <URL2> ...` → benchmark multiple pages
- Empty → ask for URL

## Step 0: Browser Health Check

Try calling `mcp__chrome-devtools__list_pages`.

**If it works:** use Chrome DevTools MCP. This is preferred — it gives BOTH a trace-based capture path (`performance_start_trace` + `performance_analyze_insight`, the most reliable way to measure LCP/CLS/INP and derive TBT, plus fix insights) AND `evaluate_script` for Navigation-Timing + resource breakdown.

**If it fails:** try `mcp__plugin_playwright_playwright__browser_evaluate`.
- If Playwright works: use Playwright MCP. There is no trace path here, so LCP/CLS come from a buffered `PerformanceObserver` (Step 1 fallback script). Throttle via CDP (Step 1).
- If both fail: "No browser tools responding. Run `/browser-reset` to diagnose." Stop.

## Step 1: Capture Performance Metrics

### Step 1a: Set and record throttling (do this FIRST, every run)

Unthrottled local runs report falsely-good numbers AND make baseline-vs-compare invalid if conditions drift — this is the #1 measurement pitfall. So pin a known profile before any capture.

**Default profile: `Slow 4G + 4x CPU`.** Allow override via `--profile <name>` (e.g. `No throttling`, `Fast 4G + 4x CPU`).

- **Chrome DevTools MCP:** call `mcp__chrome-devtools__emulate` to set CPU throttling (4x slowdown) and network throttling (Slow 4G) before navigating.
- **Playwright:** apply throttling via CDP (`Network.emulateNetworkConditions` + `Emulation.setCPUThrottlingRate`) through `browser_evaluate` / a CDP session before load. If CDP throttling is unavailable, record the profile as `unthrottled (Playwright)` and warn in the report that numbers are optimistic.

**Stamp the resolved profile into the captured JSON** as `"throttling": "<profile>"`. In compare mode, if the baseline's `throttling` differs from this run's, refuse the regression verdict and emit: `Throttling profile mismatch (baseline: X, current: Y) — re-baseline before comparing. Showing absolute budgets only.`

### Step 1b: Primary capture — trace path (Chrome DevTools MCP only)

This is the PRIMARY method when chrome-devtools MCP is available, because raw `getEntriesByType` cannot return LCP/CLS/TBT.

1. Call `mcp__chrome-devtools__performance_start_trace` with `reload=true, autoStop=true`. The returned summary is a compact (~4KB) AI-ready trace that reliably reports **LCP, CLS (and INP), plus the LCP subpart breakdown** (TTFB / load delay / load duration / render delay) — read those directly. **TBT** is not returned as a field; derive it from the trace's long-tasks when they're surfaced, otherwise fall back to the `PerformanceObserver` long-task approximation (Step 1c).
2. Optionally call `mcp__chrome-devtools__performance_analyze_insight` for actionable "why" insights — `LCPBreakdown`/`LCPDiscovery` (lazy/late-discovered LCP image), `CLSCulprits` (shifting elements), `RenderBlocking` (blocking CSS/JS), and third-party weight. Surface the top 2-3 in the report.
3. Still run the `evaluate_script` script below to enrich the resource breakdown (byType, slowest, transfer bytes) — the trace gives CWV, the script gives bytes.

### Step 1c: Capture / fallback — Navigation Timing + resources + observer-based CWV

Run via `evaluate_script` / `browser_evaluate`. On the Playwright (no-trace) path this is the ONLY source of CWV, so it installs a buffered `PerformanceObserver` for LCP and CLS — `getEntriesByType` alone silently omits the two most important metrics this command budgets.

Install the observer as early as possible (ideally before navigation; otherwise immediately on load with `buffered: true`):

**Observer install (run once per page, before the reload that you measure):**

```javascript
(() => {
  if (window.__benchCWV) return 'observer already installed';
  window.__benchCWV = { lcp: 0, cls: 0, longTasksMs: 0 };
  // Largest Contentful Paint — keep the latest (largest) value
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) window.__benchCWV.lcp = e.startTime;
  }).observe({ type: 'largest-contentful-paint', buffered: true });
  // Cumulative Layout Shift — sum shifts NOT caused by recent user input
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (!e.hadRecentInput) window.__benchCWV.cls += e.value;
    }
  }).observe({ type: 'layout-shift', buffered: true });
  // Long tasks — proxy for TBT (sum of task time over 50ms, post-FCP)
  // longtask does not support buffered:true, so only tasks AFTER install are seen (best-effort).
  try {
    new PerformanceObserver((list) => {
      const fcpEntry = performance.getEntriesByName('first-contentful-paint')[0];
      const fcp = fcpEntry ? fcpEntry.startTime : 0;
      for (const e of list.getEntries()) {
        if (e.startTime > fcp) window.__benchCWV.longTasksMs += Math.max(0, e.duration - 50);
      }
    }).observe({ type: 'longtask' });
  } catch (_) { /* longtask not supported in this browser */ }
  return 'observer installed';
})()
```

**Capture script (run after full load settles):**

```javascript
(() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paint = performance.getEntriesByType('paint');
  const fcp = paint.find(e => e.name === 'first-contentful-paint');
  const resources = performance.getEntriesByType('resource');
  const cwv = window.__benchCWV || { lcp: 0, cls: 0, longTasksMs: 0 };

  // Resource breakdown by type (third-party isolated by origin)
  const pageOrigin = location.origin;
  const byType = {};
  let thirdPartySize = 0, thirdPartyCount = 0;
  resources.forEach(r => {
    const ext = r.name.split('.').pop().split('?')[0].toLowerCase();
    const type = ['js','mjs'].includes(ext) ? 'javascript' :
                 ['css'].includes(ext) ? 'stylesheet' :
                 ['png','jpg','jpeg','gif','svg','webp','avif','ico'].includes(ext) ? 'image' :
                 ['woff','woff2','ttf','eot','otf'].includes(ext) ? 'font' : 'other';
    if (!byType[type]) byType[type] = { count: 0, totalSize: 0 };
    byType[type].count++;
    byType[type].totalSize += r.transferSize || 0;
    try { if (new URL(r.name).origin !== pageOrigin) { thirdPartySize += r.transferSize || 0; thirdPartyCount++; } } catch (_) {}
  });

  // Top 10 slowest resources
  const slowest = resources
    .map(r => ({ name: r.name.split('/').pop().split('?')[0], duration: Math.round(r.duration) }))
    .sort((a, b) => b.duration - a.duration)
    .slice(0, 10);

  return JSON.stringify({
    url: location.href,
    timestamp: new Date().toISOString(),
    timing: {
      ttfb: nav ? Math.round(nav.responseStart - nav.requestStart) : null,
      fcp: fcp ? Math.round(fcp.startTime) : null,
      // LCP/CLS from the buffered observer; trace path (Step 1b) is authoritative when available
      lcp: cwv.lcp ? Math.round(cwv.lcp) : null,
      cls: Number(cwv.cls.toFixed(3)),
      tbt: Math.round(cwv.longTasksMs), // long-task approximation of Total Blocking Time
      domInteractive: nav ? Math.round(nav.domInteractive) : null,
      domComplete: nav ? Math.round(nav.domComplete) : null,
      fullLoad: nav ? Math.round(nav.loadEventEnd) : null,
      domContentLoaded: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
    },
    resources: {
      total: resources.length,
      totalTransferSize: resources.reduce((sum, r) => sum + (r.transferSize || 0), 0),
      byType: byType,
      thirdParty: { count: thirdPartyCount, totalSize: thirdPartySize },
    },
    slowestResources: slowest,
    memory: performance.memory ? {
      usedJSHeapSize: performance.memory.usedJSHeapSize,
      totalJSHeapSize: performance.memory.totalJSHeapSize,
    } : null,
  }, null, 2);
})()
```

When the trace path (Step 1b) is available, prefer its LCP/CLS values over the observer values (the trace is authoritative for those), and prefer the trace's long-task-derived TBT when it's surfaced; use the observer values only on the Playwright/no-trace path or when the trace doesn't surface TBT.

### Step 1d: Run multiple samples + noise floor

Reload and measure **3 times** for most metrics; for the noisier load-time metrics (TTFB, FCP, LCP, Full Load) bump to **5 samples** when feasible. Re-install the observer (above) after each reload.

```javascript
// Force a clean reload between samples
location.reload()
```

Wait 3-5 seconds between reloads for the page to fully settle.

**Track the full distribution, not just the median.** For each metric record min, median, and max across the samples — the spread is the measurement noise floor, used in Step 4 to tell a real regression from jitter. Carry the median as the headline value and the spread as `±` context.

## Step 2: Baseline Mode

If `baseline` was specified, save the metrics:

Before writing, merge in the run conditions so a later compare can validate them: add the resolved `"throttling": "<profile>"` (Step 1a) and the per-metric spread (`min`/`max`) from Step 1d to the captured JSON.

Write to `~/.claude/jacked-benchmark/baseline-latest.json` using the Write tool with the captured metrics JSON.

Also save a timestamped copy:
Write to `~/.claude/jacked-benchmark/baseline-[YYYY-MM-DD-HHMMSS].json`

```
Baseline captured for: <URL>
Throttling:           <profile>   (e.g. Slow 4G + 4x CPU)

Timing (median ± spread):
  TTFB:             Nms (±N)
  FCP:              Nms (±N)
  LCP:              Nms (±N)
  CLS:              N.NNN
  TBT:              Nms (±N)
  DOM Interactive:  Nms
  DOM Complete:     Nms
  Full Load:        Nms (±N)

Resources:
  Total requests:   N
  Total transfer:   N KB
  JavaScript:       N files (N KB)
  Stylesheets:      N files (N KB)
  Images:           N files (N KB)
  Fonts:            N files (N KB)
  Third-party:      N files (N KB)

Run /benchmark <URL> after changes to compare.
```

## Step 3: Compare Mode

Load the baseline:
```bash
cat ~/.claude/jacked-benchmark/baseline-latest.json 2>/dev/null
```

If no baseline exists, report current metrics only with a note: "No baseline found. Run `/benchmark baseline <URL>` to capture one."

**Throttling guard (do this before any delta):** if `baseline.throttling` differs from the current run's profile, do NOT compute a regression verdict. Emit `Throttling profile mismatch (baseline: X, current: Y) — re-baseline before comparing. Showing absolute budgets only.` and skip to the Performance Budget section.

### Regression Thresholds

| Metric | Warning | Regression |
|--------|---------|------------|
| TTFB | >25% increase | >50% increase |
| FCP | >25% increase | >50% increase |
| LCP | >25% increase | >50% increase |
| CLS | +0.05 absolute | +0.1 absolute |
| TBT | >25% increase | >50% increase |
| DOM Complete | >25% increase | >50% increase |
| Full Load | >25% increase | >50% increase |
| Total transfer size | >15% increase | >25% increase |
| Request count | >25% increase | >50% increase |
| JS bundle size | >15% increase | >25% increase |
| Third-party size | >15% increase | >25% increase |

### Noise-floor suppression (don't cry regression over jitter)

A delta only counts if it's bigger than the measurement noise. For each metric, compare the current-vs-baseline delta against the run-to-run spread (baseline `max - min`, or the current run's spread if the baseline didn't record one). **If the absolute delta is smaller than the spread, downgrade any `REGRESSION`/`WARN` to `WARN (within noise)`** and say so. CLS deltas under 0.02 are noise. This keeps a +30% "regression" from firing when samples already vary ±40%.

### Industry Performance Budgets

Flag if these thresholds are exceeded regardless of baseline comparison (mirrors Lighthouse-CI's individual-audit + resource-summary budgets):

| Metric | Budget |
|--------|--------|
| FCP | < 1800ms |
| LCP | < 2500ms |
| CLS | < 0.1 |
| TBT | < 200ms |
| Speed Index | < 3400ms |
| Full Load | < 5000ms |
| Total transfer | < 1 MB |

Per-resource-type byte budgets (reuse the `byType` + `thirdParty` breakdown from Step 1c):

| Resource type | Budget |
|---------------|--------|
| Script (JS) | < 300 KB |
| Stylesheet (CSS) | < 100 KB |
| Image | < 500 KB |
| Font | < 100 KB |
| Third-party | < 300 KB |

(Speed Index is rarely available from the MCP trace — expect `n/a` unless a full Lighthouse run is wired in. Mark it `n/a — needs Lighthouse` whenever it isn't present.)

## Step 4: Report

```
## Performance Benchmark Report
**URL:** <URL>
**Date:** <timestamp>
**Throttling:** <profile> (e.g. Slow 4G + 4x CPU)
**Samples:** 3-5 (median headline, ± = run-to-run spread)
**CWV source:** trace (performance_start_trace) / observer (Playwright fallback)

### Timing Metrics
| Metric | Current (±spread) | Baseline | Delta | Status |
|--------|-------------------|----------|-------|--------|
| TTFB | Nms (±N) | Nms | +/-N% | OK / WARN / REGRESSION |
| FCP | Nms (±N) | Nms | +/-N% | OK / WARN / REGRESSION |
| LCP | Nms (±N) | Nms | +/-N% | OK / WARN / REGRESSION |
| CLS | N.NNN | N.NNN | +/-N.NNN | OK / WARN / REGRESSION |
| TBT | Nms (±N) | Nms | +/-N% | OK / WARN / REGRESSION |
| DOM Interactive | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| DOM Complete | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| Full Load | Nms (±N) | Nms | +/-N% | OK / WARN / REGRESSION |

(Append `(within noise)` to any status where the delta is smaller than the spread — see Step 3.)

### Resource Metrics
| Type | Count | Size | Baseline Count | Baseline Size | Delta |
|------|-------|------|----------------|---------------|-------|
| JavaScript | N | N KB | N | N KB | +/-N% |
| Stylesheets | N | N KB | N | N KB | +/-N% |
| Images | N | N KB | N | N KB | +/-N% |
| Fonts | N | N KB | N | N KB | +/-N% |
| Third-party | N | N KB | N | N KB | +/-N% |
| Total | N | N KB | N | N KB | +/-N% |

### Top 10 Slowest Resources
| Resource | Duration |
|----------|----------|
| filename.js | Nms |
| ... | ... |

### Trace Insights (when trace path ran)
From `performance_analyze_insight` — the actionable "why":
- **LCP:** <e.g. LCP image discovered late / render-blocking CSS; subpart breakdown TTFB Nms / load delay Nms / render delay Nms>
- **CLS:** <top shifting element(s)>
- **Render-blocking / third-party:** <top offenders>

### Performance Budget
| Metric | Value | Budget | Status |
|--------|-------|--------|--------|
| FCP | Nms | <1800ms | PASS / FAIL |
| LCP | Nms | <2500ms | PASS / FAIL |
| CLS | N.NNN | <0.1 | PASS / FAIL |
| TBT | Nms | <200ms | PASS / FAIL |
| Speed Index | Nms | <3400ms | PASS / FAIL / n/a |
| Full Load | Nms | <5000ms | PASS / FAIL |
| Total Transfer | N KB | <1MB | PASS / FAIL |
| Script (JS) | N KB | <300KB | PASS / FAIL |
| Stylesheet (CSS) | N KB | <100KB | PASS / FAIL |
| Image | N KB | <500KB | PASS / FAIL |
| Font | N KB | <100KB | PASS / FAIL |
| Third-party | N KB | <300KB | PASS / FAIL |

### Verdict: PASS / WARNING / REGRESSION
[Summary of what changed and why]

> **Lab vs field:** these are synthetic lab numbers under a fixed throttling profile. A page can pass here yet still fail real-user (CrUX) field thresholds, and INP is interaction-driven — a passive page-load benchmark does not capture it. For interaction/INP timing, use the `/qa` or `/ux` flow.
```

### Trend Analysis (if multiple baselines exist)

```bash
ls ~/.claude/jacked-benchmark/baseline-*.json 2>/dev/null
```

If multiple timestamped baselines exist, show a trend (only compare baselines captured under the SAME throttling profile):
```
### Trend (last N baselines, profile: <profile>)
| Date | LCP | CLS | FCP | Full Load | Transfer Size |
|------|-----|-----|-----|-----------|---------------|
| ... | ... | ... | ... | ... | ... |
```

## Hard Rules
- **READ-ONLY** — captures and reports metrics, never edits code
- **Trace path first** — on Chrome DevTools MCP, measure LCP/CLS/TBT via `performance_start_trace` (raw `getEntriesByType` can't); fall back to the buffered `PerformanceObserver` only on Playwright
- **Always throttle, always record it** — pin a profile (default Slow 4G + 4x CPU) and stamp it into the baseline; never compare across mismatched profiles
- **3-sample median (5 for load metrics)** — single measurements are noisy; report the spread and suppress regressions inside the noise floor
- **Both relative AND absolute** — compare against baseline AND industry budgets (full CWV + per-resource-type bytes)
- **Lab ≠ field** — synthetic numbers can pass while CrUX/INP fail; state the caveat, never imply lab is authoritative
- **Browser health first** — don't attempt metrics with broken browser tools
