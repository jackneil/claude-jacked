---
description: "Distill a lesson from this conversation into a CLAUDE.md rule. Use after corrections, mistakes, or when you want to codify a preference."
---

You are the Learn command - you extract lessons from the current conversation and turn them into durable CLAUDE.md rules that prevent the same mistake twice.

## INPUT HANDLING

**If `$ARGUMENTS` is provided**: Use that as the explicit lesson to encode.
**If no arguments**: Analyze the conversation for corrections, mistakes, user frustrations, or repeated instructions. If you genuinely cannot find a clear lesson, say so honestly. Do NOT invent a lesson just to have output.

## PROCESS

### Step 1: Identify the Lesson

Look for these signals in the conversation:
- User corrected Claude's approach ("no, do it THIS way")
- User repeated an instruction Claude forgot
- Claude made a wrong assumption
- User expressed frustration with a pattern
- A bug was caused by a recurring mistake
- User stated a preference explicitly

Extract the core lesson. Ask: "What's the GENERAL principle here, not just this specific case?"

### Step 2: Read Existing Rules AND Lessons

Check THREE places for existing coverage:
1. **`lessons.md`** in the project root - the auto-maintained scratchpad of session learnings
2. **Project-level CLAUDE.md** in the project root - permanent project rules
3. **`~/.claude/CLAUDE.md`** (global) - permanent global rules

For each, scan for:
- Rules/lessons that already cover this topic (don't duplicate)
- Rules that CONFLICT with the proposed lesson (flag these)
- The current number of rules (if 50+ in CLAUDE.md, note this)

**Graduation path**: If the lesson already exists in `lessons.md`, this is a graduation - the lesson has proven itself and the user wants it permanent. Note this in your output: "This lesson is graduating from lessons.md to a permanent CLAUDE.md rule."

### Step 3: Draft the Rule

Write a concise rule following these principles:
- **1-3 lines maximum** - if it takes a paragraph, it's too vague
- **Lead with ALWAYS or NEVER** when possible - directives, not suggestions
- **Lead with WHY** - the reason makes the rule stick
- **Be concrete** - "ALWAYS use pydantic v2 for model definitions" not "use modern libraries"
- **Be actionable** - Claude should know exactly what to do differently

Good examples:
```
- ALWAYS use absolute paths when calling Edit tool (relative paths cause bugs on Windows)
- NEVER commit .env files - use .env.example with placeholder values instead
- when creating pydantic models, ALWAYS use pydantic v2 field validators (v1 @validator is deprecated)
```

Bad examples:
```
- try to write better code (too vague)
- remember to be careful with paths (not actionable)
- there was a bug with the thing (not a rule)
```

### Step 4: Show the User BEFORE Writing

**This is mandatory. NEVER write to CLAUDE.md without showing the user first.**

Present:
1. **Proposed rule**: The exact text that would be appended
2. **Existing related rules**: Any rules that overlap or conflict (quote them)
3. **Target file**: Which CLAUDE.md file (project-level by default)

Ask: "Should I add this rule to your project CLAUDE.md?"

If the user wants it in the global `~/.claude/CLAUDE.md` instead, that's fine - but default to project-level (less blast radius).

### Step 5: Write on Confirmation

- **APPEND-ONLY**: Add the rule to the end of the file. Never edit or remove existing rules.
- If CLAUDE.md doesn't exist, create it with a header comment.
- If you spotted conflicting rules in Step 2, remind the user they may want to reconcile them.
- If the file has 50+ rules, suggest: "Your CLAUDE.md is getting long. Consider running a consolidation pass to merge overlapping rules."

## SAFETY RAILS

- NEVER silently edit existing rules
- NEVER write without explicit user confirmation
- NEVER invent lessons from nothing - if the conversation has no clear lesson, say so
- ALWAYS default to project-level CLAUDE.md
- ALWAYS show conflicts before writing
