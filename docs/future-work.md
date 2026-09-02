# Future Work — claude-jacked

> Generated 2026-03-16 via `/swarm-research` (5 agents) + `/dcr` (4 reviewers + pre-mortem). Validated against Claude Code native features, plugin ecosystem (9,000+ plugins), and Gary Tan's gstack.

## Executive Summary

A 5-agent swarm-research session identified potential new commands, then a full DCR review pressure-tested them against the existing ecosystem. Result: most proposals already exist as native Claude Code features, community plugins, or in gstack. The highest-value work is **subtracting** redundant features and **enhancing** existing commands, not adding new ones.

---

## Priority 1: Subtract and Consolidate

### Audit existing commands against native Claude Code — DONE

**Completed:** 2026-03-21. Full audit at `docs/command-audit-2026-03.md`.

**Results:** 16 user-facing entities assessed. All KEEP except `/learn` and `/swarm` marked EVALUATE:
- `/learn` — overlaps with Auto-Memory but is more structured (graduation path, version-controlled, auditable). KEEP with distinction notes added.
- `/swarm` — thin wrapper around native Agent Teams but adds file isolation + scaling heuristics. KEEP for now, monitor.
- `/handoff` — confirmed DO NOT BUILD.
- Surface area: 16 user-facing entities, within the 15-18 budget. New commands require deprecating one.

### Progressive disclosure for `jacked install`

Currently: all-or-nothing install (14 commands, 10 agents, 7 skills = 31 entities). New users see a wall of options.

**Proposal:** `jacked install --core` installs essentials only (e.g., `/dc`, `/dcr`, `/learn`, `/pr`, `/release`). Full `jacked install` adds everything. Dashboard groups commands by category (Quality, Planning, Git, Meta).

---

## Priority 2: Enhance Existing Commands

### Embed changelog generation in `/release`

Rather than a standalone `/changelog` command (which exists in the ecosystem already — ComposioHQ, MCP Market, gstack's `/ship`), add a step to the existing `/release` pipeline:

- Before creating the GitHub release, generate a structured changelog from `git log <last-tag>..HEAD`
- Group by conventional commit type (feat/fix/refactor/docs)
- Cross-reference closed GitHub issues via `gh`
- Use as `--notes` content instead of `--generate-notes`
- Effort: S

### Make `/whats-next` feedback-aware

Instead of custom state persistence (which competes with native Tasks system), add lightweight feedback:

- At the start of a run, read the most recent `.claude/pm/retro.md` if it exists (from a future `/retro`)
- Show a one-line "last run recommended X — you did Y (commit abc)" breadcrumb by scanning git log for evidence of past recommendations
- Don't build a full state machine — the 80/20 version is "mention what changed since last time"
- Effort: S-M

---

## Priority 3: Conditionally Build

### `/retro` — only if differentiated from gstack

**gstack's `/retro`** (https://github.com/garrytan/gstack/tree/main/retro) already does:
- Git history analysis with time windows (7d, 14d, 30d)
- Velocity metrics, commit type breakdowns, streak tracking
- JSON snapshots in `.context/retros/` for trend comparison
- `/retro compare` for week-over-week analysis

**jacked's angle (if we build it):** The unique value would be a feedback loop with `/whats-next` — "last time I recommended X, you did Y instead, here's what happened." gstack's retro is standalone; ours would feed into the planning cycle. Only build if this loop is the core feature, not the git analysis (which gstack already does better).

- Effort: M
- Prerequisite: `/whats-next` feedback awareness (Priority 2)

---

## Priority 4: Future Considerations (Not Now)

These emerged from the swarm-research but were deprioritized by the DCR review. Revisit when the ecosystem stabilizes.

| Idea | Why Not Now | Revisit When |
|------|-----------|-------------|
| `/discover` (competitive analysis) | Zero user demand signal; premature | User base grows beyond solo developer |
| `/sprint` (sprint planning) | Imposes process solo devs resist | Team features are needed |
| `/explain` (architecture walkthrough) | alexanderop/walkthrough exists; Claude does this natively with 1M context | Native explanation features plateau |
| `/adr` (decision records) | Duplicates existing `docs/` design doc pattern | ADR pattern gains traction in the community |
| `/spec` (feature specification) | Spec-driven development trend is real but tooling is unstable | GitHub Spec Kit or similar standardizes |
| `/today` (daily standup summary) | Cited in GitHub issue #29585; may go native | Anthropic hasn't shipped it after 6+ months |
| `/blast` (blast radius analysis) | Compelling safety feature; no ecosystem equivalent found | After Priority 1-3 are done |
| `/deps` (dependency audit) | Better handled by dedicated tools (Dependabot, Snyk) | If MCP integrations make it natural |
| `/onboard` (new contributor guide) | Low urgency for a solo-maintainer project | Contributors start joining |

---

## Competitive Landscape (as of March 2026)

### Gary Tan's gstack

https://github.com/garrytan/gstack — 9 slash commands organized around team-role personas:
- `/retro` — engineering retrospective with git analytics and trend tracking
- `/ship` — full release workflow with built-in changelog generation
- `/plan-ceo-review` — product-level planning review
- `/plan-eng-review` — architecture/technical review
- `/review` — production risk code review
- `/qa`, `/browse` — browser-based QA

**Key insight:** gstack treats release as a workflow (`/ship` = merge + test + bump + changelog + push + PR), not a sequence of standalone commands. This is more ergonomic than separate `/changelog` + `/release`.

### Claude Code Native Features (Feb-March 2026)

- **Auto-Memory / MEMORY.md** — persistent cross-session context
- **Session Memory** — automatic session summaries
- **Remote Control** — cross-device session access
- **Agent Teams** — native TeamCreate/TaskCreate/SendMessage
- **Tasks** — persistent task tracking across sessions
- **`--resume`** — restore full session context
- **`/remember`** — promote patterns to permanent config
- **Plugin Marketplace** — official Anthropic marketplace (launched March 7, 2026)

### Plugin Ecosystem Highlights

- **9,000+ Claude Code plugins** in the wild
- **Changelog generators:** ComposioHQ/awesome-claude-skills, MCP Market, buildwithclaude
- **Session handoff:** 5+ community implementations (MCP Market, t0ddharris, parcadei/Continuous-Claude-v3)
- **Architecture walkthrough:** alexanderop/walkthrough (interactive HTML + Mermaid diagrams)
- **Retrospectives:** Smithery.ai bitwarden/retrospecting, sprint retro automation

---

## Meta-Insight: The Subtraction Imperative

The pre-mortem analyst identified the most important finding: **claude-jacked has robust processes for adding features but zero processes for removing them.** The behavioral rules say "NEVER defer known issues" but feature bloat is a known issue that's invisible because no tool detects it.

Before adding any new commands, establish:
1. **Usage audit:** Which commands are actually used? (Would require opt-in telemetry or periodic user surveys)
2. **Redundancy check:** Which commands duplicate native Claude Code features?
3. **Deprecation policy:** How are commands retired? Announcement → deprecation warning → removal over 2 releases?
4. **Surface area budget:** Set a max command count (e.g., 15-18) and require deprecating something before adding something new.

---

## Research Sources

### Swarm Research (Phase 1 — 5 agents)
- Agent 1 (competitive analysis): Cursor, Windsurf, Cody, Continue.dev, aider, Devin, Copilot Workspace, Augment Code, developer productivity platforms
- Agent 2 (internal analysis): Full read of all 14 commands, 7 skills, 10 agents
- Agent 3 (platform vision): SDLC lifecycle mapping, phased maturity model, market trends
- Agent 4 (user pain points): GitHub issues, HN/Reddit discussions, SlopCodeBench research on AI code erosion
- Agent 5 (PM vision): Product management tools, lightweight agile, discovery session design

### DCR Review (4 reviewers + pre-mortem)
- Reviewer A: Claude Code changelog, plugin marketplace, ecosystem overlap analysis
- Reviewer B: Gary Tan's gstack analysis, UX/flow review of proposals
- Reviewer C: Maintainability audit, cross-command coupling analysis, marketplace overlap
- Pre-mortem: Feature bloat scenario, Anthropic overlap scenario, new user overwhelm scenario


---

## Proposal: teach `jacked install` about graphify, and about subscription billing

> Added 2026-08-20 from a live hank-hq session that built a 2,500-node company
> graph. Two separate proposals; the second is the one that costs users money
> today.

### 1. Offer graphify wiring in per-repo setup, where it earns its keep

`jacked` does not mention graphify anywhere today (verified: zero hits across
`jacked/`, `docs/`, `design/`). The graphify skill installs itself in its own
Step 1. That is fine for a user who already knows it exists, and invisible to
everyone else.

**This costs zero command surface**, which matters given the 15-18 entity
budget and the "subtract, don't add" finding above. graphify is a third-party
tool; the proposal is that `/jacked-setup` *detects and offers* it, not that
jacked absorbs it as a command.

Where it pays off, and where it does not:

- **Worth offering:** repos with a substantial prose corpus — contracts, board
  minutes, investor updates, research notes, policy documents. In the source
  session, a graph over an investor-update tree answered "which cyber policy is
  in force and who is the named insured" across documents nobody had read
  individually, and surfaced a partnership's status from an all-hands deck that
  no one thought to look in.
- **Not worth offering:** code-only repos. graphify extracts code structurally
  via AST with no LLM and no key, so the semantic pass — the expensive part —
  has nothing to do. Offering it there is noise.
- **Detection heuristic:** count non-code files (`.md`, `.txt`, `.pdf`, `.docx`)
  outside `node_modules`/`.git`. Offer above some threshold; stay silent below.

Setup should also warn about the operational trap that bit this session: the
sync/serve sides of a corpus drift silently. A glob uploads any file while a
hardcoded allowlist serves only named ones, so a document lands and is
unreachable with nothing failing to say so.

### 2. Recommend subscription auth over API keys — everywhere, not just graphify

**This is the load-bearing half.** Users on this box pay for Claude Code and
Codex subscriptions. Any tool that reaches for a provider API key bills them a
second time, per token, for work a subscription already covers.

graphify's skill is a concrete example of the anti-pattern: it auto-selects the
Gemini backend the moment `GEMINI_API_KEY` is set, with no prompt. Following it
in the source session spent pay-as-you-go OpenAI credit on extraction the
user's Codex subscription would have covered. A key being present is not
consent to spend it.

**Proposal:** `jacked install` / `/jacked-setup` should detect subscription
auth and prefer it, in this order:

1. `codex` CLI present and signed in -> `codex exec` (this is what `/dcr`'s
   codex engine already does, so the pattern is proven in-tree)
2. `claude` CLI -> plan auth (graphify ships a `claude-cli` backend priced
   `0.0/0.0` for exactly this reason)
3. provider API keys — last, and only with an explicit "this will spend API
   credit" line before the first call

`/dcr`'s review-engine card is the precedent worth generalising: it already
frames the choice as "spend an OpenAI subscription instead of the Anthropic
plan." That framing belongs at install time for every tool jacked touches, not
just the review engine.

### Measured notes, so nobody re-derives them

Extraction quality and cost, same 26KB document, `gpt-5.6-luna` via graphify's
`openai` backend:

| reasoning_effort | nodes | edges | output tokens | substantive facts |
| --- | --- | --- | --- | --- |
| omitted (stock config) | 16 | 21 | 5,092 | 4/7 |
| low | 9 | 10 | 4,164 | 2/7 |
| high | 23 | 43 | 12,656 | 4/7 |
| xhigh | 0 | 0 | 131,072 | 0/7 |

- graphify's `openai` backend sets **no** `reasoning_effort` (its `gemini`
  backend sets `"low"`), so a stock run is at the API default, not any
  considered setting.
- The `xhigh` row is a **ceiling artifact, not a model verdict**: `max_tokens`
  is 16,384, reasoning tokens bill as output against that same cap, so the
  model spent its whole budget thinking and emitted nothing. 131,072 / 16,384 =
  exactly 8 calls, each pinned at the ceiling as adaptive retry bisected. Raise
  the ceiling before judging high-effort modes.
- Even at `high`, the cheap model captured 4/7 of the substantive facts against
  7/7 for a frontier model. Route mechanical sweeps and drafts cheap; keep
  extraction whose value is in the caveats on a frontier model.
