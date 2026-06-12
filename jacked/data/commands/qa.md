---
description: Use when testing UI changes for visual correctness, interactions, console errors, and edge cases. Pass a URL as argument, or let it auto-detect.
---

> **Tip:** MCP-based browser tools (Playwright MCP, Claude-in-Chrome) require no bash approval and work instantly with the jacked gatekeeper. If using `agent-browser`, pre-approve it once via **Always Allow** in the jacked logs UI — this adds `Bash(npx agent-browser:*)` to your allowlist.

You are a QA engineer testing UI changes from the current coding session. Follow these steps systematically.

## Config Override

If this command was invoked via a local config wrapper (you see a `## Repo Config` section earlier in the prompt), use that config to skip detection:
- **Browser Tool** specified? → Skip Step 1, use the declared tool directly (fall back to detection if it's unavailable)
- **Stack** declared? → Skip tech stack inference in Step 2
- **Dev Server Port** specified? → Use it in Step 4 URL detection (still check `lsof` as fallback)
- **Credential Hints** listed? → Use those variable names in Step 5 credential search
- **Framework-Specific Checks** listed? → Add them to Step 6 QA pass (in addition to standard checks)

If the config overlay date is more than 90 days old, mention: "Your `/qa` config is over 90 days old — consider running `/jacked-setup qa` to refresh it."

If no `## Repo Config` section is present, run all detection steps normally.

## Step 1: Detect Browser Tools

Check which browser automation tools are available. Prefer MCP tools first — they require no bash permissions and work without gatekeeper prompts.

**Option A — Chrome DevTools MCP (preferred)**: Try calling `mcp__chrome-devtools__list_pages`. If it works, use Chrome DevTools MCP tools for all browser interaction:
- `mcp__chrome-devtools__navigate_page` → open pages
- `mcp__chrome-devtools__take_snapshot` → accessibility tree (preferred for element detection)
- `mcp__chrome-devtools__take_screenshot` → visual screenshot
- `mcp__chrome-devtools__click` → click element by ref from snapshot
- `mcp__chrome-devtools__fill` → fill input fields
- `mcp__chrome-devtools__evaluate_script` → run JavaScript on page
- `mcp__chrome-devtools__emulate` → change viewport size (mobile/tablet testing)
- `mcp__chrome-devtools__list_console_messages` → check for JS errors
- `mcp__chrome-devtools__list_network_requests` → check for failed requests

**If Chrome DevTools MCP fails** (tool call errors, connection refused, or no pages returned): Tell the user:
```
Chrome DevTools MCP is not responding. To fix:

1. Chrome version: You need Chrome 144 or newer.
   Check yours at chrome://version — update if needed.

2. Enable remote debugging (pick one):
   a) In Chrome: go to chrome://inspect/#remote-debugging and enable it
   b) Or launch Chrome with: --remote-debugging-port=9222
      macOS:  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
      Linux:  google-chrome --remote-debugging-port=9222

3. If not installed: run `jacked install` (includes Chrome DevTools MCP setup)
   or manually: claude mcp add -s user chrome-devtools -- npx chrome-devtools-mcp@latest --autoConnect
```
Then continue to Option B as fallback.

**Option B — Playwright MCP**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, use Playwright tools for all browser interaction. Note to user:
> Using Playwright MCP (Chrome DevTools MCP is preferred — see above). Playwright opens separate browser windows.

**Option C — Claude-in-Chrome**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, use Claude-in-Chrome tools for all browser interaction.

**Option D — agent-browser CLI**: Run `npx agent-browser --version` via Bash. If it succeeds, use agent-browser for all browser interaction via Bash tool calls (e.g., `npx agent-browser open <url>`, `npx agent-browser snapshot`, `npx agent-browser screenshot <path>`, `npx agent-browser click <ref>`, `npx agent-browser type <ref> <text>`, `npx agent-browser eval <js>`). This reuses your existing browser session — no new windows.
> Note: `npx` requires a gatekeeper approval prompt unless pre-approved. Add `Bash(npx agent-browser:*)` via the jacked "Always Allow" button to avoid repeated prompts.

**If none are available**: Tell the user:
```
No browser tools detected. Recommended setup:

  jacked install    (configures Chrome DevTools MCP automatically)

Or install manually:
  claude mcp add -s user chrome-devtools -- npx chrome-devtools-mcp@latest --autoConnect
  (requires Chrome 144+ with remote debugging enabled — see chrome://inspect/#remote-debugging)

Alternatives:
- Playwright MCP: Add to .mcp.json with --headless flag
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

### Accessibility Lens (if available)

Check if an accessibility specialist lens is installed:

```bash
ls ~/.claude/lenses/accessibility.md .claude/lenses/accessibility.md 2>/dev/null | head -1
```

If found, read it and incorporate its "What to check" items into your testing checklist. These are **additive** — they don't replace your existing QA checks. Focus on items that can be verified visually or via browser DevTools:

- Color contrast (use DevTools accessibility panel or Lighthouse)
- Keyboard navigation (tab through the page, verify focus indicators)
- Semantic HTML (inspect elements — buttons should be `<button>`, not `<div>`)
- Form labels (each input has a visible, associated `<label>`)
- Focus management after interactions (modal open/close, route changes)

Skip items that require specialized tooling (screen reader testing, automated WCAG scanners) unless the user specifically requests them.

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

## Step 8: Cleanup

Remove the screenshot directory after presenting the report:
```bash
rm -rf "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/tmp/qa_screenshots"
```

This command is **read-only** — it detects and reports issues but does NOT fix them. The detailed issue list is returned to the parent caller (Claude Code), which should then use `superpowers:writing-plans` to build a fix plan from the findings, let the user iterate on it, and execute with `/dcr` verification.

> **Tip:** Run `/jacked-setup qa` to generate a repo-specific config that skips browser detection, bakes in your tech stack, and adds framework-specific QA checks.
