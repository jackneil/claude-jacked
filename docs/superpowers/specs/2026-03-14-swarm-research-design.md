# Swarm Research — Design Spec

## Problem

When approaching a non-trivial planning problem, a single agent produces a single line of reasoning. This creates blind spots — the agent's first instinct becomes the plan, with no pressure-testing against alternative approaches. `/swarm-research` solves this by spawning multiple independent research agents that approach the same problem from different angles, then synthesizing and pressure-testing their proposals before presenting a recommendation.

## How It Works

### Invocation

`/swarm-research` — no arguments required.

**Context detection:**
- If the conversation already contains a described problem, feature, or thing to build that hasn't been acted on yet, kick off immediately.
- If invoked before a problem is described, note that it's armed and tell the user to describe the problem. Kick off when they do.

### Phase 1: Divergent Research

**Agent count** is auto-calibrated based on problem complexity:

| Complexity | Agents | Signals |
|-----------|--------|---------|
| Simple/focused | 2 | Single component, clear scope |
| Moderate | 3 | Multiple components, some ambiguity |
| Significant | 4 | Architectural decision, multiple subsystems |
| Major/foundational | 5 | System-wide impact, many unknowns |

**Differentiation axes** — the skill picks the most useful mix per problem from three pools:

| Axis | Purpose | Examples |
|------|---------|---------|
| **Persona** | Different professional perspectives | "Security-first architect", "Ship-fast pragmatist", "Maintainability purist" |
| **Constraint** | Different optimization targets | "Minimize complexity", "Maximize extensibility", "Optimize for performance" |
| **Method** | Different research strategies | "Start from existing codebase patterns", "Start from first principles", "Research how open-source projects solve this" |

Each agent gets a unique combination. The skill selects which axes are most useful based on the problem type.

**Agent output format** — each returns a research brief:
- **Approach summary** (2-3 sentences)
- **Key decisions** and why
- **Trade-offs** acknowledged
- **Risks** identified
- **Confidence level** (high/medium/low with reasoning)

All agents can web search if it would help their research. Not mandatory.

All Phase 1 agents spawn in ONE message (parallel Agent tool calls).

### Synthesis

After all Phase 1 agents return, the parent synthesizes their proposals:

**Convergence analysis:**
- Where did agents agree? (Strong signal — multiple independent paths reached the same conclusion)
- Where did they diverge? (Interesting tension — worth examining why)
- What did only one agent surface that others missed? (Unique insights from their angle)

**Decision logic:**
- **Clear winner** — one proposal dominates (most agents converged, or it clearly addresses the trade-offs best). Form the plan from that proposal, incorporating unique insights from others.
- **Combination** — different agents got different parts right. Merge the best elements, noting which came from which angle.
- **No convergence** — agents genuinely disagree with comparable reasoning. Present the tension to the user and let them pick a direction (or combine elements) before proceeding to Phase 2.

Output: a **draft plan** — the recommended approach with key decisions, structured enough to be verified and attacked.

### Phase 2: Verify + Attack (Parallel)

Two agents spawn simultaneously against the draft plan:

**Verification agent:**
- Double-checks feasibility, completeness, and correctness
- Checks that the approach works with the existing codebase (reads relevant files)
- Identifies gaps — things the plan assumes but doesn't address
- Can web search to validate technical claims

**Devil's advocate agent:**
- Assumes the plan will fail and works backward to explain why
- Attacks the weakest assumptions and decisions
- Proposes the strongest counter-argument or alternative the research agents missed
- Goal is to break the plan, not to be helpful

**Merge & iterate:**
- Parent combines findings from both agents
- Updates the draft plan to incorporate valid critique and fill gaps
- **Significant changes** (structural, not just wording) → re-run Phase 2 against updated plan
- **Clean or minor tweaks** → finalize and present
- Safety cap: 3 Phase 2 rounds max. If still not converging, present current state with unresolved tensions noted and let the user decide.
- In practice converges in 1-2 rounds

### Final Output

```
## Swarm Research Complete

**Problem:** [one-line restatement]
**Agents spawned:** [N] researchers + verification + devil's advocate
**Rounds:** Phase 1 (research) → Synthesis → Phase 2 x[N] (verify + attack)

### Convergence Map
- [what agents agreed on]
- [where they diverged and how it was resolved]

### Recommendation
[The pressure-tested plan — approach, key decisions, trade-offs]

### Devil's Advocate Findings
- [critiques raised and how they were addressed or rebutted]

### Confidence
[High/Medium/Low — informed by convergence level, devil's advocate survival, Phase 2 rounds]
```

The skill does NOT auto-transition to implementation. The user decides next steps.

## Implementation

This will be implemented as:
- **SKILL.md** at `jacked/data/skills/swarm-research/SKILL.md` — lightweight router pointing to command
- **Command file** at `jacked/data/commands/swarm-research.md` — full orchestration runbook

Follows existing patterns from `/swarm`, `/dcr`, and `/ux`:
- Agents spawn via parallel Agent tool calls with `subagent_type: "general-purpose"`
- Research agents are read-only (they propose, not implement)
- Parent orchestrates synthesis, merge, and iteration
- All agents get full problem context in their prompt

## What This Is Not

- Not a replacement for `/swarm` (which is for parallel implementation)
- Not a replacement for `/dcr` (which is for parallel code review)
- Not a planning tool — it produces a researched recommendation, not an implementation plan
- Does not auto-invoke `/writing-plans` or any implementation skill
