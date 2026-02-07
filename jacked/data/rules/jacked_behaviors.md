# jacked-behaviors-v2
- At the start of a session, read `lessons.md` in the project root if it exists. These are lessons from past sessions - apply them silently.
- After any correction or repeated instruction from the user, check project CLAUDE.md first — if a matching rule already exists there, improve it in place (sharper wording, more context from this failure) and do NOT duplicate it in lessons.md. Otherwise, read lessons.md. If the lesson is already there, increment its strike counter prefix (e.g. [2x], [3x]) and rewrite it sharper. At [3x] or higher, suggest /learn to graduate it to a permanent CLAUDE.md rule. If the lesson is genuinely new, append a 1-2 line entry prefixed with [1x]. Create lessons.md if needed.
- Before marking non-trivial work complete, run /dc to verify it actually works
- When an approach has gone sideways and you're patching patches, suggest /redo to scrap and re-implement cleanly
- Periodically during long sessions, suggest /techdebt to scan for debt accumulating in the codebase
- After adding several rules to CLAUDE.md, suggest /audit-rules to check for duplicates and contradictions
- When searching for context from past sessions, use /jacked to search semantic memory before re-exploring from scratch
- For non-trivial tasks (3+ steps or architectural decisions), enter plan mode first. When a fix feels hacky, step back and redesign.
- Never mark a task complete without proving it works - run tests, check logs, demonstrate correctness
- When the user asks about jacked features, gatekeeper, logs, installation, or troubleshooting, read ~/.claude/jacked-reference.md for comprehensive details before answering.
- At the start of a session, run `jacked check-version` to see if a newer version is available. If outdated, mention it to the user.
# end-jacked-behaviors
