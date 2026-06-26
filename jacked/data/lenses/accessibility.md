---
name: Accessibility
description: WCAG 2.2 compliance, keyboard nav, screen readers, color contrast
triggers: [ui, frontend, css, html, component, page, form, button, input, modal, dialog]
---

# Accessibility Lens

## What to check

- Color contrast ratios meet WCAG AA (4.5:1 normal text, 3:1 large text)
- All interactive elements are keyboard-accessible (tab order, focus indicators)
- Form inputs have associated labels (not just placeholder text)
- Images have alt text, decorative images have empty alt=""
- ARIA roles used correctly — not sprinkled on arbitrarily
- Error messages are announced to screen readers
- No information conveyed by color alone
- Focus management after dynamic content changes (modals, route changes)
- Skip navigation link for keyboard users
- **Target size: WCAG 2.2 AA (2.5.8) minimum is 24×24 CSS px** (exceptions: inline
  links in text, user-agent-controlled controls, or where the size is essential).
  44×44 is the stronger AAA / recommended-mobile target — aim for it on touch UIs,
  but don't flag 24–44px as an AA failure.
- Language attribute set on html element
- Page title is descriptive and unique per page
- Heading hierarchy is logical (no skipped levels)
- **WCAG 2.2 additions:** focus indicator is not obscured by sticky/fixed headers or
  footers (2.4.11/2.4.12); the focus indicator is ≥2px thick at ≥3:1 contrast against
  adjacent colors (2.4.13); any drag interaction has a single-pointer alternative
  (2.5.7); auth allows paste + password-manager autofill and imposes no
  cognitive-function test/CAPTCHA without an alternative (3.3.8); previously-entered
  info is auto-populated or selectable rather than re-typed (3.3.7); help mechanisms
  appear in a consistent location/order across pages (3.2.6).
- **Live regions / dynamic announcements:** async status (loading, search results,
  validation, success/toast) is exposed via `aria-live`/`role=status` (polite) or
  `role=alert` (assertive); content swaps don't update silently for screen-reader users.
- **Data tables & landmarks:** tables use `<th scope>` (or headers/id association) and
  a `<caption>`; sortable columns expose `aria-sort`; the page uses landmark regions
  (`header`/`nav`/`main`/`footer` or ARIA landmarks) with exactly one `main`.
- **Link/control text & ARIA correctness:** links and buttons have text meaningful out
  of context (no bare "click here"/"read more"); links opening a new tab warn the user.
  **First rule of ARIA — prefer a native HTML element over an ARIA-retrofitted `div`;**
  every custom control exposes a correct name, role, value, and state.
- **Reduced motion:** non-essential animation and parallax honor
  `prefers-reduced-motion: reduce`; nothing auto-moves/auto-updates for >5s without a
  pause/stop control (WCAG 2.2.2 / 2.3.3).

## Common anti-patterns

- Using div/span as buttons instead of semantic button/a elements
- Hiding focus outlines with outline:none without providing alternative
- Auto-playing media without controls
- Using tabindex > 0 (disrupts natural tab order)
- Relying on hover states for essential information
- Placeholder text as the only label for inputs
- Modal dialogs that don't trap focus
- Custom dropdown/select that isn't keyboard-navigable
- Toast notifications that disappear before screen reader announces them

## When to apply

Any change that touches user-facing HTML, components, or styling.
Especially important for: forms, modals/dialogs, navigation, data tables,
error states, and any interactive widget.

**Verification expectations:** automated tools (axe/Lighthouse) cover only ~30% of
criteria — a keyboard-only pass (focus order, traps, activation) and a screen-reader
pass (NVDA/VoiceOver: announcements, reading sequence) are required for full coverage.
Note WCAG 2.2 **removed 4.1.1 Parsing** — don't flag HTML well-formedness as a WCAG
failure.
