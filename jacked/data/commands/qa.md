---
description: "Browser-based QA testing of UI changes from the current session. Pass a URL as argument, or let it auto-detect."
---

You are a QA engineer testing UI changes from the current coding session. Follow these steps systematically.

## Step 1: Detect Browser Tools

Check which browser automation tools are available:

**Option A — Playwright MCP**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, use Playwright tools for all browser interaction.

**Option B — Claude-in-Chrome**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, use Claude-in-Chrome tools.

**If neither is available**: Tell the user:
```
No browser tools detected. Install one:
- Playwright MCP: Add to .mcp.json or run: npx @anthropic-ai/claude-code-mcp-plugin-playwright
- Claude-in-Chrome: Install the Chrome extension from https://chromewebstore.google.com
```
Then stop.

## Step 2: Identify What Changed

Run `git diff --name-only HEAD` to see what files changed. Filter for UI-relevant files:
- `.js`, `.jsx`, `.ts`, `.tsx`, `.css`, `.scss`, `.less`, `.html`
- `.vue`, `.svelte`, `.erb`, `.jinja`, `.jinja2`

Ignore files in `node_modules/`, `dist/`, `build/`, `__pycache__/`, and test files (`*.test.*`, `*.spec.*`).

Summarize what UI areas were likely affected (e.g., "Login form styling", "Dashboard data table", "Navigation component").

If no UI files changed, tell the user and ask if they still want to proceed.

## Step 3: Determine App URL

**If `$ARGUMENTS` contains a URL**: Use that URL directly.

**Otherwise**, try to detect a running dev server:
1. Check conversation context for recently mentioned URLs (e.g., `http://localhost:3000`)
2. Run `lsof -i -P -sTCP:LISTEN | grep -E ':(3000|3001|4200|5000|5173|5174|8000|8080|8888) '` to find common dev server ports

If a server is found, use it. If multiple are found, ask the user which one. If none found, ask the user for the URL.

## Step 4: Run QA Pass

Navigate to the app URL. For each UI area affected by the changes:

### Visual Check
- Take a snapshot (accessibility tree preferred over screenshot for actionability)
- Look for broken layouts, missing elements, overlapping content
- Check that text is readable and properly aligned
- Verify colors, spacing, and visual hierarchy look correct

### Interactive Testing
- Click buttons and links — do they respond correctly?
- Fill out forms — do inputs accept text, show validation?
- Test navigation — do page transitions work?
- Check dropdowns, modals, toggles, and other interactive elements

### Console Errors
- Check the browser console for JavaScript errors
- Use `mcp__plugin_playwright_playwright__browser_console_messages` or `mcp__claude-in-chrome__read_console_messages`
- Flag any errors, especially new ones related to the changed code

### Edge Cases
- Empty states: What happens with no data?
- Long text: Does overflow handling work?
- Special characters: Do inputs handle `<script>`, quotes, unicode?
- Rapid interactions: Double-clicking, fast navigation

### Responsive (if applicable)
- Resize the browser to mobile width (375px) and check layout
- Resize to tablet width (768px) and check layout

## Step 5: Report Findings

Present a structured report:

```
## QA Report

### Summary
- [Area 1]: PASS / FAIL
- [Area 2]: PASS / FAIL

### Issues Found
1. **[Severity: HIGH/MEDIUM/LOW]** Description
   - Steps to reproduce
   - Expected behavior
   - Actual behavior
   - [Screenshot if available]

### Console Errors
- [List any JS errors found, or "None"]

### Suggestions
- [Optional improvements noticed during testing]
```

If everything passes, say so clearly. If issues are found, prioritize them by severity.
