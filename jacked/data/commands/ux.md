---
description: "Parallel browser-based UX checks — spawns focused agents to test different UX aspects on affected pages simultaneously"
---

You are the UX Check Dispatcher. You spawn parallel browser-testing agents, each focused on specific UX aspects on specific pages, to validate UI changes fast. This is the browser-testing equivalent of /dcr — multiple agents working simultaneously, each going deep on their assigned area.

## UX CHECK ASPECTS (5 total)

| # | Aspect | What to Check |
|---|--------|---------------|
| 1 | **Visual & Layout** | Broken layouts, overlapping elements, spacing, alignment, text readability, color consistency, images/icons loading |
| 2 | **Responsive** | Mobile (375px), tablet (768px), desktop (1280px+) — layout integrity, touch targets, text wrapping, no horizontal scroll |
| 3 | **Interactions** | Buttons, forms, navigation, modals, dropdowns, toggles, hover states, loading states, error feedback |
| 4 | **Console & Network** | JS errors, unhandled rejections, 404s, failed API calls, slow requests, deprecation warnings |
| 5 | **Accessibility** | Semantic HTML, focus order, keyboard navigation, color contrast, ARIA labels, screen reader landmarks |

## Step 1: Detect Browser Tools

Check which browser automation tools are available. **Prefer Playwright MCP** — it supports headless operation (no visible browser window).

**Option A — Playwright MCP (preferred)**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, set `browser = "playwright"`.

**Option B — Claude-in-Chrome (fallback)**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, set `browser = "chrome"`. Note to user:
> Using Claude-in-Chrome, which requires a visible browser window. For headless operation, configure Playwright MCP with `--headless` in `.mcp.json`.

**If neither is available**: Tell the user:
```
No browser tools detected. Install one:
- Playwright MCP (recommended): Add to .mcp.json with --headless flag for non-interactive mode
- Claude-in-Chrome: Install the Chrome extension from https://chromewebstore.google.com
```
Then stop.

## Step 2: Identify What Changed

Run `git diff --name-only HEAD` to find UI-relevant files:
- `.js`, `.jsx`, `.ts`, `.tsx`, `.css`, `.scss`, `.less`, `.html`
- `.vue`, `.svelte`, `.erb`, `.jinja`, `.jinja2`
- Ignore: `node_modules/`, `dist/`, `build/`, `__pycache__/`, test files (`*.test.*`, `*.spec.*`)

Map changed files to affected pages/flows using file paths and conversation context.

If no UI files changed, tell the user and ask if they still want to proceed.

## Step 3: Detect Cross-Page Impact

After identifying changed files, classify each by impact scope using **filename and path heuristics** to determine if additional pages need testing beyond the ones directly mapped from file paths.

### Tier 1 — Likely affects all pages (test every route/page)
- Global state management (stores, contexts, reducers, `window.*State` globals)
- Router/navigation configuration files
- Global CSS or theme files (not component-scoped CSS modules)
- API client/HTTP utilities shared across pages
- WebSocket/event bus infrastructure
- Layout shells, headers, footers, navbars shared across all routes

**Path signals:** `shared/`, `common/`, `utils/`, `lib/`, `helpers/`, `layouts/`, `hooks/`, `services/`, `core/`
**Filename signals:** `app.*`, `main.*`, `router.*`, `store.*`, `state.*`, `theme.*`, `websocket.*`
**Exclude from signals:** `index.*` (module re-exports), `*.stories.*` (Storybook), `*.module.css` (CSS modules), `*.d.ts` (type declarations) — these are not shared infrastructure even when they match a signal name like `app.*`.

### Tier 2 — Likely affects a group of related pages
- Shared utility functions (date formatting, validation, HTML escaping)
- Shared sub-components used by multiple but not all pages
- Shared state/filters within a feature group (e.g., log filter state affects all log sub-tabs)

**Path signals:** `components/shared/`, `components/common/`
**Default:** If uncertain whether a file is Tier 2 or Tier 3, default to Tier 3 — Tier 1 coverage catches most cross-page regressions anyway.

### Tier 3 — Likely isolated to one page
- Page-specific components, event handlers, page-scoped CSS
- Everything not matching Tier 1 or 2 patterns

**Important:** When Tier 1 expands the page list to "all pages," the existing agent cap (4 max) and prioritization rules from the Build Test Matrix step still apply. Group pages intelligently and prioritize those most affected by the changes.

### Announce Cross-Page Analysis

If no changed files were detected at all:
```
**Cross-page impact:** Could not analyze — no changed files detected by git diff.
```

If files changed but none match shared patterns:
```
**Cross-page impact:** None detected — changed files appear page-isolated.
**Pages to test:** [original list from file mapping]
```

If cross-page impact detected:
```
**Cross-page impact detected:**
- style.css changed (global CSS) → testing ALL pages for visual consistency
- utils.js changed (shared utilities) → adding accounts, analytics, logs pages
- logs-server.js changed → isolated to Logs > Server sub-tab
**Pages to test:** [expanded list]
```

## Step 4: Select UX Personas

Select 2-3 personas that match the project's target users. These personas shape HOW agents evaluate their assigned aspects — they don't replace the 5 aspects, they add evaluative bias.

### Persona Pool

| Persona | Mindset | Evaluation Bias |
|---------|---------|-----------------|
| **Novice User** | "Can I figure this out without docs?" | Label clarity, discoverability, forgiveness, onboarding, help text |
| **Power User** | "Can I do this fast?" | Keyboard shortcuts, info density, bulk operations, workflow efficiency |
| **Design Consultant** | "Does this look professional?" | Visual hierarchy, typography, color harmony, whitespace, polish |
| **Accessibility User** | "Can I use this with a screen reader?" | Screen reader flow, keyboard-only operation, contrast, ARIA, focus management |

### Selection Logic

Infer the project type from context (`README.md`, `package.json`, `CLAUDE.md`, conversation history). Use these signals to classify:

| Project Signal | Classification | Personas |
|----------------|---------------|----------|
| Marketing pages, no auth, public content | Consumer/public app | Novice + Design Consultant |
| Auth-gated, data tables, CRUD, internal users | Admin dashboard | Power User + Novice |
| CLI references, API docs, code editors, dev-facing | Developer tool | Power User + Accessibility |
| Product listings, cart, checkout, payments | E-commerce/SaaS | Novice + Design Consultant + Power User |
| Compliance refs (HIPAA, ADA, 508), .gov domains | Government/healthcare | Accessibility + Novice |
| No clear signals | Unknown/ambiguous | Novice + Design Consultant |

**$ARGUMENTS handling:** If `$ARGUMENTS` mentions a persona name, match loosely against pool names (e.g., "power" -> Power User, "a11y" -> Accessibility User). If no pool match, treat the text as a custom persona with the user's description as its evaluation bias. Always select at least 2, maximum 3.

### How Personas Apply

Each agent evaluates their assigned aspects THROUGH the lens of ALL selected personas. Tag persona-specific issues:
- `**[MEDIUM] [Novice]** Submit button label "POST" is jargon — unclear to non-technical users`
- `**[LOW] [Power User]** No keyboard shortcut for the primary action`

## Step 5: Detect Mobile Context

Determine whether the project targets mobile users using **weighted signals**:

**Strong signals (any one -> `mobile_deep_dive = true`):**
- PWA manifest (`manifest.json` / `manifest.webmanifest` with `display: standalone`)
- Mobile-specific libraries in `package.json` (`react-native`, `capacitor`, `ionic`, `@angular/pwa`)
- Conversation context explicitly mentioning mobile usage

**Moderate signals (need 2+ to trigger):**
- Custom responsive breakpoints below 480px in project CSS (not in `node_modules/`)
- Touch-specific CSS/JS (`@media (hover: none)`, `touchstart` handlers)
- Mobile-specific meta tags beyond basic viewport (`apple-mobile-web-app-capable`, `theme-color`)

**Ignored (too universal to be meaningful):**
- `<meta name="viewport">` alone — present in virtually all modern web projects
- Framework-default `@media` queries from CSS frameworks

**Override:** If the Select UX Personas step classified the project as "admin dashboard" or "developer tool", set `mobile_deep_dive = false` regardless of signals.

**Default:** If zero signals found and no override applies, `mobile_deep_dive = false`.

### Announce Personas & Mobile

```
**UX Personas — [N] selected**
- [Persona 1] — [reason]
- [Persona 2] — [reason]
**Mobile deep dive:** Yes ([signal]) / No ([reason])
```

## Step 6: Determine App URL

**If `$ARGUMENTS` contains a URL**: Use that URL directly.

**Otherwise**, try to detect a running dev server:
1. Check conversation context for recently mentioned URLs
2. Run `lsof -i -P -sTCP:LISTEN | grep -E ':(3000|3001|4200|5000|5173|5174|8000|8080|8765|8888) '`

If found, use it. If multiple, ask the user. If none, ask for the URL.

## Step 7: Build Test Matrix

Select which aspects are relevant based on what changed:
- CSS/styling changes → weight **Visual & Layout** + **Responsive**
- JS logic/component changes → weight **Interactions** + **Console & Network**
- HTML structure changes → weight **Accessibility** + **Visual & Layout**
- When in doubt, include the aspect.

Build a matrix of (aspect, page) and assign to 2-4 agents:

**1 page:**
- Agent A: Visual & Layout + Responsive
- Agent B: Interactions + Console & Network
- Agent C: Accessibility

**2 pages:**
- Agent A: Page 1 — Visual & Layout + Responsive
- Agent B: Page 1 — Interactions + Console & Network + Accessibility
- Agent C: Page 2 — Visual & Layout + Responsive
- Agent D: Page 2 — Interactions + Console & Network + Accessibility

**3+ pages:** Group intelligently, cap at 4 agents. Prioritize pages most affected by changes.

Use your judgment — adjust grouping based on what changed. Skip aspects that clearly don't apply (e.g., skip Responsive for a purely server-rendered admin page that's never used on mobile).

**Announce the matrix:**
```
**UX Check Matrix — [N] aspects across [M] agents**
**Personas:** [Persona 1] + [Persona 2] (inferred: [classification])
**Mobile deep dive:** Yes ([signal]) / No ([reason])
- Agent A: [Page URL] — Visual & Layout + Responsive
- Agent B: [Page URL] — Interactions + Console & Network
- Agent C: [Page URL] — Accessibility
```

## Step 8: Spawn Parallel Agents

Spawn ALL agents in ONE message using parallel Task tool calls. Each agent is `subagent_type: "general-purpose"`.

### Agent Prompt Template

Include ALL of the following in each agent's Task prompt:

```
You are a UX tester performing focused browser-based checks.

## BROWSER TOOL: [playwright / chrome]

[If Playwright]:
Use tools prefixed with `mcp__plugin_playwright_playwright__browser_*`:
- `browser_tabs` action "new" — create your own tab FIRST
- `browser_navigate` — go to a URL
- `browser_snapshot` — accessibility tree (preferred for element detection)
- `browser_take_screenshot` — visual screenshot
- `browser_click` — click element by ref
- `browser_type` — type text
- `browser_console_messages` — read console output
- `browser_network_requests` — inspect network
- `browser_resize` — change viewport size

[If Chrome]:
Use tools prefixed with `mcp__claude-in-chrome__*`:
- `tabs_create_mcp` — create your own tab FIRST
- `navigate` — go to a URL (use your tab ID)
- `read_page` — accessibility tree
- `computer` action "screenshot" — visual screenshot
- `computer` action "left_click" — click at coordinates
- `find` — natural language element search
- `read_console_messages` — console output
- `read_network_requests` — network activity
- `resize_window` — change viewport size

## CRITICAL: TAB ISOLATION

Before ANY browser action, create your own tab:
- [Playwright]: `browser_tabs` action "new", then navigate in the new tab
- [Chrome]: `tabs_create_mcp`, then use ONLY the returned tab ID for ALL subsequent calls

NEVER interact with other tabs. Work exclusively in your own tab.

## YOUR ASSIGNMENT

- **Page:** [URL]
- **Aspects:** [Aspect 1] + [Aspect 2]
- **Changed files:** [list of changed UI files]
- Focus your checks on areas most likely affected by these file changes.

## PERSONA LENSES

Evaluate ALL your assigned aspects through these persona lenses:

[For each selected persona, include: name, mindset, and evaluation bias from the pool table]

For every check, ask: would this pass for EACH persona? A button that works for a power user might confuse a novice. A layout that looks fine to a developer might look unprofessional to a design consultant. Tag persona-specific issues explicitly in your report:
- **[MEDIUM] [Novice]** Button label "Dispatch" is jargon — unclear to non-technical users
- **[LOW] [Power User]** No keyboard shortcut for the primary action

## ASPECT CHECKLISTS

### Visual & Layout
- [ ] Take a screenshot — look for broken layouts, overlapping elements
- [ ] Check text readability and alignment
- [ ] Verify colors and spacing are consistent
- [ ] Confirm images and icons load correctly
- [ ] Look for clipping, overflow, or z-index issues
- [ ] Compare against what the code change intended

### Responsive
- [ ] Resize to mobile (375px width) — check layout integrity
- [ ] Resize to tablet (768px width) — check layout integrity
- [ ] Resize back to desktop (1280px+) — confirm it restores correctly
- [ ] Check for horizontal scroll at each size
- [ ] Verify touch targets are large enough on mobile (44px minimum)
- [ ] Check text wrapping and truncation behavior

#### Mobile Deep Dive (include ONLY if mobile_deep_dive = true)
- [ ] Thumb-zone accessibility — primary actions reachable in bottom 2/3 of screen?
- [ ] Touch target spacing — >= 8px gap between adjacent tap targets
- [ ] Text readability — body text >= 16px without pinch-zoom
- [ ] Form input types — `type="email"`, `type="tel"`, `type="number"` for mobile keyboards
- [ ] Scroll behavior — no jank, no content hidden behind fixed headers/footers
- [ ] Orientation — landscape mode doesn't break layout

### Interactions
- [ ] Click all buttons — do they respond correctly?
- [ ] Fill out forms — do inputs accept text, show validation?
- [ ] Test navigation — do links and routing work?
- [ ] Check dropdowns, modals, toggles — do they open/close?
- [ ] Verify hover states and focus indicators
- [ ] Test loading states — what shows during async operations?
- [ ] Test error states — what happens on invalid input or failed operations?

### Console & Network
- [ ] Read console messages — any JavaScript errors?
- [ ] Check for unhandled promise rejections
- [ ] Read network requests — any 404s or failed API calls?
- [ ] Look for slow requests (> 3 seconds)
- [ ] Check for deprecation warnings
- [ ] Verify no sensitive data in console output

### Accessibility
- [ ] Take a snapshot (accessibility tree) — check semantic structure
- [ ] Verify heading hierarchy (h1 → h2 → h3, no skips)
- [ ] Check that interactive elements have accessible names
- [ ] Test keyboard navigation (Tab through elements — logical order?)
- [ ] Look for ARIA labels on icons and non-text elements
- [ ] Check color contrast (text should be legible)
- [ ] Verify focus indicators are visible on all interactive elements

## REPORT FORMAT

Structure your findings like this:

## [Aspect Name] — [PASS / ISSUES FOUND]

### Issues
- **[CRITICAL/HIGH/MEDIUM/LOW]** [Description]
  - Steps: [how to reproduce]
  - Expected: [what should happen]
  - Actual: [what happens]

### Passed Checks
- [List what looks good]
```

## Step 9: Collect & Report

Wait for all agents to complete. Aggregate findings into a single report:

```
## UX Check Report

**Pages tested:** [N] | **Aspects covered:** [N] of 5 | **Agents used:** [N]
**Browser:** Playwright MCP / Claude-in-Chrome
**Personas applied:** [list of selected personas]
**Mobile deep dive:** Yes / No

### Results by Page

#### [Page URL]
  ✓ Visual & Layout — PASS (Agent A)
  ✗ Responsive — 1 issue (Agent A)
  ✓ Interactions — PASS (Agent B)
  ✓ Console & Network — PASS (Agent B)
  ✓ Accessibility — PASS (Agent C)

### Issues ([N] total)

1. **[MEDIUM] [Persona]** [Description]
   - Page: [URL]
   - Aspect: [which aspect found it]
   - Steps: [reproduction steps]
   - Expected: [expected behavior]
   - Actual: [actual behavior]

### Summary
[N] pages tested, [N] aspects checked, [N] issues found
```

Deduplicate findings if multiple agents noticed the same issue. Present by severity: CRITICAL > HIGH > MEDIUM > LOW.

If everything passes, say so clearly:
```
## UX Check — All Clear ✓
[N] pages tested, [N] aspects checked, 0 issues found.
```

## HARD RULES

- Detect browser tools FIRST — do not spawn agents without a working browser.
- Each agent MUST create its own tab before doing anything else.
- Spawn ALL agents in ONE message (parallel Task calls).
- Cap at 4 agents maximum to avoid browser contention.
- Minimum 2 agents — if only 1-2 aspects are relevant, combine into 2 agents anyway for depth.
- Do NOT ask "should I continue?" after spawning — always collect and report.
