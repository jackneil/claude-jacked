---
name: qa
description: Browser-based QA testing of UI changes. Use when UI files were modified and the user wants to verify changes in a browser. Detects available browser tools (Playwright MCP or Claude-in-Chrome) and runs systematic testing.
---

# QA Browser Testing

Reference material for browser-based QA testing of UI changes.

## Browser Tool Detection

### Playwright MCP

Tools prefixed with `mcp__plugin_playwright_playwright__browser_*`:
- `browser_navigate` — go to a URL
- `browser_snapshot` — accessibility tree (preferred for finding elements)
- `browser_take_screenshot` — visual screenshot
- `browser_click` — click an element by ref
- `browser_type` — type text into an element
- `browser_console_messages` — read JS console output
- `browser_network_requests` — inspect network activity
- `browser_resize` — change viewport size for responsive testing

### Claude-in-Chrome

Tools prefixed with `mcp__claude-in-chrome__*`:
- `navigate` — go to a URL
- `read_page` — accessibility tree
- `computer` — screenshot, click, type, scroll
- `find` — natural language element search
- `read_console_messages` — JS console output
- `read_network_requests` — network activity
- `resize_window` — change viewport size

## QA Checklist

### Visual
- Layout intact (no overlapping, no clipping)
- Text readable and properly aligned
- Colors and spacing consistent
- Images and icons loading

### Interactive
- Buttons respond to clicks
- Forms accept input and validate
- Navigation works (links, routing)
- Modals, dropdowns, toggles function

### Console
- No new JavaScript errors
- No unhandled promise rejections
- No 404s for assets

### Edge Cases
- Empty states render correctly
- Long text doesn't break layout
- Special characters handled in inputs

### Responsive (when applicable)
- Mobile (375px width)
- Tablet (768px width)
- Desktop (1280px+ width)
