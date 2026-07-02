---
name: recover
description: Use when a Claude Code session in this folder crashed mid-work (computer died, terminal closed, Claude broke) and you reopened Claude without a checkpoint and don't know the session ID — rebuild the last session for THIS folder from its on-disk transcript and continue. Triggers include "recover my session", "my session crashed", "it crashed before I could save", "get me back to where I was", "restore the crashed session", "resume the session that died". NOT for transient API/rate-limit errors mid-turn (use retry), NOT for deliberately saved state (use /checkpoint resume), NOT for finding an old session by topic across machines (use /jacked).
---

# Recover a crashed session

Rebuild the most-recently-active prior Claude Code session **for the current folder** from its raw on-disk transcript, inject a budgeted working-state digest into THIS session, and offer native `claude --resume` for a full thread continuation.

## When to use
- A session crashed or was killed mid-task, you reopened Claude here, no `/checkpoint` was saved, and you don't know the session ID.
- You want to keep working in the session you already have open (digest injection), or get the original thread back natively.

**Not this skill:** transient API/rate-limit blip mid-turn -> `retry`. Deliberately saved checkpoint -> `/checkpoint resume`. Topic search across all past sessions/machines -> `/jacked`.

## Requirements
Needs a current `jacked` CLI on PATH. Handle these two failure cases explicitly; do not work around them by hand-parsing transcripts:
- **`jacked` not found** -> tell the user to install or repair jacked, then stop.
- **`jacked recover` reports `No such command 'recover'` / exits 2** -> the on-PATH `jacked` is outdated (it predates this command). Tell the user to upgrade with `uv tool install claude-jacked --force && jacked install`. In a uv-managed repo checkout, `uv run jacked recover ...` runs the current source instead.

**On-disk source of truth (for self-verification only).** Transcripts always survive at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, where `<encoded-cwd>` is the cwd with `/`, `\`, and `.` all replaced by `-`. If `jacked recover` finds nothing or a near-empty list, the cause is almost always the wrong folder or a non-default `CLAUDE_CONFIG_DIR`/`CLAUDE_PROJECTS_DIR` (an index/path mismatch), **not** data loss — point the user at that path to confirm the files exist before concluding the session is gone. Do not invent state.

## Steps

1. **Find candidates.** Run:
   ```bash
   jacked recover --json --exclude "$CLAUDE_CODE_SESSION_ID"
   ```
   Passing `$CLAUDE_CODE_SESSION_ID` at the shell excludes the session you are running in (it would otherwise rank newest). If this errors with `No such command 'recover'`, your `jacked` is outdated — see Requirements.

2. **No candidates** (`count` is 0 or `chosen` is null) -> tell the user no recoverable session was found for this folder (fresh repo, wrong folder, or nothing crashed here) and stop. Do not invent state.

3. **Sanity-check the auto-pick.** If `chosen.last_prompt` is itself a `/recover` invocation, that is the live session leaking through — drop it and use the next candidate (or re-run with that id added to `--exclude`).

4. **Present and confirm — before injecting.** Show the chosen session and the alternates: `ai_title`, `age`, `git_branch`, and `last_prompt`. The recommended `chosen` is the newest session with real substance — if the very newest session is near-empty (almost no messages) it is skipped as the recommendation but still appears in the candidate list, so the user can pick it as an alternate. Ask: "Recover this one, or pick an alternate (<ids>)?" Wait for the user. Do not inject until they confirm.

5. **Inject the digest.** On confirmation:
   ```bash
   jacked recover --session <id> --digest
   ```
   The output IS the recovered working state — read it. Besides the todos, files, plan, and sub-agent findings, it surfaces two crash-critical facts when present: an **In-flight intent** block (the crashed agent's final reasoning — why the next step was next) and a **Failed actions** list (e.g. `Bash: pytest -q → FAILED: 3 errors`), since the last command/edit that *errored* is usually the single most load-bearing recovery fact. It ends with a `claude --resume <id>` line and, if it was trimmed to fit, a budget note.

   Scale the digest with `--depth {brief|standard|full}` (default `standard`): use `--depth full` for a heavy multi-file session (more requests, tool actions, files, and a larger char budget), `--depth brief` for a quick one-liner recovery. `--budget N` overrides the char budget independently.

6. **Offer native resume.** Tell the user: "For a true continuation that preserves Claude's internal state, run `claude --resume <id>` in a fresh terminal. The digest above lets us continue right here instead."

7. **Re-anchor and continue.** Summarize in 1-2 lines: "You were working on X; last step was Y; next was Z." `MEMORY.md` already carries standing project conventions. Then continue the work.

## Manual restart of /goal or /loop
If the recovered digest contains a "Manual restart required" block, the crashed session was driving a `/goal` or `/loop` that **cannot be auto-resumed** (these only run when pasted into a live Claude Code session). Surface that exact command to the user and tell them to paste it into Claude Code themselves to restart it — do not try to run it yourself.

## Wrong pick
If the user says it is the wrong session, re-run step 5 with the alternate's id from the candidate list.

## Incomplete last turn
When the digest flags "the last turn may be incomplete," treat that work as in-progress, not finished — verify it before building on it.
