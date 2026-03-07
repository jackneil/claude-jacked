---
description: "Browser-based QA testing of UI changes from the current session. Pass a URL as argument, or let it auto-detect."
---

> **Tip:** MCP-based browser tools (Playwright MCP, Claude-in-Chrome) require no bash approval and work instantly with the jacked gatekeeper. If using `agent-browser`, pre-approve it once via **Always Allow** in the jacked logs UI — this adds `Bash(npx agent-browser:*)` to your allowlist.

You are a QA engineer testing UI changes from the current coding session. Follow these steps systematically.

## Step 1: Detect Browser Tools

Check which browser automation tools are available. Prefer MCP tools first — they require no bash permissions and work without gatekeeper prompts.

**Option A — Playwright MCP (preferred)**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, use Playwright tools for all browser interaction. Note to user:
> Using Playwright MCP, which opens separate browser windows. Install agent-browser (`npm i -g agent-browser`) or Claude-in-Chrome for in-browser operation.

**Option B — Claude-in-Chrome**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, use Claude-in-Chrome tools for all browser interaction.

**Option C — agent-browser CLI**: Run `npx agent-browser --version` via Bash. If it succeeds, use agent-browser for all browser interaction via Bash tool calls (e.g., `npx agent-browser open <url>`, `npx agent-browser snapshot`, `npx agent-browser screenshot <path>`, `npx agent-browser click <ref>`, `npx agent-browser type <ref> <text>`, `npx agent-browser eval <js>`). This reuses your existing browser session — no new windows.
> Note: `npx` requires a gatekeeper approval prompt unless pre-approved. Add `Bash(npx agent-browser:*)` via the jacked "Always Allow" button to avoid repeated prompts.

**If none are available**: Tell the user:
```
No browser tools detected. Install one:
- Playwright MCP: Add to .mcp.json with --headless flag (no gatekeeper prompts)
- Claude-in-Chrome: Install the Chrome extension from https://chromewebstore.google.com
- agent-browser: npm i -g agent-browser (requires npx pre-approval)
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

## Step 5: Check for Login Credentials

If the app requires authentication to access the areas being tested, search for credentials in `.env` files before asking the user.

**Find the repo root** (`git rev-parse` is auto-approved by the gatekeeper):
```bash
git rev-parse --show-toplevel 2>/dev/null || pwd
```

**Scan env files** in priority order — run each grep separately (all are auto-approved, stop at first file with results):
```bash
grep -iE "^[A-Z_]*(EMAIL|PASSWORD|USERNAME|LOGIN)[A-Z_]*=" .env.local
grep -iE "^[A-Z_]*(EMAIL|PASSWORD|USERNAME|LOGIN)[A-Z_]*=" .env.development
grep -iE "^[A-Z_]*(EMAIL|PASSWORD|USERNAME|LOGIN)[A-Z_]*=" .env.test
grep -iE "^[A-Z_]*(EMAIL|PASSWORD|USERNAME|LOGIN)[A-Z_]*=" .env
```
Run from the repo root. **Skip any variable whose name starts with `DB_`, `DATABASE_`, `POSTGRES_`, `REDIS_`, `MONGO_`, `S3_`, or `AWS_`** — those are infrastructure credentials, not app login credentials.

**Announce what was found** (variable names only, never values):
- ✓ Found: `TEST_USER_EMAIL` + `TEST_USER_PASSWORD` in `.env.local` — "Using these for login."
- ✗ Not found: "No login credentials found in env files." → Ask the user for credentials.

**If login with found credentials fails:** Warn the user ("Credentials from `.env.local` were rejected") and ask for correct credentials. Do not retry silently.

**Security note:** If credentials were found in `.env.local`, `.env.development`, or `.env.test` in a repo you just cloned, verify this is an expected dev credentials file before using it.

## Step 6: Run QA Pass

**Screenshot setup** (agent-browser and Playwright only — Chrome does not support file-based screenshots):
```bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
rm -rf "$REPO_ROOT/tmp/qa_screenshots"
mkdir -p "$REPO_ROOT/tmp/qa_screenshots"
```
Save all screenshots to `$REPO_ROOT/tmp/qa_screenshots/<descriptive-name>.png`.
*Add `tmp/` to your project's `.gitignore` if it isn't already there.*

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

## Step 7: Report Findings

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

## Step 8: Investigate & Plan Fixes

If the report found 0 issues, announce:
**"All clear — 0 issues found. Skipping fix plan and /dcr verification."**
Go to Step 11 (Cleanup).

Otherwise, announce: **"[N] issues found. Writing implementation plan for fixes..."**

Use the `superpowers:writing-plans` skill to create a proper fix plan. Feed it the full issue list from the QA report as requirements. The writing-plans skill will:
- Investigate root causes by reading source files
- Produce bite-sized tasks with exact file paths, line numbers, and complete code changes
- Include verification steps and test commands for each fix
- Order by severity: CRITICAL > HIGH > MEDIUM > LOW

The plan should be scoped to fixing the QA-reported issues — not a general feature plan.

## Step 9: Execute Fix Plan

After the plan is written, execute it using the `superpowers:executing-plans` skill (or `superpowers:subagent-driven-development` for parallel execution). This ensures each fix is:
1. Verified against the current file state before applying
2. Tested after applying
3. Tracked to completion

**On failure**: If a fix introduces a test failure or error, STOP immediately. Do not attempt remaining fixes. Announce the failure with error details. The user decides how to proceed. Do NOT continue to /dcr with broken state.

After all items succeed, announce:
**"Fix plan complete — all [N] items resolved. Running /dcr for verification."**

## Step 10: Run /dcr

Invoke the /dcr skill for parallel recursive review of all changes. /dcr handles its own wave loop — it will review, fix, and re-verify until all selected lenses pass clean.

/dcr does NOT re-trigger /qa or /ux — it reviews code, not browser behavior.

After /dcr reports clean, proceed to Cleanup.

> **Note:** The investigate → plan → execute → /dcr pattern mirrors the same flow in `/ux`. Keep both in sync when updating.

## Step 11: Cleanup

Remove the screenshot directory after the full workflow completes:
```bash
rm -rf "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/tmp/qa_screenshots"
```
