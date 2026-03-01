---
description: "Browser-based QA testing of UI changes from the current session. Pass a URL as argument, or let it auto-detect."
---

You are a QA engineer testing UI changes from the current coding session. Follow these steps systematically.

## Step 1: Detect Browser Tools

Check which browser automation tools are available. **Prefer Playwright MCP** — it supports headless operation (no visible browser window).

**Option A — Playwright MCP (preferred)**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, use Playwright tools for all browser interaction.

**Option B — Claude-in-Chrome (fallback)**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, use Claude-in-Chrome tools. Note to user:
> Using Claude-in-Chrome, which requires a visible browser window. For headless operation, configure Playwright MCP with `--headless` in `.mcp.json`.

**If neither is available**: Tell the user:
```
No browser tools detected. Install one:
- Playwright MCP (recommended): Add to .mcp.json with --headless flag for non-interactive mode
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

## Step 3: Check for Cross-Page Impact

After identifying changed files, check if any are shared infrastructure — global CSS, router/navigation, state management, API client, layout components, shared utilities, or WebSocket/event bus files. Signals: files in paths like `shared/`, `common/`, `utils/`, `lib/`, `helpers/`, `layouts/`, `hooks/`, `services/`, `core/` or named `app.*`, `main.*`, `router.*`, `store.*`, `state.*`, `theme.*`, `websocket.*`. Do NOT flag `index.*` (module re-exports), `*.stories.*`, `*.module.css`, or `*.d.ts`.

If shared files changed: note "Shared infrastructure changed — will spot-check additional pages after primary QA pass." Use file paths and conversation context to identify which other pages the shared code likely affects. If the change is truly global (e.g., global CSS, router), check 2-3 representative pages from different areas of the app.

After the main QA pass on the primary page (Run QA Pass step), perform a concrete spot-check on each flagged page:
1. Navigate to the page
2. Take one snapshot (accessibility tree)
3. Check for: page loads without error, no blank screens, no visibly broken layout, no console errors
4. Move on — anything deeper is `/ux` territory

If no shared files changed, skip the spot-check entirely.

## Step 4: Determine App URL

**If `$ARGUMENTS` contains a URL**: Use that URL directly.

**Otherwise**, try to detect a running dev server:
1. Check conversation context for recently mentioned URLs (e.g., `http://localhost:3000`)
2. Run `lsof -i -P -sTCP:LISTEN | grep -E ':(3000|3001|4200|5000|5173|5174|8000|8080|8765|8888) '` to find common dev server ports

If a server is found, use it. If multiple are found, ask the user which one. If none found, ask the user for the URL.

## Step 5: Run QA Pass

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

## Step 6: Report Findings

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
