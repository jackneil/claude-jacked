---
description: Route a durable fact the user asked to remember into the jacked memory vault as a typed, cross-repo note, falling back to native Claude Code memory when the vault is off.
---

You are the Remember command. The user asked you to remember a durable fact. Your job is to persist it where a future session will actually find it: the jacked memory vault when it is enabled, native Claude Code memory otherwise.

> This jacked-owned `/remember` supersedes the third-party `remember` plugin's `/remember` once that plugin is retired (see `jacked memory migrate`). When both are installed, prefer this one.

## Step 1: Extract the fact

Take `$ARGUMENTS` (or the fact the user just stated) and distill it to ONE durable, self-contained statement: the thing a future query would look up. Write it in the vocabulary that future query would use, not this session's shorthand. If it is transient (a one-off detail that only matters right now), say so and do NOT store it.

## Step 2: Pick the type

Choose the best-fit note type:
- `decision`: a choice made and why (architecture, tradeoff, "we went with X over Y").
- `convention`: a durable rule, preference, or how-we-do-it-here.
- `vision`: a direction or goal.
- `reference`: a pointer to an external resource, doc, or value.
- `progress`: a state-of-the-work note (what shipped, where we are).

When genuinely torn: `reference` for a pointer, `convention` for a rule.

## Step 3: Route it

Check whether the vault is enabled with `jacked memory status --quiet` (exit 0 means enabled; a nonzero exit means off or uninitialized).

**If enabled**, write the note (group and repos are inferred from the current repo automatically):
```bash
jacked memory add --type <type> --title "<short title>" --body "<the durable fact>"
```
Add `--group <name>` only to override the inferred group, and `--tags <a,b>` for retrieval hints. Report the path it wrote.

**If disabled**, fall back to native memory: save it via Claude Code's `#` memory shortcut / `MEMORY.md`, or, for a durable rule that belongs in project or global config, suggest `/learn` to codify it into CLAUDE.md. Never silently drop a fact the user asked you to remember.

## Safety rails
- One fact per note. If the user gave several, add several notes.
- Do NOT store secrets (API keys, tokens, passwords, connection strings) in the vault. Redact to a placeholder first.
- Never invent a fact; store only what the user actually asked to remember.
