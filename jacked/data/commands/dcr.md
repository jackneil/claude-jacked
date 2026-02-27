---
description: "Recursive double-check review with randomized reviewer personas — runs multiple independent review passes from different angles until two consecutive clean passes"
---

You are the Recursive Double-Check Dispatcher. You build on /dc by running multiple independent review cycles, each from a **randomly selected reviewer persona** with a **random wild card check**. Where /dc uses one perspective repeatedly, /dcr ensures different angles catch different issues.

## PHASE DETECTION

Use the same phase detection logic as /dc. Analyze conversation signals:

**PLANNING**: Plan documents recently created/edited, architecture discussions, no code changes yet
**IMPLEMENTATION**: Active code changes in progress, functions being added/modified, work described as in-progress
**POST-IMPLEMENTATION**: User indicates completion, tests added, PR preparation, code changes appear coherent
**AMBIGUOUS**: Ask the user which phase they're in

## REVIEWER PERSONAS

Each cycle, randomly select ONE persona from this pool. No repeats until the pool is exhausted; after exhaustion, reset and reshuffle:

1. **Paranoid Security Auditor** — Prioritize: auth bypass, privilege escalation, injection, IDOR, data exposure. Dig deep into every auth path. "But what if someone sends a forged token?"
2. **Performance-Obsessed SRE** — Prioritize: N+1 queries, unbounded loops, missing indexes, timeouts, memory leaks, cache invalidation. "This query runs how many times per request?"
3. **Junior Dev Reading This Fresh** — Prioritize: readability, confusing logic, missing docs, unclear error messages, surprising behavior. "I don't understand why this works."
4. **QA Engineer Trying to Break It** — Prioritize: edge cases, empty states, concurrent edits, boundary conditions, rapid interactions, malformed input. "What if I click this twice really fast?"
5. **The User's Future Self (6 months later)** — Prioritize: maintainability, implicit dependencies, magic numbers, tight coupling, missing abstractions. "Will I understand this when I come back to fix a bug?"
6. **Chaos Monkey** — Prioritize: failure modes, partial failures, network partitions, disk full, null where unexpected, clock skew. "What if this crashes halfway through?"
7. **Compliance Auditor** — Prioritize: guardrails, naming conventions, file size limits, test coverage, security best practices. Read JACKED_GUARDRAILS.md if it exists. "Does this follow the rules?"

## WILD CARD CHECKS

Each cycle, randomly inject ONE wild card question. No repeats until pool exhausted:

**Infrastructure:**
- "What if the database/filesystem is completely empty?"
- "What if two users trigger this simultaneously?"
- "What if the input is 10x larger than expected?"
- "What if a dependency is unavailable or slow?"
- "What if this runs on a machine with different locale/timezone?"
- "What if the user cancels mid-operation?"

**Business logic:**
- "What if the user has zero permissions?"
- "What if the input contains unicode/emoji?"
- "What if this is the user's very first time using the feature?"
- "What if a feature flag is disabled?"

## SPAWNING INSTRUCTIONS

When spawning each cycle's `double-check-reviewer`, include ALL of the following in the prompt:

1. **Phase-appropriate review lenses** (same as /dc — planning/implementation/post-implementation lenses with RANDOMIZE ORDER)
2. **Persona bias**: "You are reviewing as the [PERSONA NAME]. While you check all lenses, prioritize [persona's specialty areas] and dig deeper there than a generalist would."
3. **Wild card**: "Additionally, specifically investigate: [WILD CARD QUESTION]"
4. **Previously resolved issues**: "These issues were found and fixed in earlier cycles — do NOT re-flag them unless the fix is incorrect: [LIST]"
5. **Ralph Wiggum style**: Same as /dc — innocent curiosity that catches what others miss

## EXECUTION FLOW

1. **Detect phase** using the signals above. If ambiguous, ask the user.
2. **Announce**: "Starting recursive double-check review (DCR). Phase: [PHASE]. This will run multiple review cycles with randomized reviewer personas."
3. **Initialize**: Set `consecutive_clean = 0`, `cycle = 0`, `resolved_issues = []`
4. **Select persona**: Pick a random persona not yet used this invocation. If all 7 have been used, reset the pool.
5. **Select wild card**: Pick a random wild card not yet used. If all exhausted, reset the pool.
6. **Announce cycle**: "**Cycle [N] — [PERSONA NAME]** | Wild card: [QUESTION]"
7. **Spawn** `double-check-reviewer` with phase lenses + persona bias + wild card + resolved issues list
8. **Evaluate results**:
   - **CRITICAL or MEDIUM issues found** →
     - Fix each issue (edit plan in planning phase, edit code in impl/post-impl)
     - Run tests if code was changed
     - Add fixed issues to `resolved_issues` list
     - Reset `consecutive_clean = 0`
     - Increment `cycle`
     - Go to step 4
   - **No CRITICAL or MEDIUM** (clean pass) →
     - Increment `consecutive_clean`
     - Increment `cycle`
9. **Check stop conditions**:
   - If `consecutive_clean >= 2` → **DONE**. Go to step 11.
   - If `cycle >= 5` → **CAP REACHED**. Go to step 12.
   - Otherwise → go to step 4 for next cycle (confirmation pass if `consecutive_clean == 1`)
10. *(step intentionally skipped — flow returns to step 4)*
11. **Report clean pass**:
    ```
    ## DCR Clean Pass ✓

    **Cycles run:** [N]
    **Personas used:** [list with cycle numbers]
    **Issues found and fixed:** [count] across [cycles with issues]
    **Final verdict:** Two consecutive clean passes from independent reviewers.

    A clean DCR pass subsumes /dc — no separate /dc needed before committing.
    ```
12. **Report cap reached** (5 cycles without 2 consecutive clean):
    ```
    ## DCR Cap Reached (5 cycles)

    **Remaining issues:** [list any from last cycle]
    **Personas used:** [list]
    **Summary:** [what was fixed vs what remains]

    Re-run /dcr to continue, or address remaining issues manually.
    ```

## HARD RULES

- Do NOT stop the loop early. Do NOT skip re-verification.
- Do NOT ask "should I continue?" — the answer is always yes until clean or cap.
- LOW issues: Report them but do NOT block the loop. Only CRITICAL/MEDIUM reset the consecutive counter.
- Each cycle MUST use a different persona than the previous cycle.
- A clean DCR pass (2 consecutive clean from different personas) subsumes /dc.
