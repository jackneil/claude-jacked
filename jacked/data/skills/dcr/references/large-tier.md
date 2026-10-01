# /dcr LARGE-tier material

Read this file only when the RISK TIER is LARGE. SMALL and MEDIUM runs never use it: their reviewers get no persona, no wild card, and no pre-mortem analyst.

Everything here still runs inside the LARGE agent budget (at most 16 agents for the whole run) and the wave cap from `## BUDGET FIRST` in SKILL.md.

## REVIEWER PERSONAS (LARGE tier only)

At the LARGE tier, each Wave-1 reviewer gets a different persona. Shuffle the pool; no repeats until exhausted, then reset. Each Wave-1 reviewer MUST have a different persona AND a different wild card. Re-check waves (wave 2+) get no persona.

1. **Paranoid Security Auditor** — "But what if someone sends a forged token?"
2. **Performance-Obsessed SRE** — "This query runs how many times per request?"
3. **Junior Dev Reading This Fresh** — "I don't understand why this works."
4. **QA Engineer Trying to Break It** — "What if I click this twice really fast?"
5. **The User's Future Self (6 months later)** — "Will I understand this when I come back to fix a bug?"
6. **Chaos Monkey** — "What if this crashes halfway through?"
7. **Compliance Auditor** — "Does this follow the rules?"
8. **On-Call SRE at 3am** — "Can I figure out what happened from the logs?"
9. **Database Migration Veteran** — "What happens to existing data when this deploys?"

Persona prompt line (SPAWNING INSTRUCTIONS item 5, Wave 1 only): "You are reviewing as the [PERSONA NAME]. Your persona shapes HOW you evaluate your assigned lenses — dig deeper where your persona's instincts apply."

## WILD CARD CHECKS (LARGE tier only)

At the LARGE tier, each Wave-1 reviewer gets a different wild card. Shuffle the pool; no repeats until exhausted, then reset. Add any repo-configured **Domain Wild Cards** (Config Override) to this pool before shuffling.

**Infrastructure:**
- "What if the database/filesystem is completely empty?"
- "What if two users trigger this simultaneously?"
- "What if the input is 10x larger than expected?"
- "What if a dependency is unavailable or slow?"
- "What if this runs on a machine with different locale/timezone?"
- "What if the user cancels mid-operation?"
- "What if this external call times out? Is the timeout configured? What's the retry strategy?"
- "If this service's dependency goes down, does the failure cascade or degrade gracefully?"
- "What if this operation partially completes and the process crashes — what state is the data in?"

**Business logic:**
- "What if the user has zero permissions?"
- "What if the input contains unicode/emoji?"
- "What if this is the user's very first time using the feature?"
- "What if a feature flag is disabled?"
- "Can a first-time user find this feature from the natural entry point without reading docs or tooltips?"
- "What if the fix silently changes behavior that users are already trained to expect — do they notice, and does it help or confuse them?"

**Observability & data:**
- "Something broke in production at 3am — can the on-call diagnose it from logs alone, without reading source code?"
- "If this write fails halfway, what state is the data in? Can you tell from the logs what succeeded and what didn't?"

Wild-card prompt line (SPAWNING INSTRUCTIONS item 6, Wave 1 only): "Additionally, specifically investigate: [WILD CARD QUESTION]"

The conditional Frontend Design reviewer also gets a persona and a wild card at the LARGE tier.

## PRE-MORTEM FAILURE SCENARIOS (LARGE tier only)

The pre-mortem analyst gets 2-3 scenarios from this pool (shuffled; no repeats until exhausted, then reset). Add any repo-configured **Domain Pre-Mortem Scenarios** (Config Override) to this pool before shuffling.

**Operational:**
- "6 months in production, this feature is being rolled back. What went wrong?"
- "A user filed a P0 bug at 3am. The on-call couldn't figure out what happened from the logs. Why?"
- "Load increased 10x and this was the first thing to break. Trace the failure path."
- "A deploy went out and this silently corrupted data for 2 hours before anyone noticed. How?"

**Design:**
- "A new developer joined and introduced a regression in this code within their first week. What was unclear?"
- "This feature shipped but adoption is near zero — users can't figure it out. What's confusing?"
- "6 months later, a requirements change means this needs to work differently — but the design makes it nearly impossible to modify. What's coupled too tightly?"
- "A user filed a bug saying the feature 'disappeared' — it still exists but they can no longer find it after this change. What moved or changed that broke their muscle memory?"

**Integration:**
- "An upstream dependency changed its API and this broke silently. Where are the implicit contracts?"
- "Two features that each work correctly in isolation create a bug when used together. What's the interaction?"
- "A downstream service had a 30-minute outage and this system amplified it into a 2-hour cascade. Trace the amplification path."
- "A deploy went out and 5% of API consumers started getting errors because a field they depend on was removed. How did this slip through?"
- "A background job failed silently for 3 days. Nobody noticed until a user reported missing data. Why was there no alert?"

## PRE-MORTEM ANALYST (LARGE tier, Wave 1 only - a dedicated agent; on a Fable-class session it dispatches on `model: "opus"` like the other volume reviewers)

At the LARGE tier, spawn one additional, dedicated reviewer as the pre-mortem analyst in the SAME message as all other Wave 1 reviewers. Do not fold it into another reviewer's prompt: its value is the independent perspective shift.

- Use `subagent_type: "double-check-reviewer"` (or general-purpose with pre-mortem instructions).
- Pass the model explicitly: `model: "opus"` on a Fable-class session; on an Opus-or-below session, the session's model (never below Opus).
- Assign 2-3 shuffled failure scenarios from the PRE-MORTEM FAILURE SCENARIOS pool.
- Include the analyst prompt below (SPAWNING INSTRUCTIONS item 10), plus the PROJECT_CONTEXT block, the phase context, the evidence requirement, the DO NOT FLAG list, and the scope/provenance instruction.
- It reports in the standard CRITICAL/MEDIUM/LOW format. Its findings go through FINDING VALIDATION and the normal fix phase.
- It does NOT re-spawn in later waves. It is one-shot in Wave 1: its value is the initial perspective shift, not iterative verification.
- With the Codex engine active, it runs as a Codex job like the other non-carve-out reviewers (see references/codex-engine.md).

Analyst prompt (item 10):

    "You are the PRE-MORTEM ANALYST. You do NOT look for bugs or problems — you ASSUME FAILURE HAS ALREADY HAPPENED and work backward to explain the cause. This is a fundamentally different evaluation framework from the other reviewers.

    For each assigned failure scenario, write a short post-mortem as if the failure is real:
    - **What failed**: Describe the failure concretely
    - **Root cause**: Trace it back to specific code/design decisions with file:line references
    - **Why it wasn't caught**: What assumption or gap allowed this to happen?
    - **Severity**: CRITICAL / MEDIUM / LOW using the same scale as other reviewers

    Your failure scenarios: [SCENARIO 1], [SCENARIO 2], [SCENARIO 3]

    You are READ-ONLY. Report findings but do NOT edit files. Include file paths and line numbers."

## LARGE Wave-1 announcement

```
**Wave 1 LARGE — [N] lenses across [M] reviewers + Pre-Mortem Analyst**
- Reviewer A ([PERSONA]): [Lens X] + [Lens Y] | Wild card: [Q1]
- Reviewer B ([PERSONA]): [Lens Z] + [Lens W] | Wild card: [Q2]
...
- Pre-Mortem Analyst: [2-3 failure scenarios from pool]
```

In the final report, each lens line carries its reviewer's persona, e.g. `✓ Logic & Edge Cases — Wave 1 (Chaos Monkey), rechecked Wave 2 (1 issue fixed)`.
