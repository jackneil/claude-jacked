# GStack Integration Design — Selective Skill Adoption into Jacked

**Date:** 2026-03-24
**Status:** Design
**Context:** Evaluated all 28 GStack skills against jacked's existing capabilities (3 hooks, 16 commands, 7 skills, 10 agents, security gatekeeper, guardrails system, web dashboard) plus the superpowers plugin and 15+ other installed plugins. This document covers only the skills that fill genuine gaps.

---

## Executive Summary

GStack (Garry Tan's Claude Code skill pack) provides 28 slash commands implementing a sprint-based development workflow. After thorough comparison, **21 of 28 skills are duplicates** of what jacked + superpowers + installed plugins already provide. **7 skills fill real gaps** and should be adapted as native jacked commands/skills/hooks.

**Do NOT install the full gstack package.** It would conflict with existing skills (name collisions on `/qa`, `/review`, competing browser tools, duplicate meta-routers) and introduce a parallel infrastructure (telemetry, config system, browse daemon, session tracking) that duplicates jacked's existing infrastructure.

---

## Skills to Integrate

### 1. `/cso` — Security Audit (OWASP + STRIDE)

**Priority:** HIGH
**Type:** New jacked command (`~/.claude/commands/cso.md`)
**GStack source:** `skills/cso/SKILL.md`

**What it does:**
- Multi-phase security audit: tech stack detection → attack surface census → git history secret scan → dependency audit → per-OWASP-category analysis → STRIDE threat model
- Read-only — produces a Security Posture Report without making code changes
- 8/10+ confidence gate to eliminate noise (17 false positive exclusions)
- Each finding includes a concrete exploit scenario
- Scope modes: `--infra`, `--code`, `--skills`, `--supply-chain`, `--owasp`, `--diff` (branch changes only), `--comprehensive`

**Why jacked needs this:**
- Jacked's security gatekeeper is *runtime protection* (blocks dangerous commands as they happen)
- The security-guidance plugin is a PreToolUse hook that warns about code patterns
- Neither is a *proactive audit* that systematically reviews the codebase for vulnerabilities
- `/cso` is the missing third leg: prevent (gatekeeper) → warn (security-guidance) → audit (cso)

**Dependencies:** None beyond standard tools (Bash, Read, Write, Grep, Glob, WebSearch). No browser needed.

**Integration notes:**
- Adapt as a standalone command `.md` file — no GStack infrastructure needed
- Strip GStack telemetry/config/update-check preamble
- Add to jacked behavioral rules: "suggest `/cso` before merging security-sensitive changes"
- Could integrate findings into jacked's analytics DB for tracking security posture over time

**Adaptation required:**
- Remove references to GStack state system (`~/.gstack/`, `gstack-review-log`, etc.)
- Remove optional Codex integration (or make it a separate concern)
- Keep the core audit methodology, OWASP categories, STRIDE framework, and reporting format

---

### 2. `/freeze` + `/unfreeze` — Edit Scope Restriction

**Priority:** HIGH
**Type:** New PreToolUse hook addition to security gatekeeper + two new commands
**GStack source:** `skills/freeze/SKILL.md`, `skills/unfreeze/SKILL.md`

**What it does:**
- `/freeze <path>` — restricts all Edit and Write tool operations to a single directory
- Blocks any file modification outside the boundary path
- Does NOT restrict Read, Bash, Grep, Glob (read-only is always allowed)
- `/unfreeze` — removes the restriction
- State stored in a simple file (`freeze-dir.txt`)

**Why jacked needs this:**
- The security gatekeeper blocks *dangerous commands* but doesn't restrict *where* Claude edits
- During debugging, Claude often "helpfully" touches unrelated files (refactoring while fixing a bug, updating docs while patching a route)
- Freeze prevents scope creep during focused work
- Particularly valuable when working in sensitive areas (auth, billing, multi-tenancy)

**Dependencies:** None. Simple state file + hook logic.

**Integration approach — two options:**

**Option A: Integrate into security gatekeeper (recommended)**
- Add freeze-dir check to the existing `security_gatekeeper.py` PreToolUse hook
- When `~/.claude/jacked-freeze-dir.txt` exists, block Edit/Write operations to paths outside it
- Advantages: single hook, no additional hook overhead, gatekeeper already evaluates every tool call
- The gatekeeper's Tier 0/1 checks run in <1ms — adding a freeze check adds negligible latency

**Option B: Separate hook**
- New `freeze_guard.py` PreToolUse hook
- Simpler to maintain but adds another hook to the evaluation chain

**Commands:**
- `freeze.md` — reads the path argument, writes `~/.claude/jacked-freeze-dir.txt`, confirms the boundary
- `unfreeze.md` — deletes the file, confirms removal
- No need for `/guard` (GStack's combo command) since jacked's gatekeeper already provides the `/careful` equivalent

**Behavioral rule addition:**
- `superpowers:systematic-debugging` could suggest activating freeze when entering investigation mode (mirrors how GStack's `/investigate` auto-activates `/freeze`)

---

### 3. `/retro` — Engineering Retrospective

**Priority:** MEDIUM
**Type:** New jacked command (`~/.claude/commands/retro.md`)
**GStack source:** `skills/retro/SKILL.md`

**What it does:**
- Analyzes git history and produces per-contributor breakdowns
- Metrics: commits, LOC added/removed, test ratio, PR sizes, fix ratio, coding session detection (from commit timestamps), file hotspots, shipping streaks
- Team-aware: specific praise and growth opportunities per contributor
- Test health trends (ratio changes over time)
- Multiple windows: 24h, 7d, 14d, 30d
- Compare mode: diff current period vs prior period
- Global mode: works across multiple repos and AI tools

**Why jacked needs this:**
- No equivalent anywhere in the current stack
- Closes the development feedback loop — sprint velocity, team patterns, quality trends
- Natural companion to jacked's existing analytics (gatekeeper decisions, command usage, agent invocations)

**Dependencies:** Git, GitHub CLI (`gh`) for PR data. No browser needed.

**Integration notes:**
- Adapt as standalone command
- Could pull additional data from jacked's analytics DB (command usage frequency, gatekeeper approval patterns, session durations)
- Strip GStack-specific integrations (Greptile batting average, `.context/retros/` state directory)
- Use jacked's analytics DB for trend storage instead of GStack's `.context/retros/` files
- Add to behavioral rules: "suggest `/retro` at the end of long sessions or weekly"

---

### 4. `/canary` — Post-Deploy Monitoring

**Priority:** MEDIUM
**Type:** New jacked command (`~/.claude/commands/canary.md`)
**GStack source:** `skills/canary/SKILL.md`

**What it does:**
- Post-deploy continuous monitoring loop (10 min default, configurable 1-30 min)
- Takes periodic screenshots, checks console errors, measures performance
- Compares against previously captured baselines
- Auto-discovers pages to monitor from navigation links
- Baseline capture mode (run before deploy) saves reference state
- Alerts on anomalies: new console errors, performance regressions, content changes

**Why jacked needs this:**
- No post-deploy verification exists today
- After Railway deploys, the only check is manual browsing
- Especially valuable after production deploys where issues may not be immediately obvious

**Dependencies:** Browser tool required. GStack uses its custom Playwright daemon.

**Browser adaptation:**
GStack's canary depends on its `/browse` daemon (Playwright-based). Jacked needs to adapt this to use **chrome-devtools MCP** (already installed) instead:
- `navigate_page` instead of `goto`
- `take_screenshot` instead of `screenshot`
- `get_console_message` / `list_console_messages` instead of `console`
- `evaluate_script` for `performance.getEntries()` calls
- `list_network_requests` for request monitoring

This is the main adaptation work — the monitoring logic stays the same, just the browser interface layer changes.

**Integration notes:**
- Store baselines in `~/.claude/jacked-canary/baselines/` (not `.gstack/`)
- Could be triggered automatically by a future `/land-and-deploy` command
- Railway-specific: check deploy status via `railway status` or Railway API

---

### 5. `/land-and-deploy` — Post-Merge Deploy Verification

**Priority:** MEDIUM
**Type:** New jacked command (`~/.claude/commands/land-and-deploy.md`)
**GStack source:** `skills/land-and-deploy/SKILL.md`

**What it does:**
- Post-PR-merge workflow that picks up where `/commit-push-pr` leaves off
- Merges the PR (via `gh pr merge`)
- Waits for CI to pass
- Waits for deploy to complete (auto-detects platform: Railway, Vercel, Fly.io, etc.)
- Runs canary verification on the production URL
- Offers revert if issues detected
- Produces a deploy report with timing data

**Why jacked needs this:**
- Current workflow ends at PR creation (`/commit-push-pr`, `/pr`)
- No automation for the merge → deploy → verify cycle
- Particularly valuable for Railway where deploys happen automatically after merge

**Dependencies:** GitHub CLI (`gh`), `/canary` (for post-deploy verification), browser tool (via canary).

**Integration notes:**
- Depends on `/canary` being implemented first
- Railway detection: look for `railway.toml`, `RAILWAY_*` env vars
- Pre-merge readiness gate: could check jacked's review dashboard (was `/dcr` run? did it pass?)
- Store deploy config in CLAUDE.md (one-time setup, replaces GStack's `/setup-deploy`)
- Add behavioral rule: "after PR is approved and CI passes, suggest `/land-and-deploy`"

---

### 6. `/benchmark` — Performance Regression Detection

**Priority:** MEDIUM
**Type:** New jacked command (`~/.claude/commands/benchmark.md`)
**GStack source:** `skills/benchmark/SKILL.md`

**What it does:**
- Captures real performance metrics via `performance.getEntries()` API:
  - TTFB, FCP, LCP, DOM Interactive, DOM Complete, Full Load
  - Resource sizes, bundle sizes, request counts
  - Top 10 slowest resources
- Baseline capture mode: saves reference metrics
- Comparison mode: diffs current vs baseline with regression thresholds
  - >50% timing increase = regression
  - >25% bundle size increase = regression
- Industry performance budget checks
- Trend analysis across historical baselines

**Why jacked needs this:**
- No performance tracking exists today
- Healthcare apps need performance monitoring — slow pages erode user trust and compliance
- Pairs well with `/canary` (canary monitors health, benchmark tracks performance over time)

**Dependencies:** Browser tool required (same chrome-devtools MCP adaptation as `/canary`).

**Integration notes:**
- Same browser adaptation as `/canary` — `evaluate_script` for performance API calls
- Store baselines in `~/.claude/jacked-benchmark/baselines/`
- Could be triggered as part of `/dcr` or `/canary` flows
- Add behavioral rule: "suggest `/benchmark` before and after performance-sensitive changes"

---

### 7. `/codex` — Cross-Model Review

**Priority:** LOW
**Type:** New jacked command (`~/.claude/commands/codex.md`)
**GStack source:** `skills/codex/SKILL.md`

**What it does:**
- Wraps OpenAI Codex CLI for cross-model code review
- Three modes:
  - **Review** — `codex review` against current diff, P1/P2/P3 severity classification, PASS/FAIL gate
  - **Challenge** — adversarial mode, Codex tries to break the code (max reasoning effort)
  - **Consult** — open conversation with session continuity
- Cross-model comparison when both Claude's `/dcr` and Codex have analyzed the same branch
- Identifies overlapping vs unique findings between models

**Why jacked might want this:**
- Different models catch different blind spots
- Independent verification from a non-Claude model adds confidence
- Adversarial challenge mode is unique — no jacked equivalent

**Why it's LOW priority:**
- Requires OpenAI subscription + `npm install -g @openai/codex`
- Adds external dependency and cost
- `/dcr` with randomized personas already provides diverse review angles

**Integration notes:**
- Only implement if user has/wants OpenAI access
- Adapt as standalone command
- Could integrate with `/dcr` — offer Codex challenge as an optional final pass
- Strip GStack state management, use jacked's own review tracking

---

## Skills NOT Being Integrated (with reasoning)

### Duplicate of Jacked Security Gatekeeper
| GStack | Why Skip |
|--------|----------|
| `/careful` | Pattern-matching destructive command blocker. Jacked's gatekeeper is a 5-tier evaluation chain with LLM fallback, sensitive file protection, command categories, audit system, and security profiles. Strictly inferior. |
| `/guard` | Combo of `/careful` + `/freeze`. Since we're skipping `/careful` (gatekeeper is better) and integrating `/freeze` separately, this combo wrapper isn't needed. |

### Duplicate of Existing Review Tools
| GStack | Jacked/Plugin Equivalent |
|--------|--------------------------|
| `/review` | `/dcr` (recursive multi-lens, randomized personas, runs until clean) |
| `/plan-ceo-review` | `superpowers:writing-plans` + `/dc` (phase-aware review) |
| `/plan-eng-review` | `superpowers:writing-plans` + `/dcr` (architecture lens) |
| `/autoplan` | `superpowers:executing-plans` + `superpowers:subagent-driven-development` |

### Duplicate of Existing QA/Browser Tools
| GStack | Jacked/Plugin Equivalent |
|--------|--------------------------|
| `/qa` | `/qa` (jacked) — direct name collision, same concept |
| `/qa-only` | Covered by existing `/qa` report mode |
| `/browse` | firecrawl + chrome-devtools MCP — two browser tools already |
| `/setup-browser-cookies` | Dev-login auth bypass flow in HANK OS |

### Duplicate of Other Existing Tools
| GStack | Jacked/Plugin Equivalent |
|--------|--------------------------|
| `/office-hours` | `superpowers:brainstorming` |
| `/investigate` | `superpowers:systematic-debugging` |
| `/ship` | `/commit-push-pr` + `superpowers:finishing-a-development-branch` |
| `/document-release` | `readme-maintainer` agent |
| `/design-consultation` | `frontend-design` plugin + established design system docs |
| `/gstack` (root) | `superpowers:using-superpowers` (meta-router) |
| `/gstack-upgrade` | Self-updater for gstack only |
| `/setup-deploy` | One-time manual config — just add Railway section to CLAUDE.md |

### Borderline — Not Worth the Complexity
| GStack | Reasoning |
|--------|-----------|
| `/plan-design-review` | DCR already has frontend design reviewer sub-component. Specialized design scoring (0-10 across 7 dimensions) is interesting but not critical enough to warrant another command competing in the review space. |
| `/design-review` | Live-site visual audit + fix loop. Overlaps with `/qa` + `/ux`. Different angle but too much overlap. |

---

## Implementation Order

```
Phase 1 — Safety & Security (no browser dependency)
  1. /freeze + /unfreeze  (hook integration into gatekeeper + 2 command files)
  2. /cso                 (standalone command file)

Phase 2 — Process & Insights (no browser dependency)
  3. /retro               (standalone command file)

Phase 3 — Deploy Pipeline (requires browser adaptation)
  4. /canary              (command + chrome-devtools MCP adaptation)
  5. /benchmark           (command + shares browser adapter with canary)
  6. /land-and-deploy     (command, depends on canary)

Phase 4 — Optional
  7. /codex               (only if OpenAI access is desired)
```

**Phase 1 and 2 can ship immediately** — no browser adaptation work needed.
**Phase 3 requires adapting GStack's Playwright-based browser commands to use chrome-devtools MCP.** This is the main engineering work. The monitoring/benchmarking logic stays identical; only the browser interface layer changes.

---

## Behavioral Rule Additions

Add to `jacked/data/rules/jacked_behaviors.md`:

```markdown
- After implementing security-sensitive changes (auth, RBAC, multi-tenancy, billing, credential handling), suggest /cso for a security audit
- When debugging in a focused area, suggest /freeze to prevent accidental edits outside the target module
- At the end of long sessions or weekly, suggest /retro for an engineering retrospective
- After a production deploy, suggest /canary for post-deploy monitoring
- Before and after performance-sensitive changes, suggest /benchmark for regression detection
```

---

## What NOT to Import from GStack Infrastructure

| GStack Component | Why Skip |
|---|---|
| `~/.gstack/` state directory | Jacked uses `~/.claude/jacked.db` (SQLite) and `~/.claude/jacked-*` files |
| `gstack-telemetry-log` | Jacked has its own analytics DB with command_usage, agent_invocations, hook_executions tables |
| `gstack-config` / `config.yaml` | Jacked uses settings.json + CLI flags |
| `gstack-review-log` / Review Dashboard | Could integrate review tracking into jacked's analytics DB instead |
| Browse daemon (Playwright/Bun) | Use chrome-devtools MCP (already installed) |
| Version update checks | Jacked has `jacked check-version` |
| Session tracking | Jacked has session_account_tracker hook + multi-account system |
| Proactive skill suggestions | `superpowers:using-superpowers` handles this |

---

## Open Questions

1. **Freeze integration location:** Option A (integrate into security_gatekeeper.py) vs Option B (separate hook)? Recommendation: Option A for simplicity, but the gatekeeper is already ~2800 lines.

2. **Browser adaptation scope:** Should the chrome-devtools MCP adapter be a shared utility that `/canary`, `/benchmark`, and future browser-dependent commands all import? Or should each command inline its browser calls?

3. **Analytics integration:** Should `/retro` and `/cso` log their runs to jacked's analytics DB? This would enable tracking security posture and team velocity trends in the web dashboard.

4. **Codex priority:** Is OpenAI access available/desired? If not, `/codex` drops off the list entirely.
