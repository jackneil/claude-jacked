---
description: Use when an approach has gone sideways and patching patches. Scraps the current approach and re-implements from scratch with full hindsight after structured reflection.
---

You are the Redo command - you force a clean-slate re-implementation when the current approach has gone sideways. You don't just "try again" - you preserve old work safely, reflect on what went wrong, and redesign from first principles.

## PREREQUISITE CHECK

Before anything else:
1. **Verify you're in a git repository.** Run `git status`. If it fails (not a git repo), stop and tell the user: "/redo requires a git repository to safely preserve your work."
2. **Verify there IS something to redo.** Check git status and recent conversation for implementation work. If nothing has been implemented yet (just planning/discussion), say: "Nothing to redo yet - there's no implementation to scrap. Try entering plan mode to design your approach first."
3. **Decide whether a full redo is even the right move.** Before scrapping everything, gauge how localized the failure is:
   - If the mess is concentrated in one module/file/function, a targeted fix - or a redo of just that failing slice - will usually beat a full re-implementation. Offer the narrower option and let the user choose before you scrap the whole thing.
   - If the failure is pervasive (the whole approach/architecture is wrong, the abstractions fight you everywhere), a full scrap-and-redo is justified - proceed.
   - The rewrite literature's clearest refactor-over-rewrite signal is a localized problem. Don't reach for the biggest hammer when a smaller one fixes it.
4. If the user provides `$ARGUMENTS`, treat that as context for WHAT to redo

## PROCESS

### Step 1: Preserve Current Work

**This is non-negotiable. NEVER destroy work without saving it first.**

1. Run `git status` to check for uncommitted changes.
2. If there are uncommitted changes, stash them:
   - Run `git stash push -m "redo: stashed work before re-implementation"`
   - If the stash succeeds, tell the user: "Your current work is stashed. Run `git stash pop` if you want it back."
   - If the stash command fails (exit code non-zero), **STOP** and tell the user the stash failed - do not proceed without preserving their work.
   - If there's nothing to stash (clean working tree), that's fine - proceed. Already-committed work is safe in git history.

### Step 2: Capture Current Behavior (regression baseline)

**Before you scrap anything, pin down what the old approach already got working.** A clean-slate redo that doesn't know what "working" looked like will silently regress behavior and rediscover solved bugs the hard way. This is your safety net and your final "is the redo actually better?" check - it turns that question from a vibe into evidence.

1. **If a test suite exists**, run it against the old approach while it's still accessible (current tree, or the old branch/commit) and record which tests pass. That green set is your regression baseline - the redo must reproduce it.
2. **If there's no test suite**, write a short characterization checklist: the key inputs and the outputs/behaviors the old code actually produced (happy path plus any edge cases it handled). Capture observed behavior, not intended behavior.
3. Keep this baseline - you will verify the redo against it in Step 7 before recommending the user merge.

### Step 3: Create a Redo Branch

Create a new branch for the clean re-implementation. Follow the user's branch naming conventions if specified in their CLAUDE.md (e.g., naming patterns, date formats). If no convention is specified, use a descriptive name like `redo-<feature>`.

This gives you a clean canvas while keeping the old approach accessible.

### Step 4: Structured Reflection (MANDATORY)

**You MUST complete this reflection BEFORE writing any new code.** No skipping, no shortcuts.

Answer these four questions explicitly:

1. **What was the original goal?**
   - Strip away implementation details. What were we actually trying to achieve?

2. **What went wrong with the previous approach?**
   - Be specific. "It got messy" is not an answer.
   - What decisions led to the mess? What assumptions were wrong?

3. **What do we know now that we didn't know before?**
   - Constraints discovered during implementation
   - Edge cases that weren't obvious upfront
   - API behaviors, library limitations, data quirks

4. **What should the new approach account for?**
   - List the gotchas and constraints from questions 2 and 3
   - These become requirements for the redesign

**Salvage pass - harvest what the old code got RIGHT (do this before scrapping):**

Reflecting on what went *wrong* is only half the job. The old implementation also encodes hard-won knowledge, and reproducing that behavior is the expensive part of any rewrite. Before you throw it away, inventory what it got *right*:
- Edge cases it handled, and the odd-looking conditionals that turned out to be bug fixes for real problems
- Input validations, guards, and defensive checks
- Non-obvious behaviors, and workarounds for API/library/data quirks - anything that looks ugly but exists for a reason

Treat those ugly lines as solved problems, not noise: a blind clean-slate redo rediscovers every one of those bugs the hard way. Fold each salvaged item into your question-4 list so the redesign is required to preserve it.

Present this reflection to the user. They need to see it and confirm it captures the situation.

### Step 5: Redesign

Enter plan mode. Design the new solution from first principles using the reflection above as input.

Key mindset shifts for the redesign:
- **Simplest thing that works** - fight the urge to over-engineer
- **Same goal, at feature parity** - the redo targets the SAME goal the old code did, and must reproduce the behaviors you captured in Step 2 and salvaged in Step 4. Hitting parity is the bar.
- **Address the actual failure points** - don't just rearrange the same bad approach
- **Use the hindsight** - you have information the first attempt didn't have
- **Consider if the goal itself needs adjusting** - sometimes the right redo is changing WHAT, not HOW

**Scope discipline - beware the Second-System Effect.** A redo carries a parity floor plus an irresistible "while we're at it, let's also improve..." pull, and that pull is exactly how redos balloon into stalled rewrites. This redo does ONE thing: re-implement the original goal, better. It must NOT absorb a deferred wish-list. Any new feature or improvement that wasn't in the original scope goes on a separate "after the redo lands" list - not into this redo.

### Step 6: Implement

Only after the plan is approved, implement the new solution on the redo branch.

### Step 7: Verify Against Captured Behavior (gate)

**Do not declare the redo done - or recommend the user merge it - until it provably reproduces the baseline from Step 2.** Agent-driven redos are exactly where silent regressions and hallucinated "fixes" hide, so this gate is non-negotiable:
- If you captured a test suite: run it against the redo. Every test that passed on the old approach must pass on the redo. Investigate and resolve any that don't - a parity regression is a failure, not a footnote.
- If you captured a characterization checklist: exercise the redo against each item and confirm it produces the same outputs/behaviors.
- Report the result as evidence ("baseline: N tests / M behaviors; redo: all reproduced") - not a vibe. If something can't be reproduced, surface it loudly and decide with the user before merging.

### Step 8: Wrap Up

Tell the user:
- Their old work is in `git stash` (if it was stashed)
- They're on a new branch
- The redo reproduces the captured baseline (cite the Step 7 evidence)
- They can compare approaches with `git diff main..HEAD` (or whatever the base branch is)
- If the redo is better, they can merge it. If not, they can switch back.

## SAFETY RAILS

- ALWAYS stash/preserve before doing anything destructive
- ALWAYS create a new branch - never redo on the same branch
- ALWAYS capture the old approach's working behavior before scrapping, and verify the redo reproduces it before recommending a merge
- ALWAYS complete the reflection - including the salvage pass that harvests what the old code got right - before writing code
- NEVER skip the reflection step even if the user says "just redo it"
- NEVER let the redo absorb new scope: parity first; wish-list items go on a separate "after the redo" list
- PREFER a targeted fix or a single-slice redo when the failure is localized; reserve full scrap for pervasive failures
- If `git stash` fails for some reason, STOP and tell the user - don't proceed without preserving their work
