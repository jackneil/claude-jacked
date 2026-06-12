---
name: jack-it-up
description: Use when starting any significant feature or non-trivial task that deserves a thorough development cycle. Triggers on "jack it up", "do this right", "full cycle", "build this properly", or when the user wants quality-first development over speed.
---

# Jack It Up

Iterative development cycle that prioritizes getting it right over getting it done. Each phase builds on the last, and review cycles continue until the work scores a 10/10 — not just functional, but polished.

## The Mindset

The goal is NOT to finish the work. The goal is to do the work perfectly.

```dot
digraph mindset {
    "Just ship it" [shape=box, style=filled, fillcolor=lightcoral, label="Getting it done\n(wrong mindset)"];
    "Is this excellent?" [shape=diamond];
    "Ship it" [shape=box, style=filled, fillcolor=lightgreen, label="Ship it\n(right mindset)"];
    "Refine" [shape=box, label="Refine further"];

    "Is this excellent?" -> "Ship it" [label="yes — genuinely"];
    "Is this excellent?" -> "Refine" [label="no"];
    "Refine" -> "Is this excellent?";
}
```

Be inquisitive, not just task-completing. Ask "is this the best way?" at every stage. This is NOT scope creep — it is thoroughness. The difference: scope creep adds features nobody asked for; thoroughness ensures the requested features work flawlessly.

## Red Flags — Stop and Refocus

These thoughts mean the work is drifting toward "just get it done":

| Thought | What to do instead |
|---------|-------------------|
| "This is good enough" | Run /dc. If it finds issues, it's not good enough. |
| "Let me skip the review, it's simple" | Simple things break in subtle ways. Review it. |
| "I'll fix that later" | Fix it now. "Later" means "never." |
| "The tests pass, we're done" | Tests passing is the minimum. Review quality, not just correctness. |
| "This review cycle is overkill" | The review found issues last time. Trust the process. |

## The Cycle

```dot
digraph cycle {
    rankdir=TB;
    node [shape=box];

    brainstorm [label="1. Brainstorm\n(superpowers:brainstorming)"];
    plan [label="2. Write Plan\n(superpowers:writing-plans)"];
    review_plan [label="3. Review Plan\n(/dc on plan)"];
    execute [label="4. Execute Plan\n(superpowers:subagent-driven-development)"];
    review_impl [label="5. Double-Check Review\n(/dc on implementation)"];
    clean [shape=diamond, label="Clean pass?"];
    ship [label="6. Ship It\n(/pr)"];
    done [label="PR created", shape=doublecircle];

    brainstorm -> plan;
    plan -> review_plan;
    review_plan -> execute [label="plan passes"];
    review_plan -> plan [label="plan has issues\n(fix and re-review)"];
    execute -> review_impl;
    review_impl -> clean;
    clean -> ship [label="yes"];
    clean -> plan [label="no — findings become\nspec for next plan"];
    ship -> done;
}
```

### Phase 1: Brainstorm

**REQUIRED SUB-SKILL:** `superpowers:brainstorming`

Explore the user's intent, requirements, and design space before touching code. Do not assume the first idea is the right one. Ask questions. Challenge assumptions. Consider alternatives.

Output: A clear understanding of what to build and why.

**Lens awareness:** Before presenting the design, check for installed specialist lenses:

```bash
ls ~/.claude/lenses/*.md .claude/lenses/*.md 2>/dev/null
```

If lenses exist, read their frontmatter (name, description, triggers). If any lens triggers match the feature being brainstormed (e.g., building UI → accessibility lens, building API → api-ergonomics lens), surface relevant design considerations:

> "The **{lens.name}** lens suggests considering: {2-3 key items from the lens's 'What to check' section relevant to this feature}"

This is informational only — it doesn't block or change the brainstorm flow. It ensures specialist concerns are raised during design rather than caught late in review.

### Phase 2: Write Plan

**REQUIRED SUB-SKILL:** `superpowers:writing-plans`

Turn the brainstorm output into a concrete, task-by-task implementation plan with complete code, exact file paths, test commands, and commit messages. No placeholders. No "TBD."

**Output format: HTML, not Markdown.** When you invoke `superpowers:writing-plans`, **explicitly instruct the sub-skill in its prompt**:

> "Write this plan as HTML using the template at `~/.claude/jacked-templates/plan-template.html`. Output `.html`, not `.md`. Save to `docs/superpowers/plans/{YYYY-MM-DD}-{slug}.html`. Do not produce Markdown."

The sub-skill's default is Markdown — without this explicit override, you'll get `.md`. The template has placeholders for goal, architecture (Mermaid diagrams), file structure, tasks (as `<ul class="tasks">` checklists), and open questions.

Why HTML: plans are artifacts the human re-reads during execution. Markdown opened locally is a wall of text. HTML renders diagrams, styles tables and code, supports print/PDF. These files never go to GitHub's web UI, so Markdown's only advantage doesn't apply.

### Phase 3: Review the Plan

Invoke `/dc` (which auto-detects planning phase). The double-check review spawns reviewers and a pre-mortem analyst to stress-test the plan.

- If CRITICAL or MEDIUM issues found → fix the plan, re-review until clean.
- Do NOT proceed to execution with an unreviewed or failing plan.

Output: A reviewed, clean plan.

### Phase 4: Execute the Plan

**REQUIRED SUB-SKILL:** `superpowers:subagent-driven-development`

Always use subagent-driven development — a fresh subagent per task with two-stage review (spec compliance, then code quality). Do not use `superpowers:executing-plans` (that is the inline fallback for environments without subagent support).

Implement the plan task by task. Each task follows TDD (test first, implement, verify). Commit after each task.

Output: Working implementation with passing tests.

### Phase 5: Double-Check Review

Invoke `/dc` (which auto-detects implementation/post-implementation phase). The review:

1. Captures ALL gaps, issues, and problems found across every lens
2. Documents findings as a structured list with file:line references and severity
3. Invokes `superpowers:writing-plans` to turn findings into a fix plan
4. Reviews that fix plan before presenting it

- If the review passes clean → done. Ship it.
- If findings exist → the fix plan becomes the input for a new Phase 4 (execute) → Phase 5 (review) cycle.

### The Loop

Phases 4 and 5 repeat until a clean pass. Each cycle:
- Narrows the issue space (fewer findings each round)
- Increases confidence (more lenses pass clean)
- Converges toward 10/10 quality

Do NOT declare "done" until the final /dc review passes with no CRITICAL or MEDIUM findings.

### Phase 6: Ship It

Invoke `/pr` to create or update the pull request. The `/pr` command runs the `pr-workflow-checker` agent which now includes a **pre-flight verification** phase that automatically checks for:

- Stale stashes (verifies changes are already in HEAD before suggesting drop)
- Stale worktrees (verifies branch is merged and clean before suggesting removal)
- Untracked files that should be committed or gitignored
- Local branches tracking deleted remotes
- Memory freshness (MEMORY.md open PRs, test counts, known issues vs reality)

The pre-flight **never auto-cleans** — it reports findings with proof of what's safe to clean and what needs attention, then asks. NEEDS ATTENTION items are warnings, not blockers.

After pre-flight, the agent handles PR creation with issue linking and a comprehensive description.

Output: PR URL. The cycle is complete.

## When NOT to Use This

- **Trivial one-line fixes** — just make the change
- **Exploratory prototyping** — brainstorm is enough, skip the full cycle
- **User explicitly asks for speed over quality** — respect the request

## Integration with /dc

The `/dc` skill already implements the findings-to-plan pipeline for implementation reviews (Phase 5). This skill orchestrates the full cycle around it. When `/dc` produces a reviewed fix plan, this skill picks it up and executes it, then runs `/dc` again.
