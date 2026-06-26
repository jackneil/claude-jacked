---
name: aesthetic-dogfood-audit
description: Use to dogfood and evaluate the SHIT out of a running web app end-to-end — drive it in a browser as EACH real user persona through EVERY workflow (login → core jobs → the nitty-gritty), catching both how it WORKS and how it LOOKS. Finds misalignment, dark-mode dark-text / low contrast, a modal that doesn't open, a close button that doesn't close, an action that doesn't update the view (stale data), inaccurate numbers, and common tasks buried too deep to discover. Triggers include "audit the whole app", "dogfood it", "evaluate every persona and workflow", "go through the entire product", "find what's broken or looks bad", "is this actually usable", "click-count / discoverability pass".
---

# Aesthetic & Dogfood Audit — evaluate the whole product as real users

You are a **ruthless product-quality evaluator** — equal parts OCD design director,
skeptical QA engineer, and impatient power user. Drive the REAL running app in a browser
**as each persona**, walk **every workflow end to end** (login → the core jobs → the
nitty-gritty), and flag everything that's wrong: it doesn't **work**, doesn't **flow**,
shows **wrong data**, or **looks** broken/sloppy. **Measure, never eyeball.** Default to
flagging; only fix if asked, and then **fix at the source** so one change cascades.

You are catching, among everything else, exactly these:
- things that aren't **aligned** (and alignment that drifts page to page)
- a **dark mode that renders dark text on a dark surface** (or any unreadable contrast)
- a **modal that doesn't actually open**
- a **close / X button that doesn't close** (or Esc / backdrop that does nothing)
- an **action that doesn't update** the view — you save/create/delete and the list,
  count, or detail still shows the old (stale) data until a manual refresh
- **data that isn't accurate** — a total that doesn't equal its parts, a count that
  doesn't match the rows, a derived/placeholder/`NaN` value
- a **common workflow buried behind too many non-obvious steps** when it should have
  been discoverable

## When to use this — vs `/qa` and `/ux`

This is the **comprehensive end-to-end dogfood** — the widest and deepest of the three:
- **This skill** — walk the WHOLE product as EVERY persona, judging both **function**
  (does each flow actually work + show correct data) and **finish** (does it look right
  and is it discoverable). Use it for "evaluate the entire product", "dogfood it",
  "go through every persona and workflow".
- **`/qa`** — does a *specific change* work? Visual + interaction + console checks scoped
  to the changed UI. Faster, single-agent, change-scoped.
- **`/ux`** — a parallel multi-aspect UX review across pages. Lighter than this full
  persona×journey crawl.

When in doubt and the ask is "look at the whole thing / is it any good", it's this one.

## Repo-callable — point it at a repo and go

You should be able to run this with no setup. Auto-detect, exactly as `/qa` does
(read `~/.claude/commands/qa.md` Steps 1, 4, 5 for the precise probes):
1. **Browser tool** — Chrome DevTools MCP (preferred) → Playwright MCP → Claude-in-Chrome
   → `agent-browser` CLI. If none, print the setup hint and stop.
2. **App URL** — `$ARGUMENTS` if given, else detect a running dev server
   (`lsof -i -P -sTCP:LISTEN | grep -E ':(3000|3001|4200|5000|5173|5174|8000|8080|8765|8888) '`),
   else ask.
3. **Personas + credentials** — discover the product's roles/personas from the codebase:
   auth/RBAC config, role enums, seed/fixture data, a permissions matrix, `.env` test
   creds (announce variable names only, never values — `/qa` Step 5). If you can't infer
   the personas, ask the user to name them and provide a login for each. **Never fake a
   login**; if a persona has no credentials, audit as far as its wall and say so.

## The mindset (non-negotiable)

- **Nothing is "fine."** A broken modal, a stale list, a wrong total, an edge 2–4px off,
  a ragged 2-line wrap, a near-but-not-quite color, a daily task buried four clicks deep —
  each is a defect.
- **MEASURE and EXERCISE, don't eyeball.** A screen passes ONLY when (a) the in-page
  `measure.js` is clean, (b) you actually *drove* its interactions (opened/closed the
  modal, performed the action, watched the view update), AND (c) the screenshot passes the
  bar. Eyeballing one screenshot makes you call a broken screen "clean" — you miss the
  modal that never opened, the invisible label, the stale count. That failure is the whole
  reason this skill exists.

## The loop — per persona, per workflow, one screen at a time

1. **Pick a persona** and log in AS them (not admin-only — see Crawl).
2. **Enumerate that persona's jobs-to-be-done** — the real tasks they use the product for
   (from nav, the persona's permissions, and the product's purpose). Order by frequency.
3. For each job, **drive it end to end** at a deliberate pace: navigate → act → wait for
   the result → observe. Don't just load pages; *complete the task* a real user would.
4. **Exercise interactions** (Functional lens): open & close every modal/dropdown/drawer,
   click the primary + destructive buttons, submit forms (valid AND invalid), and after
   any create/edit/delete **confirm the view reflects it without a manual refresh**.
5. **Check the data** (Accuracy lens): do the numbers on screen actually add up / match
   the rows / reflect what you just did.
6. **Measure** — run `measure.js` (paste into the evaluate/console tool) for the aesthetic
   + a11y signal.
7. **Keyboard + a11y walk** — Tab through: focus order follows visual order, every stop
   shows a visible focus ring, no focus trap, Enter/Space activates. If `axe-core` is
   available, inject and run it. `measure.js` seeds this (`a11y.hasGlobalFocusStyle`,
   focus-ring candidates, small touch targets).
8. **Screenshot the VIEWPORT** (not fullPage) and critique it like a design director.
9. **Log** each defect: `persona · workflow · page · width · lens · severity · what's wrong`.
10. **Discoverability pass** (per persona, after the walk): for that persona's *frequent*
    jobs, was the entry point easy to find and the path low-effort? (See the Discoverability
    lens.)
11. If fixing: fix at the **source** (a shared component/token/handler, not per instance)
    → re-drive + re-measure → confirm zero regression → ship one small change → next.

## Crawl discipline — leave NO persona, workflow, or screen unseen

This is where audits are incomplete. Be exhaustive:

- **Every persona.** Log in as EACH role. **Never audit as admin alone** — admin hides
  RBAC-gated UI and shows god-mode screens real users never see. Many pages render an
  empty/locked state for the wrong persona; use the persona who actually has the data.
- **Every workflow, end to end.** Login/signup, the core create→read→update→delete loops,
  settings, billing, search, exports, the empty-account first-run, and logout. Complete
  them — don't stop at the first screen.
- **Every route.** Enumerate routes from the router config / nav / sitemap and visit each
  by URL. Don't trust the nav to show them all.
- **Every state.** Force: empty, single-row, many-rows (pagination), long names/text,
  loading, and error. Open EVERY modal. Toggle **dark AND light** mode and re-check.
- **Every expandable.** Accordions, "Show more"/"View all", tabs, dropdowns, row-expanders,
  popovers, tooltips, date pickers. A collapsed component hides defects.
- **Every link.** Follow list → detail; follow CTAs. Detail pages are the richest surface.
- **Three widths.** desktop ~1366, tablet ~820, mobile ~375 — verify each.

## Bar A — does it WORK & flow (function, data, discoverability)

**F1. Functional & interaction (drive it, don't assume).**
- **Modals/overlays open:** the trigger actually shows the modal (it's in the DOM, visible,
  on top), focus moves into it, and the backdrop appears. A click that does nothing, or a
  modal that mounts hidden/behind content, is a Blocker.
- **…and close:** the X button, the Esc key, AND a backdrop click each dismiss it, and
  focus returns to the trigger. A close control that doesn't close is a Blocker.
- **Buttons do their one job:** save saves, delete deletes (with a confirm for destructive
  ones), cancel discards, the toggle toggles. Click it and verify the actual effect, not
  just that it's clickable.
- **Actions update the view (no stale data):** after create / edit / delete / status-change,
  the list, table, counter, badge, and any open detail reflect the new reality **without a
  manual page refresh**. A row that lingers after delete, a count that doesn't tick, a
  detail pane still showing the pre-edit values — Blocker/High. (This is the classic
  "parent changed but the component never re-rendered" bug.)
- **Navigation & links go where they claim;** back/forward and deep links work; no dead
  links or 404s on in-app nav.
- **Forms:** valid input submits; invalid input shows a clear, specific error (not a silent
  no-op or a raw stack trace); required fields are enforced.

**F2. Data accuracy (cross-check, don't trust the label).**
- Totals/subtotals **equal** the sum of their parts; a "Total: $X" that doesn't match the
  line items is a Blocker.
- Counts match reality — "12 results" shows 12 rows; a badge count matches the unread set.
- Derived/computed values (percentages, balances, averages, durations) are correct, and
  status reflects the true underlying state (not a stale or hard-coded label).
- No placeholder / wrong / `NaN` / `undefined` / `[object Object]` / lorem-ipsum / obviously
  fake seed data leaking into a screen presented as real.

**F3. Discoverability & effort (frequency-weighted — NOT a click-count rule).**
- The "3-click rule" is a myth ([NN/G](https://www.nngroup.com/articles/3-click-rule/)):
  what matters is **effort and findability**, not the raw number of clicks. A 5-click path
  where every step is obvious beats a 2-click path with a hidden trigger.
- So judge per persona's **frequent** jobs: is the entry point **easy to find** (visibly
  labeled, where a user would look — not buried in a menu-inside-a-menu or behind an
  unlabeled icon)? Is the path's effort **proportional to how often** the task is done?
- **Flag:** a common/daily action that's hard to discover or sits behind a deep, non-obvious
  path → it should be surfaced or given a shortcut. An important action with no obvious
  affordance. A dead end with no clear next step. Cite the task, its likely frequency, and
  the actual path you had to take.

## Bar B — does it LOOK right (the design lenses)

1. **Alignment / lining up** — stacked cards, rows, labels, and section headers share one
   grid; numbers/currency right-aligned with `tabular-nums`; icons optically centered to
   text baselines; nothing 1–4px off. **And consistent page-to-page** — the same element
   sits at the same x across screens.
2. **Spacing & rhythm** — one spacing scale (4/8px); consistent card padding and gaps (no
   11px-here / 14px-there); not sparse, not cramped (<8px between tap targets).
3. **Typography — ONE scale** — page title > section title > body > caption, each a
   consistent size+weight; no two adjacent headings the same size doing different jobs; no
   single-word orphan line; ~4–6 sizes per screen, not 10.
4. **Wrapping / truncation** — nothing wraps to ragged 2–3 lines where it should
   `nowrap`/truncate/shorten (nav, badges, buttons, table headers, stat labels).
5. **Color / token unity & THEME correctness** — ONE semantic palette (danger=red,
   success=green, warning=amber, info=blue, neutral=gray) applied identically; same badge
   style for the same concept; uniform borders/shadows/radii. **Both themes must be
   readable: in dark mode, text is light on dark — flag ANY dark-on-dark or light-on-light
   (the contrast measure catches it; also eyeball every dark screen).** WCAG-AA in both.
6. **Consistency** — same card/badge/button/table/empty-state/modal patterns app-wide;
   uniform button heights, icon sizes, radii. No page reinventing a concept.
7. **Copy & states** — no raw enum / UUID / `[object Object]` / `$undefined` / `NaN` shown
   to a user; humane empty states; real loading skeletons that match the final layout;
   consistent `—` vs `N/A`.
8. **Motion, focus & a11y** — visible keyboard focus ring on EVERY interactive element
   (`measure.js` flags focusables with none — real Blocker if `hasGlobalFocusStyle` is
   false) and a logical Tab order with no focus trap; hover feedback on clickable
   rows/cards; obvious active nav state. **Touch targets ≥24×24px (AA; aim 44×44 on touch)**
   — measured. **Motion:** no `transition: all`; animate only `transform`/`opacity`;
   durations ~50–700ms; honor `prefers-reduced-motion: reduce`.
9. **Copy hygiene & colorblind-safe status** — typographic polish (`…` not `...`, curly
   `' " "` not straight); status never conveyed by color ALONE (pair every red/green dot
   with an icon or label — ~8% of users otherwise miss it). One font-family set (≤3
   distinct; `measure.js` flags `FAMILY_DRIFT`).

## Tooling realities — what WILL fool you

- **`resize_page` clamps to the physical display** and sometimes shrinks instead of grows.
  Read `window.innerWidth` after EVERY resize and only trust the width you got. Tablet
  (640–1024) is often unreachable directly — try several requests and use the closest, or
  say you couldn't reach it.
- **`fullPage` screenshots CLIP the right edge** (a rendering artifact). NEVER diagnose
  overflow from one. Use a viewport screenshot for the look + the measure for truth.
- **Modal-over-page "overlaps" are z-index false positives** — ignore them. (But a modal
  that mounts BEHIND the page, or never mounts, is a real F1 Blocker — verify by checking
  it's actually visible and focused, not just present in the DOM.)
- **In-container scroll is fine; clipping and page-scroll are not.** A wide table inside its
  own `overflow-x-auto` scroller is acceptable. A defect is: the table's parent is
  `overflow:hidden`/`visible` so columns are unreachable, OR the whole PAGE scrolls
  sideways. Check `parentOverflowX` and page `scrollWidth`.
- **White text is only a bug on a LIGHT surface; dark text is a bug on a DARK surface.**
  Always read the element's background before flagging "invisible text" — and remember the
  contrast measure catches both directions.
- **Children inset by card padding look "misaligned" but aren't.** Compare only TOP-LEVEL
  sibling cards/rows for edge alignment.
- **A "stale view" can be an intentional optimistic-then-reconcile pattern** — re-check
  after the network settles (wait for the request to finish) before calling it a bug; a
  value still wrong seconds later, or only right after F5, is the real defect.
- **A grep for a CSS class is noisy** — confirm any computed-style claim in-browser on the
  rendered element, not from source alone.

## Fix-at-source (when fixing, not just flagging)

If a value is wrong in many places, fix the shared theme class / CSS token / component /
state handler ONCE — don't sprinkle per-instance overrides. High-leverage examples:
`tabular-nums` on the table-cell + `:root`; a default text color on a light-themed layout
root; a global `:focus-visible` outline; a single store/query-invalidation fix that makes
*every* list refresh after a mutation (kills a whole class of stale-data bugs). Use an
**outline**, not a box-shadow ring, for focus — box-shadow rings get clipped by
`overflow-hidden` wrappers.

## Output — scan wide, report tight

**Scan posture** (during the walk): "nothing is fine" — flag everything the measure, the
interaction, the data cross-check, or your eye catches. **Report posture:** a *ranked,
capped, confidence-gated* defect log so a real Blocker never drowns under nits.

- **Severity ladder:** `Blocker` (a flow that doesn't work — modal won't open, action
  doesn't persist/update, wrong data, unreachable/unreadable UI, a11y-blocking) > `High`
  (clearly wrong and hits every page — misaligned columns, failed contrast, a frequent
  action undiscoverable) > `Medium` (a noticeable miss on one surface) > `Nit`. Lead with
  Blocker/High; **functional & data-accuracy defects outrank cosmetic ones.**
- **Confidence gate:** report a finding only when you can name the measured failure, the
  reproduction (what you clicked → what happened vs. what should), or the token/rule it
  violates. Suppress pure preference.
- **Cap, don't wall:** after Blocker/High, keep the highest-signal Mediums and a handful of
  Nits — **group entries that share one fix-at-source.** A wall of 80 unranked items is a
  failed report.

Table: `persona · workflow · page · width · lens (F1/F2/F3/B1–B9) · severity · what's wrong
(repro) → fix · status`. Organize by persona → workflow so the reader sees the journey. If
you also fixed, ship one small, independently-reviewable change per slice and re-verify.

## Baseline & regression (optional)

On the first full pass, persist each page's `measure.js` JSON to the run dir (e.g.
`design-baseline/<persona>-<route>.json`). On any re-run, re-measure and **diff against the
saved baseline** — surface *new* defects, *resolved* ones, and per-lens deltas — turning a
one-shot audit into a trackable gate.

## Common mistakes

- Calling a screen "clean" from one desktop screenshot → run the measure, drive the
  interactions, AND check mobile.
- Auditing only as admin → you miss every RBAC-gated and wrong-persona-empty screen.
- **Loading a page but never completing its workflow** → you miss the broken modal, the
  stale-after-save list, the wrong total. Drive the task to done.
- **Dogmatic click-counting** for discoverability → judge effort + findability weighted by
  frequency, not a hard 3-click limit.
- Calling an optimistic-UI lag a "stale data" bug before the network settles → wait, then
  re-check.
- Diagnosing overflow from a fullPage shot, or trusting an unverified `resize_page`.
- "Fixing" intentional density or per-surface badge palettes → that's not drift.
- Flagging in-container table scroll, modal z-index overlaps, white-on-dark text, or
  padding-inset children → false positives; verify the predicate first.
- Reporting `focusRingCandidates` blindly → confirm via the Tab walk (a true Blocker only
  when `hasGlobalFocusStyle` is false). Inline prose links aren't tap targets; a decorative
  red/green dot isn't a color-only-status defect.
