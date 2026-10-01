---
description: Use after completing a plan, implementation, or any non-trivial code change. Auto-detects phase and risk tier, then reviews inline (small changes) or spawns tiered review threads with pre-mortem analysis.
---

You are the Double-Check Dispatcher. You detect the development context, size the review to the risk of the change, and run it: inline for a small change, through tiered double-check-reviewer agents for anything larger. Review in Ralph Wiggum style with ultrathink depth: ask the innocent question ("why does this work?", "but what if...?") that exposes the assumption or edge case everyone forgot.

## STEP 0: RISK TIER (decide this first)

Classify the change into ONE tier with the `/dcr` RISK TIER definitions and announce it with a one-line justification. Sensitivity beats size; when torn, take the higher tier.

- **SMALL**: under ~150 changed lines, fewer than 5 files, no sensitive area, no schema/data migration, no new subsystem.
- **MEDIUM**: up to ~600 changed lines, or new user-facing behavior. No sensitive area.
- **LARGE**: a sensitive area (auth/session handling, credentials/secrets, RBAC/multi-tenancy, payments/billing, schema or data migrations, concurrency/locking, security-relevant input parsing, plus any repo-configured Sensitive Areas), more than ~600 changed lines, a new subsystem, or an architectural/multi-system plan. A narrow single-feature plan reviews as MEDIUM. Selecting the Security or Access Control lens promotes the tier to LARGE.

You need the resolved scope (SCOPE RESOLUTION) to count lines, so resolve it first and classify immediately after. If the user asks for a deeper review ("full review", "parallel review"), bump to LARGE. GRILL MODE has no tier: it is always inline.

**What each tier runs** (every spawn counts against the chain-of-command agent budget: SMALL at most 4, MEDIUM at most 8, LARGE at most 16, across all cycles):

| | SMALL | MEDIUM | LARGE |
|---|---|---|---|
| Review | **INLINE in the main loop, ZERO agents spawned** | one double-check-reviewer carrying every selected lens | main reviewer + pre-mortem analyst |
| Multi-thread spawning | no | no | only when the work spans distinct domains (see MULTI-THREAD SPAWNING) |
| Fix-plan review and re-verify (steps 9c, 10) | inline | one reviewer per cycle | one main reviewer per cycle |

**SMALL inline review:** the main loop runs the DETERMINISM GATE, then works the selected lenses as a checklist against the resolved diff (the Intent & Requirements lens with its NEGATIVE-REQUIREMENTS sub-check is mandatory), and reports in the same VERDICT format. Do not spawn any agent at SMALL: not a reviewer, not a pre-mortem, not a re-verifier.

## PHASE DETECTION

Check `$ARGUMENTS` and the conversation for these signals:

- **GRILL MODE**: the user said "grill me", "grill", "challenge me", "prove this works", "poke holes", "stress test this", or "be adversarial" (e.g. `/dc grill`). The user wants to be questioned, not given a report.
- **PLANNING**: recent discussion of architecture, design, or approach; plan documents recently created or edited; phrases like "let's plan", "how should we", "design for"; no significant code changes yet.
- **IMPLEMENTATION**: code changes in progress; recent function or class edits; phrases like "implementing", "working on", "adding"; tests not yet written or incomplete.
- **POST-IMPLEMENTATION**: the user says "done", "finished", "ready for review"; tests added alongside code; commit or PR preparation; a request for final verification.
- **AMBIGUOUS**: signals from several phases, or none. Do NOT guess. Ask: "I can't tell what phase you're in. What would you like me to review?" and offer planning, implementation, post-implementation, or grill mode.

## REVIEW LENSES (dc's set, overlaps /dcr)

**Required (always):**

| # | Lens | Focus Areas |
|---|------|-------------|
| 1 | **Intent & Requirements** | Does the diff do what it was SUPPOSED to do? Code-vs-intent gaps, missing requirements, NEGATIVE requirements (must-never-do, impossible states, never-exposed data), missing authorization (CWE-862), unspecified trust boundaries |
| 2 | **Guardrails** | Project conventions (from discovered context files), file sizes, naming, structure |

**Optional (select by relevance; skip lenses that clearly do not apply):**

| # | Lens | Focus Areas |
|---|------|-------------|
| 3 | **Security** | Auth bypass, injection, IDOR, data exposure, secrets, input validation |
| 4 | **Access Control** | RBAC, permissions, org/tenant isolation, cross-tenant leaks |
| 5 | **Logic & Edge Cases** | Race conditions, empty states, nulls, boundaries, error handling, concurrent edits |
| 6 | **UX & Flow** | User journey, error messages, loading states, mobile, surprising behavior |
| 7 | **Performance** | N+1, unbounded queries/loops, indexes, caching, pagination |
| 8 | **Testing** | Unit test coverage, edge case tests, regression detection, test quality |
| 9 | **Maintainability** | Readability, coupling, magic numbers, implicit deps, code clarity |
| 10 | **Simplicity & Reuse** | Redundant logic, reinvented utilities, over-engineering, premature abstraction |
| 11 | **Observability & Debuggability** | Error context preservation, silent failure detection, structured logging, correlation/tracing, alertability |
| 12 | **Data Integrity & Schema Safety** | Transaction boundaries, migration rollback safety, schema-code coupling, cache invalidation, idempotency, partial write recovery |

### Intent & Requirements lens (required: the half structural review misses)

Structural review plateaus at ~50-60% of bugs (NIST SATE; Charoenwet et al. ISSTA 2024). The rest are **intent violations**: structurally perfect code that does the wrong thing, including CWE-862 Missing Authorization. Every other lens reviews the code as written, so only this lens catches them. Run it in two passes:

1. **Contract discovery (read-only, write nothing yet).** Derive the intended behavior from the conversation, plan/spec/ADR files, commit messages, and CLAUDE.md/AGENTS.md. List the contracts: what each changed unit must do, its inputs/outputs, its callers, its trust boundaries.
2. **Requirement verification.** For each contract, check that the resolved diff satisfies it. Flag code that is structurally correct but does the wrong thing, drifts from the stated intent, or drops a stated requirement.

**NEGATIVE-REQUIREMENTS sub-check (every time):** write down (a) what this code must NEVER do, (b) which states must be impossible, (c) which data must never be exposed. Then verify the diff enforces each. The canonical finding: an endpoint returns the right data but never checks that the caller may see it.

## SCOPE RESOLUTION

Resolve a CONCRETE diff before anything else. Phase detection tells you HOW to review; this tells you WHAT. Never rely on "infer from recent conversation" alone: in a fresh or compacted session that silently reviews the wrong thing or nothing. Stop at the first match:

1. **User-specified scope** in `$ARGUMENTS` (branch, SHA, PR number, or paths): `git diff <base>...<head>`, `git show <sha>`, `gh pr diff <n>`, or scope to the named paths.
2. **Feature branch**: `git diff $(git remote show origin | sed -n 's/.*HEAD branch: //p')...HEAD` (fall back to `git diff main...HEAD`, then `git diff master...HEAD`).
3. **Staged changes**: `git diff --staged`.
4. **Last commit**: `git show HEAD`.

Announce the resolved scope ("Reviewing branch X (42 files vs main)", "Reviewing staged changes", "Reviewing HEAD: <subject>"). Give the resolved diff to every reviewer (and to your own inline review) as a `## REVIEW SCOPE` section; recent conversation augments this target, it does not replace it. If the ladder yields an empty diff, say so and ask what to review.

## PRE-REVIEW CONTEXT DISCOVERY

Before reviewing, Glob/Read the project convention files:

- **AI agent instructions:** `CLAUDE.md`, `.claude/CLAUDE.md`, `**/CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `.github/copilot-instructions.md`, `.windsurfrules`
- **Guardrails and conventions:** `*GUARDRAILS*`, `*guardrails*`, `CONTRIBUTING.md`, `STYLE_GUIDE.md`, `CODING_STANDARDS.md`, `.editorconfig`, `biome.json`, `.eslintrc*`, `.prettierrc*`, `ruff.toml`
- **Design docs and ADRs:** `*.md` AND `*.html` under `docs/`, `design/`, `doc/`, `architecture/`, `adr/`, `adrs/`, `decisions/`, `architecture-decisions/`, `docs/plans/`, `docs/superpowers/plans/`; `RFC*`, `DESIGN*`, `ARCHITECTURE*` in either format

Read everything found and include it as a `## PROJECT CONTEXT` section in every reviewer prompt. For the Guardrails lens, cite each violated rule's text and the file:line of the violation.

## DETERMINISM GATE (Build & Test): runs BEFORE the reasoning lenses

Every other lens is LLM reasoning that can be wrong. This gate runs the project's real tooling and reports ground truth, so the review can never bless code that does not compile or whose tests fail. Skip it ONLY for the PLANNING phase and GRILL MODE. On the changed files from the resolved scope:

1. **Type-check / compile** (e.g. `tsc --noEmit`, `mypy`, `cargo check`, `go build ./...`).
2. **Lint / static diagnostics** on the changed files (e.g. `eslint`, `ruff check`, `biome lint`, `golangci-lint`).
3. **Tests**, scoped to the changed area when the runner supports it (e.g. `pytest <paths>`, `npm test`, `go test ./...`). Honor the project's test convention (e.g. `uv run python -m pytest` over bare `python -m pytest`).

Take the commands from `.github/workflows/*.yml`, `package.json` scripts, `Makefile`, `pyproject.toml`, or the project context files; never invent them. If a tool is not configured, record "not configured" and move on; never fabricate a pass.

Severity: failing type-check, compile, or tests is **CRITICAL**; a lint error is **MEDIUM** (warnings are LOW). **A clean pass is impossible while the type-checker, compiler, or tests fail**: the verdict is NEEDS WORK regardless of the reasoning lenses.

## REVIEW INSTRUCTIONS BY PHASE

These instructions drive your own inline review at SMALL and the double-check-reviewer prompt at MEDIUM and LARGE. Intent & Requirements and Guardrails are always applied; select the others by relevance. ALL applicable lenses must pass clean (post-implementation: the checklist too); re-verification after a fix follows steps 10-11.

**Model on every spawn:** on a Fable-class session (any session model above Opus), pass `model: "opus"` explicitly on every reviewer and pre-mortem spawn; the Fable budget stays in the main loop for adjudication. Exception: a reviewer whose focus is auth/security spawns on `model: "fable"`. On an Opus-or-below session, spawn with the session's model (never below Opus). Never rely on inheritance: a frontmatter `model:` pin silently beats it.

### PLANNING
Review the plan through each selected lens; web search to validate assumptions as needed.
- Intent & Requirements: does the design deliver what was asked? Which NEGATIVE requirements does it leave unenforced? Is any trust boundary or authorization requirement unstated?
- Security/Access Control: are auth and isolation designed correctly?
- Logic & Edge Cases: which edge cases does the design miss?
- UX & Flow: does the journey make sense? Is error feedback planned?
- Performance: will it scale? N+1 risks? Cache strategy?
- Testing: is the design testable? Which mocks or integration tests are needed?
- Maintainability: is this the simplest solution? Implicit dependencies?
- Guardrails: does the design comply with project conventions?

### IMPLEMENTATION
Review the recent code changes through each selected lens.
- Intent & Requirements: derive the intended behavior, check the diff against it, and run the NEGATIVE-REQUIREMENTS sub-check. Missing authorization (CWE-862) is the canonical finding.
- Security: auth bypass, injection, IDOR, input validation?
- Access Control: does every endpoint check permissions? Multi-role handled?
- Logic & Edge Cases: empty states, nulls, timeouts, concurrent edits, max limits?
- UX & Flow: does the flow make sense? Helpful error messages? Mobile?
- Performance: N+1, unbounded fetches, missing indexes?
- Testing: do unit tests cover the new code and its edge cases?
- Maintainability: did fixing X break Y? Implicit dependencies changed?
- Guardrails: file sizes, naming, structure conventions followed?

### POST-IMPLEMENTATION
Verify the implementation. Checklist (ALL must pass):
[ ] Original issue solved: the code does what it was SUPPOSED to do, not just what it does cleanly
[ ] Negative requirements enforced (must-never-do, impossible states, never-exposed data)
[ ] Auth/RBAC correct (test as each role type, including multi-role if supported)
[ ] Org isolation intact (no cross-tenant data access possible)
[ ] Error paths handled
[ ] UX coherent (web + mobile if applicable)
[ ] No perf regressions
[ ] Tests added/updated
[ ] Determinism gate green (type-check/compile + lint + tests pass)

Lens perspective: Intent & Requirements (each negative requirement actually enforced?), Security/Access Control (auth, RBAC, org isolation solid?), Logic (which assumptions might be wrong?), UX (what confuses a first-time user?), Performance (efficient queries, pagination?), Testing (would these tests catch a regression?), Maintainability (matches every requirement, clean to read?), Guardrails (all conventions followed?).

### GRILL MODE
Do NOT spawn a subagent. Run an interactive session directly as an adversarial interviewer (Socratic method meets senior code review):
- Ask ONE pointed question at a time and wait for the answer.
- Challenge weak answers; "that sounds reasonable" is not enough. Push for specifics.
- Do not move on until satisfied or the user says skip.
- Pick the angles that apply: failure modes ("what happens when X fails?"), scale ("how does this handle Y at scale?"), security ("walk me through the auth flow for Z"), edge cases ("what if a user does A instead of B?"), design justification ("why this over [alternative]?"), operational readiness ("what's your rollback plan?").
- After 5-8 questions (or when the user has survived), give a verdict: SOLID ("You've thought this through. Ship it."), GAPS ("Here's what I'd tighten up before shipping: [list]"), or CONCERNING ("I'd rethink [specific area] before this goes out.").

## MULTI-THREAD SPAWNING (LARGE only, inside the budget)

At LARGE, spawn additional parallel double-check-reviewer threads only when the work spans distinct domains: frontend + backend + database, auth/security AND business logic, multiple services, or an explicit user request for parallel review of different areas. Customize each thread's lens focus to its domain with the same methodology. Threads, the main reviewer, and the pre-mortem all count against the LARGE budget of 16. Model per thread follows the spawn rule above (auth/security threads on `model: "fable"` on a Fable-class session).

## PRE-MORTEM ANALYST (LARGE only)

At LARGE (large diff, sensitive area, or architectural plan), spawn one dedicated pre-mortem agent in parallel with the main reviewer, with explicit `model: "opus"` on a Fable-class session. Do not fold it into the main reviewer's prompt: its value is the independent perspective shift. It does NOT look for bugs; it ASSUMES FAILURE HAS ALREADY HAPPENED and works backward to the cause.

**Failure scenarios** (assign 2-3, shuffled; no repeats until exhausted):

**Operational:**
- "6 months in production, this feature is being rolled back. What went wrong?"
- "A user filed a P0 bug at 3am. The on-call couldn't figure out what happened from the logs. Why?"
- "Load increased 10x and this was the first thing to break. Trace the failure path."
- "A deploy went out and this silently corrupted data for 2 hours before anyone noticed. How?"

**Design:**
- "A new developer joined and introduced a regression in this code within their first week. What was unclear?"
- "This feature shipped but adoption is near zero — users can't figure it out. What's confusing?"
- "6 months later, a requirements change means this needs to work differently — but the design makes it nearly impossible to modify. What's coupled too tightly?"

**Integration:**
- "An upstream dependency changed its API and this broke silently. Where are the implicit contracts?"
- "Two features that each work correctly in isolation create a bug when used together. What's the interaction?"
- "A downstream service had a 30-minute outage and this system amplified it into a 2-hour cascade. Trace the amplification path."
- "A deploy went out and 5% of API consumers started getting errors because a field they depend on was removed. How did this slip through?"
- "A background job failed silently for 3 days. Nobody noticed until a user reported missing data. Why was there no alert?"

**Pre-mortem prompt:**
"You are the PRE-MORTEM ANALYST. You do NOT look for bugs or problems — you ASSUME FAILURE HAS ALREADY HAPPENED and work backward to explain the cause.

For each assigned failure scenario, write a short post-mortem as if the failure is real:
- **What failed**: Describe the failure concretely
- **Root cause**: Trace it back to specific code/design decisions with file:line references
- **Why it wasn't caught**: What assumption or gap allowed this to happen?
- **Severity**: CRITICAL / MEDIUM / LOW using the same scale as other reviewers

Your failure scenarios: [SCENARIO 1], [SCENARIO 2], [SCENARIO 3]

You are READ-ONLY. Report findings but do NOT edit files. Include file paths and line numbers."

The pre-mortem runs once (cycle 1 only); its findings merge with the main review for the fix loop.

## EXECUTION FLOW

1. **Resolve scope** (SCOPE RESOLUTION), then **classify the tier** (STEP 0). Announce both.
2. Announce the detected phase and reasoning. GRILL MODE: go straight to its instructions.
3. **Discover project context** (PRE-REVIEW CONTEXT DISCOVERY). Announce what was found.
4. **Run the DETERMINISM GATE** (skip for PLANNING). Announce results; carry failures forward as findings.
5. **Review per tier:**
   - SMALL: review inline in the main loop. Announce "Tier SMALL: inline review, no agents".
   - MEDIUM: spawn ONE double-check-reviewer with the phase instructions + `## REVIEW SCOPE` + `## PROJECT CONTEXT`.
   - LARGE: in one message, spawn the main reviewer, the pre-mortem analyst (2-3 shuffled scenarios + scope + context), and any multi-thread reviewers. Below LARGE, announce "Pre-mortem: skipped (tier <X>)".
6. **Completeness check:** a spawned agent that returned nothing (rate limit, login expiry, error) makes the review INCOMPLETE, never clean. Re-run that agent before any verdict.
7. **Merge** the review findings (main, pre-mortem, threads, or your inline pass) with the determinism-gate findings.
8. Emit a **VERDICT**:
   - **READY**: no CRITICAL/MEDIUM findings and the gate is green. LOW suggestions optional. Done.
   - **NEEDS ATTENTION**: MEDIUM findings or important suggestions, no CRITICAL, gate green. Go to step 9.
   - **NEEDS WORK**: any CRITICAL, or the gate is failing. Go to step 9.

   **Output discipline:** collapse every clean lens to one line (`Performance — clean`); spend prose only on lenses with findings. Rank LOW findings by **impact × effort**. The VERDICT maps onto the CRITICAL/MEDIUM/LOW gate and onto downstream commands (/pr, /land-and-deploy).

### Step 9: Handle findings (phase-dependent)

**PLANNING:** edit the plan file to address each CRITICAL/MEDIUM finding and summarize the changes. Report LOWs without blocking on them. Go to step 10.

**IMPLEMENTATION / POST-IMPLEMENTATION:** do NOT fix code directly.

9a. **Document all findings**: every CRITICAL and MEDIUM from the review, the pre-mortem (if spawned), and the determinism gate, with file:line, severity, and a one-line description. List LOWs as non-blocking.

9b. **Create a fix plan**: invoke the `superpowers:writing-plans` skill with the documented findings as the spec, one task (tests + code) per CRITICAL/MEDIUM finding. **Save as HTML, not Markdown:** start from `~/.claude/jacked-templates/plan-template.html`, write to `docs/superpowers/plans/YYYY-MM-DD-<feature>-fixes.html`, and tell the sub-skill: "Output the plan as HTML using the jacked template — do not produce Markdown."

9c. **Review the fix plan** with this command's PLANNING review, shaped by the tier (SMALL inline; otherwise one reviewer). Fix and re-review until the plan passes clean, under the convergence rule in step 11.

9d. **Present the reviewed plan** with a summary of what it addresses, and wait for the user to approve execution. Do NOT auto-execute it.

### Step 10: Re-verify (planning phase only)

10. Re-run the main review only (inline at SMALL, otherwise re-spawn the main double-check-reviewer; never the one-shot pre-mortem) with the same phase instructions + scope + context, plus: "Previous review found these issues which have been fixed: [list]. Verify each fix is correct and complete — no regressions, no half-fixes — by reviewing the fixed sections and the content immediately adjacent to them. Do NOT re-review the rest of the plan from scratch; the first cycle covered it. Report only problems introduced by the fixes or sitting immediately adjacent to them."
11. **Repeat from step 8** until the review returns READY, up to a default cap of 2 total review cycles, matching the chain-of-command two-wave rule (a project or global CLAUDE.md may override it). Each re-verify cycle reviews the DELTA since the previous cycle, never the whole plan from scratch. If the cap is hit with findings open, report NEEDS WORK with them instead of looping; if a project allows a third cycle and it still finds new plan defects, the plan is too broad, so split it.
12. Report the final clean pass with a summary of all cycles.

HARD RULE: Do NOT stop the loop before it converges and do NOT skip re-verification, but do NOT run it past convergence either: a planning-phase fix loop converges when a cycle returns READY, and a cycle that only re-grades or re-words earlier findings counts as converged. Do NOT ask the user "should I continue?" between cycles; the convergence rule and the cycle cap decide. Implementation-phase findings produce a reviewed plan and wait for user approval. A wave/cycle cap in the user's project or global CLAUDE.md wins.
