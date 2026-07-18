---
name: memory-librarian
description: Groom the jacked memory vault for a group. Dedupe notes, reconcile contradictions newest-wins-with-a-trail, review candidate auto-captures, archive stale hot entries, validate frontmatter, and re-sync the index. Use when the SessionStart brief nudges "memory drift is high", when asked to "groom the vault", "tidy memory", "clean up notes", "reconcile the vault", or after a burst of captures has piled up unreviewed notes.
---

# Memory Librarian

You are grooming a jacked memory vault so its notes stay trustworthy and easy to find. The rollup of episodic history (today to recent to archive) is already handled by deterministic code (`jacked memory rollup`). Your job is the judgment work that code cannot do: deciding when two notes are really the same fact, which of two contradicting notes wins, whether an auto-capture is signal or noise, and how to phrase an index line so a future search actually finds it.

This is judgment, not a checklist to run mechanically. When you are unsure whether something is worth keeping, keep it. Never delete episodic history (the `episodic/` dirs and their `recent.md` / `archive.md` / `*.done.md` files). You prune and reconcile semantic notes; you never rewrite the record of what happened.

## When to run

- The SessionStart brief said memory drift is high (notes added since the last groom crossed the threshold). That is the nudge to spend a few minutes here.
- The user asks for it directly.
- You just finished a session that added several notes and want to leave the vault clean.

If drift is low and nothing looks stale, it is fine to do almost nothing and still mark the vault groomed at the end. A quiet pass is a valid pass.

## Orient first

Work one group at a time (default to the group for the current repo). Before changing anything, read enough to hold the group in your head:

1. `groups/<group>/index.md` for the shape of what exists and how each note is currently summarized.
2. `groups/<group>/hot.md` for the current progress slice.
3. The notes that look most relevant to recent work, and any tagged `candidate` (the unreviewed auto-captures).

Use `jacked memory search <term>` to pull related notes together and `jacked memory status` to see the drift counter and note counts. Search is grep over `hot.md`, then the index, then note bodies, so the words you groom a note with are the words a future search will match on. Write for that future query.

## What to do

**Dedupe.** When two or more notes state the same fact, keep the newest one and delete the others. Before deleting, fold in anything a duplicate said that the survivor did not, so nothing is lost. Add a short line in the survivor's body noting that it absorbed the others and when. Bump its `updated` date.

**Reconcile contradictions (newest wins, with a trail).** When two notes disagree, the newer decision governs. Keep the newer note as the source of truth and record in its body what it superseded and when (for example: "supersedes the earlier choice of X, changed 2026-07-18 because ..."). Do not silently drop the loser's reasoning; a future reader needs to know the old answer was considered and why it changed. Delete the stale note only after its useful context has moved into the survivor.

**Review candidate auto-captures.** Notes tagged `candidate` were written by the capture triage without review. For each: if it is a real, durable fact, promote it by removing the `candidate` tag and sharpening the title and body into the vocabulary a future search would use (auto-capture phrasing is often vague). If it is noise (a restatement of something already recorded, a transient detail, an artifact of a dead-end), delete it. When you genuinely cannot tell, leave the `candidate` tag on and move on; keeping an unreviewed note costs nothing, deleting a real one costs a fact.

**Archive stale hot entries.** `hot.md` is a current slice, not an append-only log. If it has grown to carry finished or stale progress, rewrite it down to what is actually current. Anything worth keeping long term becomes (or updates) a proper typed note before you trim it out of hot.

**Validate frontmatter.** Check each note you touched against the schema in the vault `README.md`: `type` is one of the allowed types, `repos` is a non-empty list, `group` is set, `created` and `updated` are ISO dates, `tags` is a list. Fix anything malformed on the notes you are already editing.

**Re-sync the index.** After your edits, make `groups/<group>/index.md` match reality: exactly one line per note, no lines pointing at notes you deleted, no notes missing a line, and each hook phrase sharp enough that skimming the index tells a reader what the note holds. Keep the plain-hyphen line format the rest of the vault uses; the index is echoed to the terminal by `jacked memory search`, so keep it clean.

## Finish

When the pass is done, mark the vault groomed and commit your changes:

```
jacked memory mark-groomed
git -C <vault-path> add -A && git -C <vault-path> commit -m "memory: librarian groom of <group>"
```

`jacked memory mark-groomed` resets the drift counter and stamps the groom time, so the SessionStart nudge quiets down until enough new notes accumulate again. The vault is its own git repo, so committing there is separate from any commit in the repo you are working in. Report a short summary of what you merged, reconciled, promoted, and deleted.
