# Firecrawl `/interact` Endpoint

**Researched:** 2026-04-04
**Source:** https://www.firecrawl.dev/blog/introducing-interact-endpoint
**Released:** March 25, 2026
**Cost:** 2 credits per session minute (prorated per second), plus standard scrape credits for the initial page

## What It Does

Turns any Firecrawl scrape into a live browser session you can control. Instead of just getting page content back, the browser stays open and you can click, type, navigate, and extract data from dynamic pages — all through the same session.

## The Flow

```
1. Scrape a page (keeps browser session alive)
   firecrawl scrape https://example.com --profile my-app

2. Take actions on the page (repeat as needed)
   firecrawl interact "click the search button"
   firecrawl interact "type 'query' into the search box and press Enter"
   firecrawl interact "extract the results table"

3. End the session
   firecrawl interact stop
```

Sessions last up to 10 minutes (5-minute idle timeout). Chain as many interactions as needed within that window.

## Two Interaction Modes

### Natural Language (no code)

Describe the action in plain English. Firecrawl finds the element and does it.

```bash
firecrawl interact "Search for iPhone 16 Pro Max"
firecrawl interact "Click on the first result and tell me the price"
firecrawl interact "Fill in the email field with user@example.com and click Submit"
```

Each prompt should be one clear task. The agent handles element finding, clicking, typing, and waiting.

### Code Execution (precise control)

Pass Playwright code directly. The `page` object is pre-connected to the live session.

```bash
# JavaScript (default)
firecrawl interact -c "
  await page.click('#next-page');
  await page.waitForLoadState('networkidle');
  const title = await page.title();
  JSON.stringify({ title });
"

# Python
firecrawl interact -c --python "
  await page.click('#next-page')
  print(await page.title())
"

# Against a specific scrape ID
firecrawl interact <scrape-id> -c "await page.title()"
```

Node.js, Python, and Bash are all supported. Same Playwright API as the `firecrawl browser` tool.

## Named Profiles (Persistent Auth)

Save browser state (cookies, localStorage, login sessions) across scrapes with named profiles. Don't re-authenticate every time.

```bash
# Session 1: Log in and save state
firecrawl scrape https://app.example.com/login --profile my-app
firecrawl interact "Fill in user@example.com and password, then click Login"
firecrawl interact stop

# Session 2: Already logged in
firecrawl scrape https://app.example.com/dashboard --profile my-app
firecrawl interact "Extract the dashboard data"
firecrawl interact stop
```

Profiles persist until deleted. Useful for recurring scrapes on authenticated pages.

## Live View URLs

Every interact response includes two URLs:

- **`liveViewUrl`** — read-only stream of the browser (for monitoring, embedding in dashboards)
- **`interactiveLiveViewUrl`** — fully interactive stream where a human can take over the browser

Use cases: human-in-the-loop review, debugging scrapes, embedding a browser view in internal tools.

## API (Direct, Not CLI)

```
POST /v2/scrape                         → returns scrapeId + page content
POST /v2/scrape/{scrapeId}/interact     → takes actions (prompt or code)
DELETE /v2/scrape/{scrapeId}/interact   → ends the session
```

## How It Differs From `firecrawl browser`

| Feature | `firecrawl browser` | `firecrawl interact` |
|---|---|---|
| Starts from | Standalone browser session | An existing scrape |
| Element targeting | `@ref` IDs from `snapshot` | Natural language or code |
| Auth persistence | No built-in profiles | Named profiles save cookies/state |
| Live view | Requires `--stream` flag | Included by default |
| Session state | Separate from scrape | Carries over from the initial scrape |
| Best for | Deterministic automation, QA | Dynamic content behind interactions, auth flows |

## How It Differs From Browser Sandbox

`/interact` is attached to an existing scrape session — you scrape a URL first to get a `scrapeId`, then take actions in that same page. The browser sandbox (`firecrawl browser`) is a standalone browser environment launched independently, without needing a scrape first. Use `/interact` when you want to act on a page you just scraped; use the browser sandbox for full automation workflows that don't start from a scrape.

## Use Cases

- **Paginated data:** Click "next page" in a loop, extract at each step
- **Form submission:** Fill search boxes, apply filters, get dynamic results
- **Authenticated scraping:** Log in once with a profile, scrape the dashboard repeatedly
- **Multi-step workflows:** Chain prompts — search → filter → click → extract
- **Live monitoring:** Embed `liveViewUrl` in an internal tool

## Potential Use in jacked

- **QA with auth:** If the dashboard ever adds login, named profiles would avoid re-auth in test flows
- **Natural language QA:** Less setup than `snapshot` + `click @ref` — but also less deterministic
- **Dashboard embedding:** `liveViewUrl` could show a live browser inside the jacked dashboard
- **Current assessment:** The existing `firecrawl browser` shorthand handles our QA needs fine for now. `/interact` becomes more valuable when we need persistent auth or natural language interaction.
