---
description: Use when UI changes span multiple components or pages, layout or navigation changed, a new user-facing feature was added, or comprehensive UX validation is needed.
---

## Config Override

If this command was invoked via a local config wrapper (you see a `## Repo Config` section earlier in the prompt), use that config to skip detection:
- **Browser Tool** specified? → Skip Step 1, use the declared tool directly (fall back to detection if unavailable)
- **Stack** declared? → Skip tech stack inference in Step 2
- **Dev Server Port** specified? → Use it in Step 3 URL construction (still check `lsof` as fallback)
- **`## Dev Servers`** table present (multi-server / monorepo repo)? → In Step 6, bring up ALL listed servers (a frontend pointed at a dead backend fails silently) before testing — this supersedes a single `Dev Server Port`
- **Component Paths** listed? → Use as focus hints for which pages to prioritize in Step 3. These may be per-app (e.g. ``apps/desktop/src/...` (desktop)``) when the repo has `apps/*/` — treat each app's paths as scoping hints for the app that owns the changed file
- **Credential Hints** listed? → Use those variable names in Step 4 credential search
- **UX Focus Areas** listed? → Emphasize those aspects in your test plan across all agents

If the config overlay date is more than 90 days old, mention: "Your `/ux` config is over 90 days old — consider running `/jacked-setup ux` to refresh it."

If no `## Repo Config` section is present, run all detection steps normally.

You are the UX Check Dispatcher. You spawn parallel browser-testing agents, each focused on specific UX aspects on specific pages, to validate UI changes fast. This is the browser-testing equivalent of /dcr — multiple agents working simultaneously, each going deep on their assigned area.

> **Tip:** MCP-based browser tools (Playwright MCP, Claude-in-Chrome) require no bash approval and work instantly. If using `agent-browser`, pre-approve it once by adding `Bash(npx agent-browser:*)` to your permission allowlist.

## UX CHECK ASPECTS (7 total)

| # | Aspect | What to Check |
|---|--------|---------------|
| 1 | **Visual & Layout** | Broken layouts, overlapping elements, spacing, alignment, text readability, color consistency, images/icons loading |
| 2 | **Responsive** | Mobile (375px), tablet (768px), desktop (1280px+) — layout integrity, touch targets, text wrapping, no horizontal scroll |
| 3 | **Interactions** | Buttons, forms, navigation, modals, dropdowns, toggles, hover states, loading states, error feedback |
| 4 | **Console & Network** | JS errors, unhandled rejections, 404s, failed API calls, slow requests, deprecation warnings, visible-text grammar/clarity/jargon |
| 5 | **Accessibility** | Semantic HTML, focus order, keyboard navigation, WCAG 2.1 AA contrast (4.5:1 body / 3:1 large), Enter/Space activation, visible focus, ARIA labels, landmarks |
| 6 | **Discoverability** | New features only: entry points from related pages, navigation depth to reach feature, first-use clarity, return navigation back to origin |
| 7 | **Robustness & States** | Content-overflow stress (long strings, many rows, narrow columns), invalid-input form validation, distinct loading / empty / error states |

## Step 1: Detect Browser Tools

Check which browser automation tools are available. Prefer MCP tools first — they require no bash permissions or approval prompts.

**Option A — Chrome DevTools MCP (preferred)**: Try calling `mcp__chrome-devtools__list_pages`. If it works, set `browser = "chrome-devtools"`. Use these tools:
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

**Option B — Playwright MCP**: Try using `mcp__plugin_playwright_playwright__browser_snapshot`. If it works, set `browser = "playwright"`. Note to user:
> Using Playwright MCP (Chrome DevTools MCP is preferred — see above). Playwright opens separate browser windows.

**Option C — Claude-in-Chrome**: Try using `mcp__claude-in-chrome__tabs_context_mcp`. If it works, set `browser = "chrome"`.

**Option D — agent-browser CLI**: Run `npx agent-browser --version` via Bash. If it succeeds, set `browser = "agent-browser"`. This reuses your existing browser session — no new windows.
> Note: `npx` requires an approval prompt unless pre-approved. Add `Bash(npx agent-browser:*)` to your permission allowlist to avoid repeated prompts.

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

Run `git diff --name-only HEAD` to find UI-relevant files:
- `.js`, `.jsx`, `.ts`, `.tsx`, `.css`, `.scss`, `.less`, `.html`
- `.vue`, `.svelte`, `.erb`, `.jinja`, `.jinja2`
- Ignore: `node_modules/`, `dist/`, `build/`, `__pycache__/`, test files (`*.test.*`, `*.spec.*`)

Map changed files to affected pages/flows using file paths and conversation context.

If no UI files changed, tell the user and ask if they still want to proceed.

### Detect New Features (Discoverability trigger)

Also run both commands — the first catches staged new files, the second catches untracked (created but not yet staged) new files:
```bash
git diff --name-status HEAD | grep "^A" | grep -iE '\.(js|jsx|ts|tsx|css|scss|html|vue|svelte)$'
git ls-files --others --exclude-standard 2>/dev/null | grep -iE '\.(js|jsx|ts|tsx|css|scss|html|vue|svelte)$'
```

**Filter out utility/infrastructure files** before concluding a new feature exists. Files where the path contains `utils/`, `helpers/`, `hooks/`, `lib/`, `shared/`, `services/`, or where the filename matches `*.util.*`, `*.helper.*`, `*.service.*`, `*.store.*` are likely support code — not new navigable features. These do NOT trigger `new_feature_detected`.

If any remaining UI files appear (pages, views, components in feature directories) → set `new_feature_detected = true`.

When true, use the file paths and conversation context to:
- Describe what the new feature IS (e.g., "new Account Details page at `/accounts/:id`")
- Identify which **existing pages** a user would logically navigate FROM to reach it (e.g., Accounts list, Dashboard, any page that references accounts)
- Store these as **entry point pages** — the Discoverability agent will check them

**If entry points cannot be identified** from file paths or conversation context, default to checking the app's root/home page and any top-level navigation section most semantically related to the new feature (e.g., for an "Account Details" page, default entry points = Accounts list + Dashboard).

**$ARGUMENTS override:** If `$ARGUMENTS` contains `discoverability` or `disc`, force `new_feature_detected = true` even if git detection found no new files. Useful when testing a feature added in a previous session.

Announce:
```
**New feature detected:** [description]
**Expected entry points:** [list of existing pages that should link to the new feature]
**Discoverability check:** ENABLED
```

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

### Monorepo Scoping (apps/*/ and packages/* layouts)

In a monorepo (`apps/*/` or `packages/*/` layout), scope Tier classification to the app that OWNS the changed file — the nearest `apps/*/` boundary above it. A `shared/` (or Tier-1) change under `apps/web/` is Tier 1 for **`apps/web` only**, not every app in the repo — don't fan a web-scoped change out across `apps/admin`, `apps/desktop`, etc.

A change under `packages/*` (a shared library consumed by multiple apps) is the exception: enumerate the apps that actually import/consume that package and test EACH consuming app's own URL — not just one. Use each app's per-app **Component Paths** (from the config or `apps/*/` structure) to map the change to the right app URL.

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

Select 2-3 personas that match the project's target users. These personas shape HOW agents evaluate their assigned aspects — they don't replace the 7 aspects, they add evaluative bias.

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

**Isolate → PROVE it → only THEN drive every flow fully. Fail closed.** This review clicks
through pages and exercises interactions across personas, including writes (submit/save/delete).
**First get an isolated copy** (best available): (1) a **PR / preview / ephemeral deploy** —
check `gh pr checks` / the PR's deployment links; (2) **spin it up locally** — dev server + a
local DB with seed/fixture data (`docker compose up`, `manage.py runserver`, `npm run dev`/`pnpm
dev`, a `seed`/`migrate` command, `.env.local`); (3) a disposable staging. **Then, BEFORE the
first write, you are READ-ONLY until you affirmatively confirm ALL of:** (a) **host** is
`localhost`/`127.0.0.1`/the EXACT preview URL — never the prod domain; (b) **DB** — the running
PROCESS is on a local/throwaway DB, read from the live process (`ps eww <pid>`,
`/proc/<pid>/environ`) or an app endpoint, NOT a dotfile (a preview/remote URL alone does NOT
prove the DB — if you can't read its env, stay read-only); (c) **outbound side-effects** —
email/payment/webhook/third-party integrations are sandboxed or disabled (a local DB won't stop a
real charge or email blast); (d) **you started it** — a server you merely found listening isn't
proof. ANY doubt → it's production, stay read-only. **Only once ALL pass: exercise every flow to
completion including the destructive ones** — that's the payoff of isolating. Forced onto
production? Stay READ-ONLY and say the interactive checks need an isolated instance. **This gate
governs EVERY write in this command** — login, form submits, create/edit/delete across personas,
and any re-run; you are READ-ONLY at each such step until ALL of (a)–(d) pass.

**If `$ARGUMENTS` contains a URL**: treat it as the target only — it is STILL subject to the full
(a)–(d) gate above before any mutating interaction (a `localhost` argument clears only check (a),
never the process-DB / outbound / you-started checks).

**Otherwise**, detect the dev server(s) in this order:
1. **`## Dev Servers` table declared** (multi-server / monorepo)? → ensure EVERY listed server is up before testing — a frontend whose backend API is dead is the #1 real "looks broken" failure. Start any that aren't listening (honoring each app's "server may be user-managed, don't kill it" note), and respect the (a)–(d) isolation gate above for any server you start. Map each changed file to the app that owns it (nearest `apps/*/` boundary) so agents test on the right app URL.
2. **Single `Dev Server Port` set** (config or context)? → use it.
3. Check conversation context for recently mentioned URLs
4. Run `lsof -i -P -sTCP:LISTEN | grep -E ':(1420|1421|3000|3001|4000|4173|4200|4321|5000|5173|5174|6006|8000|8001|8080|8765|8888) '`

If found, use it. If multiple, ask the user. If none, ask for the URL.

## Step 7: Check for Login Credentials

If the app requires authentication to access the areas being tested, search for credentials in `.env` files before asking the user.

**Find the repo root**:
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

**Screenshot setup** (agent-browser and Playwright only — Chrome does not support file-based screenshots):
```bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
rm -rf "$REPO_ROOT/tmp/ux_screenshots"
mkdir -p "$REPO_ROOT/tmp/ux_screenshots"
```
Save all screenshots to `$REPO_ROOT/tmp/ux_screenshots/<agent-letter>-<descriptive-name>.png`.
*Add `tmp/` to your project's `.gitignore` if it isn't already there.*

Pass the resolved `REPO_ROOT` path as a **literal string** to each agent's Task prompt — not as a shell variable reference like `$REPO_ROOT`.

## Step 8: Build Test Matrix

Select which aspects are relevant based on what changed:
- CSS/styling changes → weight **Visual & Layout** + **Responsive**
- JS logic/component changes → weight **Interactions** + **Console & Network**
- JS logic or form/input changes (forms, lists, tables, data rendering) → weight **Robustness & States**
- HTML structure changes → weight **Accessibility** + **Visual & Layout**
- New UI files added (`new_feature_detected = true`) → include **Discoverability** — assign the entry point pages list to the agent handling this aspect
- When in doubt, include the aspect.

Build a matrix of (aspect, page) and assign to 2-4 agents:

**1 page (modified only):**
- Agent A: Visual & Layout + Responsive
- Agent B: Interactions + Console & Network
- Agent C: Accessibility + Robustness & States

**1 page (new feature — new_feature_detected = true):**
- Agent A: Visual & Layout + Responsive
- Agent B: Interactions + Console & Network + Robustness & States
- Agent C: Accessibility + Discoverability

**2 pages (modified only):**
- Agent A: Page 1 — Visual & Layout + Responsive
- Agent B: Page 1 — Interactions + Console & Network + Robustness & States
- Agent C: Page 2 — Visual & Layout + Responsive
- Agent D: Page 2 — Interactions + Console & Network + Robustness & States

**2 pages (new feature — new_feature_detected = true):**
- Agent A: Page 1 — Visual & Layout + Responsive
- Agent B: Page 1 — Interactions + Console & Network + Robustness & States
- Agent C: Page 2 — Visual & Layout + Accessibility
- Agent D: Page 2 — Interactions + Discoverability + Robustness & States (receives the entry point pages list)

**Robustness & States placement:** by default the agent that owns **Interactions** also owns **Robustness & States** (forms and states are tested together). When that agent is already carrying three aspects, hand Robustness to the lightest agent on the same page instead. At the 4-agent cap, never drop it — fold it into the Interactions agent.

**3+ pages:** Group intelligently, cap at 4 agents. Prioritize pages most affected by changes.

**Tiered dispatch (Fable-class session: any session model above Opus):** pass the model explicitly on every agent spawn. Agents whose assignment includes **Visual & Layout** (or any design-quality judgment - alignment, spacing, visual hierarchy, "does this look designed") dispatch on `model: "fable"`: aesthetic judgment is a lane that stays on the top model. All other agents (Interactions, Console & Network, Robustness & States, Responsive mechanics, Accessibility, Discoverability) dispatch on `model: "opus"` - they are volume work, and the full 4-agent width applies. Group aspects so Visual & Layout concentrates on as FEW agents as possible (ideally one per run) so the Fable spend stays small. On Opus and below, spawn with the session's model (never below Opus). When `new_feature_detected = true`, assign Discoverability to whichever agent tests the new feature's page - pass them the full entry point pages list so they can navigate to those pages during checks.

Use your judgment — adjust grouping based on what changed. Skip aspects that clearly don't apply (e.g., skip Responsive for a purely server-rendered admin page that's never used on mobile).

**Announce the matrix:**
```
**UX Check Matrix — [N] aspects across [M] agents**
**Personas:** [Persona 1] + [Persona 2] (inferred: [classification])
**Mobile deep dive:** Yes ([signal]) / No ([reason])
- Agent A: [Page URL] — Visual & Layout + Responsive
- Agent B: [Page URL] — Interactions + Console & Network + Robustness & States
- Agent C: [Page URL] — Accessibility
```

## Step 9: Spawn Parallel Agents

**Before spawning ANY agent, the Step 6 (a)–(d) isolation gate must be cleared for the target —
you (the dispatcher), not the sub-agents, perform that verification once.** The sub-agents do the
actual clicking/filling, so they cannot re-verify isolation; you must hand them the verdict.
Substitute the result into the `## ISOLATION` block of every agent prompt: **ISOLATED** only if
all four checks passed, otherwise **READ-ONLY**. If you could not clear the gate, every agent is
READ-ONLY — no exceptions.

Spawn ALL agents in ONE message using parallel Task tool calls. Each agent is `subagent_type: "general-purpose"`.

### Agent Prompt Template

Include ALL of the following in each agent's Task prompt:

```
You are a UX tester performing focused browser-based checks.

## ISOLATION (read FIRST — non-negotiable): READ-ONLY — production or unproven
(The lead REPLACES the line above with `ISOLATED — writes allowed` ONLY on a clean four-check pass. If it still says READ-ONLY — including if the lead forgot to fill it in — you ARE read-only.)
- READ-ONLY → navigate + observe ONLY. Do NOT submit/save/delete, click destructive buttons, or
  log in (auth can create a session or fire a real notification). **Page-LOAD is itself potentially
  effectful:** do NOT navigate to surfaces with known on-load side-effects (a GET that
  marks-as-read / confirms / unsubscribes, or fires a notification / webhook / email / audit-write
  on view) — describe them instead. The only floor is benign third-party analytics that writes no
  app state. Report what you would test and that it needs an isolated instance.
- ISOLATED → you may exercise writes and destructive flows freely, but ONLY against the exact
  target URL the lead cleared — never any other host. When unsure, treat as READ-ONLY.

## BROWSER TOOL: [chrome-devtools / agent-browser / chrome / playwright]

[If chrome-devtools]:
Use tools prefixed with `mcp__chrome-devtools__*`:
- `navigate_page` — open pages (use page selector or create new page)
- `take_snapshot` — accessibility tree with refs (preferred for element detection)
- `take_screenshot` — visual screenshot
- `click` — click element by ref from snapshot
- `fill` — fill input fields
- `evaluate_script` — run JavaScript on page
- `emulate` — change viewport size (mobile/tablet testing)
- `list_console_messages` — check for JS errors
- `list_network_requests` — check for failed requests
- `new_page` — create a new tab

[If agent-browser]:
Use the Bash tool with `npx agent-browser` commands:
- `npx agent-browser open <url>` — navigate to a URL
- `npx agent-browser snapshot` — accessibility tree with refs (preferred for element detection)
- `npx agent-browser screenshot <path>` — save screenshot to file
- `npx agent-browser click <ref>` — click element by @ref from snapshot
- `npx agent-browser type <ref> <text>` — type text into element
- `npx agent-browser fill <ref> <text>` — clear and fill element
- `npx agent-browser eval <js>` — run JavaScript on page
- `npx agent-browser scroll <dir> [px]` — scroll up/down/left/right
- `npx agent-browser wait <sel|ms>` — wait for element or time

Note: agent-browser reuses your existing browser — no tab isolation needed.

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

## SCREENSHOT PATH (agent-browser and Playwright only)

Save screenshots to: `[REPO_ROOT]/tmp/ux_screenshots/[agent-letter]-[description].png`
Example: `/Users/jack.neil/myproject/tmp/ux_screenshots/A-homepage-mobile.png`

**Chrome only:** `claude-in-chrome` does not support saving screenshots to files. Skip file-based screenshot saving — use accessibility tree snapshots instead.

## CRITICAL: TAB ISOLATION (Chrome DevTools/Chrome/Playwright only — not needed for agent-browser)

Before ANY browser action, create your own tab:
- [Chrome DevTools]: `new_page`, then use `select_page` to target your tab for all subsequent calls
- [Chrome]: `tabs_create_mcp`, then use ONLY the returned tab ID for ALL subsequent calls
- [Playwright]: `browser_tabs` action "new", then navigate in the new tab

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

### Task traversal (think-aloud)

Static aspect scoring isn't enough. For EACH selected persona, pick one realistic goal that persona would have on your assigned page (e.g., Novice: "create my first record"; Power User: "filter and bulk-edit"; Design Consultant: "scan the page for hierarchy and polish"). Actually attempt to complete that goal end-to-end in the browser. As you go, "think aloud": note every point of friction, hesitation, or dead end — where you weren't sure what to click, where a label was ambiguous, where the flow stalled. Report those friction points as findings (tagged with the persona), in addition to the static checklist results. This is where the highest-signal usability issues surface. **READ-ONLY verdict?** Pick a goal reachable by navigation/observation alone, and stop at the first step that would submit/save/create/delete or require login — narrate what you'd do for the rest. Do NOT pick a mutating goal (e.g. "create my first record", "bulk-edit") on a read-only/unproven target.

## ASPECT CHECKLISTS

> **If your ISOLATION verdict is READ-ONLY, every step ANYWHERE in this prompt — the checklists
> below AND the Task-traversal goal above — that would submit, save, delete, log in, or otherwise
> change server state / fire a side-effect is OBSERVE-ONLY** — locate and describe the control and
> what you'd expect, but do NOT activate it. The "click / fill / submit every form" and
> "complete the goal end-to-end" imperatives apply in full ONLY when your verdict is ISOLATED.

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

**Heuristic lens (Nielsen) — ask these while testing, flag misses as usability issues:**
- Is system status visible during async actions (spinner, progress, disabled state), or does the UI look frozen?
- Is there an escape hatch / undo for destructive or multi-step actions (cancel, confirm, undo)?
- Are labels jargon-free and matched to the real world, or do they assume internal terminology?
- Is the interaction consistent with the rest of the app (same patterns, wording, placement)?
- When an error occurs, can the user understand and recover from it (plain-language message + a way forward)?

### Console & Network
- [ ] Read console messages — any JavaScript errors?
- [ ] Check for unhandled promise rejections
- [ ] Read network requests — any 404s or failed API calls?
- [ ] Look for slow requests (> 3 seconds)
- [ ] Check for deprecation warnings
- [ ] Verify no sensitive data in console output

**Content & microcopy** (cheap to catch in the browser, high-impact for novice users):
- [ ] Read the visible text — any grammar, spelling, or clarity problems?
- [ ] Flag jargon or internal terminology that a real user wouldn't recognize
- [ ] Flag mislabeled or ambiguous buttons and headings (does the label match what the control actually does?)

### Robustness & States (forms, lists, and async states)
- [ ] Content overflow — paste a very long string into text fields and headings; load (or simulate) many list/table rows; check narrow columns. Does anything clip, push the layout, or scroll horizontally?
- [ ] Invalid-input validation — submit every form with empty, malformed, and out-of-range values. Is there a clear, specific validation message for each, and is submission blocked?
- [ ] Loading state — is there a distinct loading indicator during async work (not a frozen or blank screen)?
- [ ] Empty state — when there's no data yet, is there a helpful message and a clear next action (not a bare empty container)?
- [ ] Error state — when an operation fails, is there a distinct, recoverable error screen (not a silent failure or raw stack trace)?

### Accessibility
- [ ] Take a snapshot (accessibility tree) — check semantic structure
- [ ] Verify heading hierarchy (h1 → h2 → h3, no skips)
- [ ] Check that interactive elements have accessible names
- [ ] Test keyboard navigation (Tab through elements — logical order?)
- [ ] Look for ARIA labels on icons and non-text elements
- [ ] Check color contrast against WCAG 2.1 AA: >= 4.5:1 for body text, >= 3:1 for large text (>=18pt or >=14pt bold). Compute via DevTools/accessibility panel — do not eyeball it.
- [ ] Verify focused controls activate on Enter and Space (buttons, links, custom widgets)
- [ ] Confirm a visible focus state on EVERY interactive element (no `outline: none` with no replacement)

### Discoverability (include ONLY if new_feature_detected = true)

**New feature:** [description — e.g., "Account Details page at /accounts/:id"]
**Expected entry points:** [list from Step 2 analysis — existing pages where users would look for this]

**Heuristic lens (Nielsen) — apply while walking the path in:**
- Match real world: is the feature's name/label something a user would recognize and search for, or internal jargon?
- Recognition over recall: can the user *see* the way in (a visible link/affordance), or must they remember a route?
- Consistency: does the entry point follow the same navigation patterns as the rest of the app?
- Help & recovery: if the user lands somewhere wrong, is there an obvious way back or forward?

#### Entry Point Checks
For each expected entry point page:
- [ ] Navigate to the entry point page. If the page returns a 404, requires auth you don't have, or fails to load — flag as **[MEDIUM] Entry point unreachable during test** and skip to the next entry point. Do not abort the entire Discoverability check.
- [ ] Look for a link, button, menu item, card, or nav entry pointing to the new feature
- [ ] If present: is it visible without scrolling? Is the label clear and self-explanatory?
- [ ] If absent: flag severity based on the entry point's importance:
  - **[HIGH]** if this is the primary or only natural navigation path to the feature
  - **[MEDIUM]** if other entry points exist and this is a secondary path
  - No flag if the missing link is genuinely optional (e.g., a "related" shortcut)

#### Navigation Flow In
- [ ] Follow the most natural path from the primary entry point to the new feature
- [ ] Count the steps/clicks required — 1-2 clicks = good, 3+ for a primary feature = **[MEDIUM]**
- [ ] Is each step's next action obvious? (clear labels, visible affordances, no dead ends)
- [ ] Are there breadcrumbs or "you are here" indicators along the path?

#### First-Use Experience
- [ ] Does the new feature page make its purpose immediately clear without reading docs?
- [ ] Empty state (no data yet): is there a helpful message with a clear call to action?
- [ ] Are controls, buttons, and form fields labeled clearly for a first-time user?
- [ ] Is there any tooltip, hint text, or contextual help to guide initial use?

#### Return Navigation
- [ ] Is there a visible Back button, breadcrumb, or close affordance?
- [ ] Does the browser Back button work correctly (no broken history stack)?
- [ ] After completing the primary action, where does the user land? Is it the right place?
- [ ] Are there cross-links to related features that naturally follow from using this one?

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

## COMMUNICATION PRINCIPLES

How you WRITE findings matters as much as what you find:

1. **Problems over prescriptions.** Describe the problem and its impact on the user — do NOT prescribe the CSS/code fix. This is a read-only diagnostic; the fix is decided downstream.
   - Good: "Spacing is inconsistent with adjacent elements, creating visual clutter that makes the form harder to scan."
   - Bad: "Change the margin to 16px." (prescribes a fix you aren't allowed to make and may be wrong)
2. **Lead with what works.** Open your report with one honest line acknowledging what's working well before listing issues. It calibrates severity and keeps the review constructive — but never invent praise; if a screen is genuinely broken, say so.

## REPORT FORMAT

Structure your findings like this:

## [Aspect Name] — [PASS / ISSUES FOUND]

**What works:** [one line on what's solid here — the strong parts of this aspect]

### Issues
- **[CRITICAL/HIGH/MEDIUM/LOW]** [Problem + its user impact — not a prescribed fix]
  - Steps: [how to reproduce]
  - Expected: [what should happen]
  - Actual: [what happens]

### Passed Checks
- [List what looks good]
```

## Step 10: Collect & Report

Wait for all agents to complete. Aggregate findings into a single report:

```
## UX Check Report

**Pages tested:** [N] | **Aspects covered:** [N] of 7 | **Agents used:** [N]
**Browser:** Playwright MCP / Claude-in-Chrome
**Personas applied:** [list of selected personas]
**Mobile deep dive:** Yes / No

**What works well:** [one honest line synthesizing the strongest parts across agents — lead here before the issues. Don't invent praise.]

### Results by Page

#### [Page URL]
  ✓ Visual & Layout — PASS (Agent A)
  ✗ Responsive — 1 issue (Agent A)
  ✓ Interactions — PASS (Agent B)
  ✓ Console & Network — PASS (Agent B)
  ✓ Robustness & States — PASS (Agent B)
  ✓ Accessibility — PASS (Agent C)
  ✓ Discoverability — PASS / N/A (Agent C)

### Issues ([N] total)

1. **[MEDIUM] [Persona]** [Problem + its user impact — describe the issue, not a prescribed fix]
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

### Cleanup

After presenting the report, remove the screenshot directory:
```bash
rm -rf "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/tmp/ux_screenshots"
```

This command is **read-only** — it detects and reports issues but does NOT fix them. The detailed issue list is returned to the parent caller (Claude Code), which should then use `superpowers:writing-plans` to build a fix plan from the findings, let the user iterate on it, and execute with `/dcr` verification.

## HARD RULES

- Detect browser tools FIRST — do not spawn agents without a working browser.
- Each agent MUST create its own tab before doing anything else.
- Spawn ALL agents in ONE message (parallel Task calls).
- Cap at 4 agents maximum to avoid browser contention. **Tiered dispatch:** on a Fable-class session, pass the model explicitly on every spawn - `model: "fable"` only for the agent(s) carrying Visual & Layout / design-quality judgment (concentrate those on one agent when possible), `model: "opus"` for everything else at full width.
- Minimum 2 agents — if only 1-2 aspects are relevant, combine into 2 agents anyway for depth.
- Do NOT ask "should I continue?" after spawning — always collect and report.
- This command is READ-ONLY — detect and report only. Do NOT fix code or invoke /dcr.
- Each /ux invocation is independent — do not reference previous runs.
