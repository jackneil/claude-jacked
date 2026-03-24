---
description: "Performance regression detection — captures web performance metrics, compares against baselines, flags regressions"
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

**If it works:** use Chrome DevTools MCP. This is preferred — `evaluate_script` gives direct access to the Performance API.

**If it fails:** try `mcp__plugin_playwright_playwright__browser_evaluate`.
- If Playwright works: use Playwright MCP.
- If both fail: "No browser tools responding. Run `/browser-reset` to diagnose." Stop.

## Step 1: Capture Performance Metrics

Navigate to the target URL, wait for full page load, then capture metrics:

### Core Web Vitals + Timing

Run via `evaluate_script` / `browser_evaluate`:

```javascript
(() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const paint = performance.getEntriesByType('paint');
  const fcp = paint.find(e => e.name === 'first-contentful-paint');
  const resources = performance.getEntriesByType('resource');

  // Resource breakdown by type
  const byType = {};
  resources.forEach(r => {
    const ext = r.name.split('.').pop().split('?')[0].toLowerCase();
    const type = ['js'].includes(ext) ? 'javascript' :
                 ['css'].includes(ext) ? 'stylesheet' :
                 ['png','jpg','jpeg','gif','svg','webp','ico'].includes(ext) ? 'image' :
                 ['woff','woff2','ttf','eot'].includes(ext) ? 'font' : 'other';
    if (!byType[type]) byType[type] = { count: 0, totalSize: 0 };
    byType[type].count++;
    byType[type].totalSize += r.transferSize || 0;
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
      domInteractive: nav ? Math.round(nav.domInteractive) : null,
      domComplete: nav ? Math.round(nav.domComplete) : null,
      fullLoad: nav ? Math.round(nav.loadEventEnd) : null,
      domContentLoaded: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
    },
    resources: {
      total: resources.length,
      totalTransferSize: resources.reduce((sum, r) => sum + (r.transferSize || 0), 0),
      byType: byType,
    },
    slowestResources: slowest,
    memory: performance.memory ? {
      usedJSHeapSize: performance.memory.usedJSHeapSize,
      totalJSHeapSize: performance.memory.totalJSHeapSize,
    } : null,
  }, null, 2);
})()
```

### Run Multiple Samples

For more accurate results, reload and measure 3 times. Use the median value for each metric.

```javascript
// Force a clean reload between samples
location.reload()
```

Wait 3-5 seconds between reloads for the page to fully settle.

## Step 2: Baseline Mode

If `baseline` was specified, save the metrics:

Write to `~/.claude/jacked-benchmark/baseline-latest.json` using the Write tool with the captured metrics JSON.

Also save a timestamped copy:
Write to `~/.claude/jacked-benchmark/baseline-[YYYY-MM-DD-HHMMSS].json`

```
Baseline captured for: <URL>

Timing:
  TTFB:             Nms
  FCP:              Nms
  DOM Interactive:  Nms
  DOM Complete:     Nms
  Full Load:        Nms

Resources:
  Total requests:   N
  Total transfer:   N KB
  JavaScript:       N files (N KB)
  Stylesheets:      N files (N KB)
  Images:           N files (N KB)
  Fonts:            N files (N KB)

Run /benchmark <URL> after changes to compare.
```

## Step 3: Compare Mode

Load the baseline:
```bash
cat ~/.claude/jacked-benchmark/baseline-latest.json 2>/dev/null
```

If no baseline exists, report current metrics only with a note: "No baseline found. Run `/benchmark baseline <URL>` to capture one."

### Regression Thresholds

| Metric | Warning | Regression |
|--------|---------|------------|
| TTFB | >25% increase | >50% increase |
| FCP | >25% increase | >50% increase |
| DOM Complete | >25% increase | >50% increase |
| Full Load | >25% increase | >50% increase |
| Total transfer size | >15% increase | >25% increase |
| Request count | >25% increase | >50% increase |
| JS bundle size | >15% increase | >25% increase |

### Industry Performance Budgets

Flag if these thresholds are exceeded regardless of baseline comparison:

| Metric | Budget |
|--------|--------|
| FCP | < 1800ms |
| LCP | < 2500ms |
| Full Load | < 5000ms |
| Total JS | < 300 KB (compressed) |
| Total transfer | < 1 MB |

## Step 4: Report

```
## Performance Benchmark Report
**URL:** <URL>
**Date:** <timestamp>
**Samples:** 3 (median values)

### Timing Metrics
| Metric | Current | Baseline | Delta | Status |
|--------|---------|----------|-------|--------|
| TTFB | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| FCP | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| DOM Interactive | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| DOM Complete | Nms | Nms | +/-N% | OK / WARN / REGRESSION |
| Full Load | Nms | Nms | +/-N% | OK / WARN / REGRESSION |

### Resource Metrics
| Type | Count | Size | Baseline Count | Baseline Size | Delta |
|------|-------|------|----------------|---------------|-------|
| JavaScript | N | N KB | N | N KB | +/-N% |
| Stylesheets | N | N KB | N | N KB | +/-N% |
| Images | N | N KB | N | N KB | +/-N% |
| Fonts | N | N KB | N | N KB | +/-N% |
| Total | N | N KB | N | N KB | +/-N% |

### Top 10 Slowest Resources
| Resource | Duration |
|----------|----------|
| filename.js | Nms |
| ... | ... |

### Performance Budget
| Metric | Value | Budget | Status |
|--------|-------|--------|--------|
| FCP | Nms | <1800ms | PASS / FAIL |
| Full Load | Nms | <5000ms | PASS / FAIL |
| Total JS | N KB | <300KB | PASS / FAIL |
| Total Transfer | N KB | <1MB | PASS / FAIL |

### Verdict: PASS / WARNING / REGRESSION
[Summary of what changed and why]
```

### Trend Analysis (if multiple baselines exist)

```bash
ls ~/.claude/jacked-benchmark/baseline-*.json 2>/dev/null
```

If multiple timestamped baselines exist, show a trend:
```
### Trend (last N baselines)
| Date | FCP | Full Load | Transfer Size |
|------|-----|-----------|---------------|
| ... | ... | ... | ... |
```

## Hard Rules
- **READ-ONLY** — captures and reports metrics, never edits code
- **3-sample median** — single measurements are noisy, always take 3
- **Both relative AND absolute** — compare against baseline AND industry budgets
- **Browser health first** — don't attempt metrics with broken browser tools
