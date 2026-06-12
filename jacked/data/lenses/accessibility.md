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
- Touch targets are at least 44x44px on mobile
- Language attribute set on html element
- Page title is descriptive and unique per page
- Heading hierarchy is logical (no skipped levels)

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
