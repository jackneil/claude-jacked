---
name: aesthetic-dogfood-audit
description: Use when asked to dogfood, design-review, or do an OCD / fit-and-finish / "go through every page" aesthetic pass over a running web app — driving the real UI in a browser, walking every route, link, expandable, persona, and state to find UI that is misaligned, sloppy, inconsistent, low-contrast, or "looks stupid". Triggers include "audit the whole app", "find what looks bad", "screenshot every screen", "be a nitpicky designer", "clean up the layout everywhere".
---

# Aesthetic Dogfood Audit

You are an **OCD design director**. Drive the REAL running app in a browser, walk
EVERYTHING — every route, link, click, expandable, persona, and state — screenshot
it, and flag every layout/aesthetic defect. **Measure, never eyeball.** Default to
flagging; only fix if asked, and then **fix at the source** so one change cascades.

## When to use this — vs `/qa` and `/ux`

Three browser walkthroughs exist; they answer different questions:

- **This skill** — the *pixel-level fit-and-finish* pass: "go through every page and
  make it look right." Measure-driven, exhaustive crawl, design-director eye for
  alignment/spacing/type/contrast/consistency. Reach for it on "audit the whole app",
  "find what looks bad", "be a nitpicky designer".
- **`/qa`** — does a *specific change work*? Visual correctness, interactions, console
  errors scoped to the changed UI. Faster, single-agent, change-scoped.
- **`/ux`** — is the app *usable*? Parallel UX review across pages/flows.

If the ask is "did my change break," that's `/qa`/`/ux`, not this.

## The mindset (non-negotiable)

- **Nothing is "fine."** Every edge 2–4px off its neighbor, every ragged 2-line wrap
  where one line fits, every clashing or near-but-not-quite color, every lonely value
  in a huge empty card, every number that doesn't line up in its column — is a defect.
- **MEASURE, don't eyeball.** A screen passes ONLY when the in-page measure is clean
  AND the screenshot passes the bar. Eyeballing a single screenshot will make you call
  a broken screen "clean" (you will miss a floating element, an invisible label, a
  hidden column). That failure is the whole reason this skill exists.

## The loop — one screen at a time

1. **Drive** the real workflow, logged in **AS the target persona** (see Crawl).
2. **Crawl exhaustively** — expand and click everything (see Crawl discipline).
3. **Measure** — run `measure.js` (paste into the browser's evaluate/console tool).
4. **Keyboard + a11y walk** — Tab through the page, capturing `document.activeElement` at
   each stop: focus order must follow visual order, every stop must show a visible focus
   ring, there must be no focus trap (Tab eventually loops/leaves), and Enter/Space must
   activate the focused control. If `axe-core` is available, inject it and run it against
   the live DOM; fold violations into the log. `measure.js` seeds this with
   `a11y.hasGlobalFocusStyle`, focus-ring candidates, and small touch targets.
5. **Screenshot the VIEWPORT** (not fullPage) and critique it like a design director.
6. **Log** each defect: `page · width · lens · severity · what's wrong`.
7. If fixing: fix at the **source** (a shared theme class / token / component, not per
   instance) → re-measure + re-screenshot at the affected widths → confirm zero
   regression → ship a small focused change → next screen.

## Crawl discipline — leave NO screen unseen

This is where audits are incomplete. Be exhaustive:

- **Every route.** Enumerate routes from the router config / nav links / sitemap, then
  visit each by URL. Don't trust that the nav shows them all.
- **Every persona.** Log in as EACH role. **Never audit as admin alone** — admin hides
  RBAC-gated UI and shows god-mode screens real users never see. Many pages render an
  empty/locked state for the wrong persona; use the persona who actually has the data.
- **Every state.** Force: empty, single-row, many-rows (pagination), long names/text,
  loading, and error. Open EVERY modal. Toggle **dark AND light** mode.
- **Every expandable.** Click accordions, "Show more"/"View all", tabs, dropdowns,
  row-expanders, popovers, tooltips, date pickers. A collapsed component hides defects.
- **Every link.** Follow list → detail (click rows/cards into their detail pages);
  follow CTAs. Detail pages (financial breakdowns, line items) are the richest surface.
- **Three widths.** desktop ~1366, tablet ~820, mobile ~375 — verify each.

## The design bar (the lenses you judge against)

1. **Alignment / lining up** — stacked cards, rows, labels, and section headers share
   one grid; numbers/currency right-aligned with `tabular-nums`; icons optically
   centered to text baselines; nothing 1–4px off.
2. **Spacing & rhythm** — one spacing scale (4/8px); consistent card padding and gaps
   (no 11px-here / 14px-there); not sparse, not cramped (<8px between tap targets).
3. **Typography — ONE scale** — page title > section title > body > caption, each a
   consistent size+weight. No two adjacent headings the same size doing different jobs;
   no single-word orphan on its own line; a screen uses ~4–6 sizes, not 10.
4. **Wrapping / truncation** — nothing wraps to ragged 2–3 lines where it should
   `nowrap`/truncate/shorten (nav, badges, buttons, table headers, stat labels).
5. **Color / token unity** — ONE semantic palette (owed/danger=red, paid/success=green,
   warning=amber, info=blue, neutral=gray) applied identically everywhere; same badge
   style for the same concept; uniform borders/shadows/radii; WCAG-AA in both themes.
6. **Consistency** — same card/badge/button/table/empty-state/modal patterns app-wide;
   uniform button heights, icon sizes, radii. No page reinventing a concept.
7. **Copy & states** — no raw enum / UUID / `[object Object]` / `$undefined` / `NaN`
   shown to a user; humane empty states (not a blank box or lonely sentence); real
   loading skeletons that match the final layout; consistent `—` vs `N/A`.
8. **Motion, focus & a11y** — visible keyboard focus ring on EVERY interactive element
   (`measure.js` flags focusables with none — real Blocker if `hasGlobalFocusStyle` is
   false) and a logical Tab order with no focus trap; hover feedback on clickable
   rows/cards; obvious active nav state. **Touch targets ≥44×44px at mobile (~375)** —
   measured. **Motion (measured):** no `transition: all`, animate only `transform`/
   `opacity` (never `width`/`height`/`top`/`left`/`margin` — they jank the main thread),
   durations ~50–700ms, and honor `prefers-reduced-motion: reduce`.
9. **Copy hygiene & colorblind-safe status** — typographic polish (`…` not `...`, curly
   `’ “ ”` not straight `' "`, `Saving…` not `Saving...`); status must never be conveyed
   by color ALONE — every red/green dot or badge pairs the color with an icon or label
   (fails ~8% of users otherwise). One font-family set (≤3 distinct; `measure.js` flags
   `FAMILY_DRIFT`).

## Tooling realities — what WILL fool you

- **`resize_page` clamps to the physical display** and sometimes shrinks instead of
  grows. Read `window.innerWidth` after EVERY resize and only trust the width you got.
  Tablet (640–1024) is often unreachable directly — try several requests (e.g. a 1100
  request may land ~733) and use the closest, or say you couldn't reach it.
- **`fullPage` screenshots CLIP the right edge** (a rendering artifact). NEVER diagnose
  overflow from one. Use a viewport screenshot for the look + the measure for truth.
- **Modal-over-page "overlaps" are z-index false positives** — ignore them.
- **In-container scroll is fine; clipping and page-scroll are not.** A wide table inside
  its own `overflow-x-auto` scroller is acceptable for a dense power-user grid. A defect
  is: the table's parent is `overflow:hidden`/`visible` so columns are unreachable, OR
  the whole PAGE scrolls sideways. Check `parentOverflowX` and page `scrollWidth`.
- **White text is only a bug on a LIGHT surface.** White-on-dark (a dark cockpit) is
  correct. Always read the element's background before flagging "invisible text".
- **Children inset by card padding look "misaligned" but aren't.** Compare only
  TOP-LEVEL sibling cards/rows for edge alignment; a nested element at parent-left +
  padding is correct.
- **A grep for a CSS class is noisy** — the relevant wrapper often lives in a PARENT
  component. Confirm any computed-style claim (overflow, color, alignment) in-browser on
  the rendered element, not from source alone.

## Fix-at-source (when fixing, not just flagging)

If a value is wrong in many places, fix the shared theme class / CSS token / component
ONCE — don't sprinkle per-instance overrides. Highest-leverage examples seen in the
wild: `tabular-nums` on the table-cell + `:root` (every number aligns); a default text
color on a light-themed layout root (kills the whole white-on-light class); a global
`:focus-visible` outline (every button gets a focus ring). Use an **outline**, not a
box-shadow ring, for focus — box-shadow rings get clipped by `overflow-hidden` wrappers.

## Output — scan wide, report tight

**Scan posture** (during the walk): "nothing is fine" (above) — flag everything the
measure or your eye catches. **Report posture:** don't dump the raw pile. The report is a
*ranked, capped, confidence-gated* defect log so a real Blocker never drowns under nits.

- **Severity ladder:** `Blocker` (broken / unreadable / unreachable UI, or an a11y
  violation that blocks use) > `High` (clearly wrong and hits every page — misaligned
  columns, failed contrast, no focus ring at all) > `Medium` (a noticeable polish miss on
  one surface) > `Nit` (true hair-splitting). Lead with Blocker/High.
- **Confidence gate:** report a finding only when you can name the measured failure or
  cite the specific token / rule / lens it violates. Suppress pure preference ("I'd prefer
  blue"). When you're unsure or it's opinion-only, drop it.
- **Cap, don't wall:** after the Blocker/High items, keep the highest-signal Medium items
  and at most a handful of Nits — **group entries that share one fix-at-source** so the
  count reflects fixes, not symptoms. A wall of 80 unranked items is a failed report.

Table: `page · width · lens · severity (Blocker/High/Medium/Nit) · what's wrong → fix ·
status`. If you also fixed, ship one small, independently-reviewable change per coherent
slice and re-verify each.

## Baseline & regression (optional)

On the first full pass, persist each page's `measure.js` JSON to the run dir (e.g.
`design-baseline/<route>.json`). On any re-run — after a fix or next session — re-measure
and **diff against the saved baseline** instead of re-judging from scratch: surface *new*
defects, *resolved* ones, and per-lens deltas (type-scale & font-family counts, contrast
fails, focus-ring-missing count, small touch targets, motion flags, color-only status).
That turns a one-shot audit into a trackable gate. The within-session
fix→re-measure→confirm-zero-regression loop already covers the immediate case; the
baseline adds cross-session tracking.

## Common mistakes

- Calling a screen "clean" from one desktop screenshot → run the measure + check mobile.
- Auditing only as admin → you miss every RBAC-gated and wrong-persona-empty screen.
- Diagnosing overflow from a fullPage shot, or trusting an unverified `resize_page`.
- "Fixing" intentional density or per-surface badge palettes (those exist to keep many
  statuses distinct within one screen) → that's not drift; don't force false uniformity.
- Flagging in-container table scroll, modal z-index overlaps, white-on-dark text, or
  padding-inset children → false positives; verify the predicate first.
- Reporting `focusRingCandidates` blindly → programmatic focus may not trip
  `:focus-visible` and a ring drawn on a parent isn't detected; confirm via the Tab walk
  (a true Blocker only when `hasGlobalFocusStyle` is false). Inline prose links aren't tap
  targets, and a purely decorative red/green dot isn't a color-only-status defect.
