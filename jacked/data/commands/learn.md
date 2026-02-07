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

### Step 4: Write the Rule

Act confidently. Do NOT ask the user for permission — just write the rule.

- **APPEND-ONLY**: Add the rule to the end of the file. Never edit or remove existing rules.
- Default to the global `~/.claude/CLAUDE.md` unless the lesson is clearly project-specific, in which case use the project-level CLAUDE.md.
- If CLAUDE.md doesn't exist, create it with a header comment.
- If you spotted conflicting rules in Step 2, rewrite the conflicting rule to incorporate both (in-place edit, not duplicate).
- If the file has 50+ rules, suggest running `/audit-rules` to consolidate.
- If the lesson is graduating from `lessons.md`, remove or update the `lessons.md` entry after writing the CLAUDE.md rule.

### Step 5: Report What You Did

After writing, give a brief summary (1-2 lines) of what was added and where. Don't ask if it's OK — it's done.

## SAFETY RAILS

- NEVER invent lessons from nothing - if the conversation has no clear lesson, say so
- NEVER duplicate an existing rule - update the existing one instead
- ALWAYS default to global `~/.claude/CLAUDE.md` unless clearly project-specific
