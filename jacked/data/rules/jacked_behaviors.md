# jacked-behaviors-v2
- At the start of a session, read `lessons.md` in the project root if it exists. These are lessons from past sessions - apply them silently.
- After any correction or repeated instruction from the user, read `lessons.md` first. If the lesson is already there but you made the same mistake again, the existing lesson wasn't strong enough - rewrite it sharper with more specificity and context from this failure. If a lesson has been rewritten twice (three total failures on the same concept), it needs to graduate to a permanent CLAUDE.md rule - suggest /learn. If the lesson is genuinely new, append a 1-2 line entry. Create the file if needed.
- Before marking non-trivial work complete, run /dc to verify it actually works
- When an approach has gone sideways and you're patching patches, suggest /redo to scrap and re-implement cleanly
- Periodically during long sessions, suggest /techdebt to scan for debt accumulating in the codebase
- After adding several rules to CLAUDE.md, suggest /audit-rules to check for duplicates and contradictions
- When searching for context from past sessions, use /jacked to search semantic memory before re-exploring from scratch
- For non-trivial tasks (3+ steps or architectural decisions), enter plan mode first. When a fix feels hacky, step back and redesign.
- Never mark a task complete without proving it works - run tests, check logs, demonstrate correctness
# end-jacked-behaviors
