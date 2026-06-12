# Compound Command Approval in Token Selector

**Date:** 2026-04-10
**Status:** Final
**Scope:** `logs-gatekeeper.js` — tokenizer + always-allow modal

## Problem

When a compound command like `git add file.py && git commit -m "msg"` appears in the gatekeeper logs, clicking "always allow" opens a modal with a flat token list: `git`, `add`, `file.py`, `&&`, `git`, `commit`, `-m`, `"msg"`. The user can only set one boundary across the whole string — no way to approve `git add` and `git commit` as separate rules. This means `&&` gets treated as a regular token, and the user must either approve the exact compound command or manually type a custom pattern.

The backend gatekeeper already splits on `&&`/`||` and evaluates each subcommand independently (security_gatekeeper.py line 966). The frontend modal needs to match this behavior.

## Design

### Tokenizer: `tokenizeForSelector(command, method)`

**File:** `jacked/data/web/js/components/logs-gatekeeper.js:43-80`

When the command contains `&&`, `||`, or `;`, split into subcommands first. Return a new compound type:

```javascript
// Simple command (unchanged):
{ type: 'tokens', tokens: ['git', 'status'], recommendedIndex: 1 }

// Compound command (new):
{ type: 'compound', parts: [
    { tokens: ['git', 'add', 'file.py'], operator: null, recommendedIndex: 1 },
    { tokens: ['git', 'commit', '-m', '"msg"'], operator: '&&', recommendedIndex: 1 },
]}
```

Split on `&&`, `||`, `;` only. **NOT** `|` (pipes) — `grep foo | wc -l` is one logical pipeline, not two independent commands. This matches the backend gatekeeper which splits on `&&`/`||` but handles pipes separately.

Each part gets its own token array, operator label (for display), and recommended index (computed using the existing `_KNOWN_COMMAND_PREFIXES` dictionary).

### Modal: `showAlwaysAllowModal({ tokenData, repoPath })`

**File:** `jacked/data/web/js/components/logs-gatekeeper.js:127-537`

When `tokenData.type === 'compound'`, render a multi-section layout:

**Per subcommand section:**
- Operator label between sections (`&&`, `||`, `;`) styled as a dim separator
- Its own clickable token pills with boundary selection (reuse existing `renderTokens` logic)
- Checkbox: "Add rule for this part" (default: checked)
- Its own pattern display (`Bash(git add:*)`)
- Its own recommended badge

**"Add Rule" button:**
- Collects all checked patterns
- POSTs each to `/api/claude-settings/permissions/rule` (existing endpoint, no change)
- Shows success: "Added N rules: git add:*, git commit:*"
- Scope (global/project) applies to all rules in the batch

**Single-command fallback:** When `tokenData.type === 'tokens'` (no operators), behavior is identical to today. Zero regression risk.

### Edge Cases

- **Single subcommand with operator:** `git add . &&` (trailing operator) → strip empty parts after split
- **Three+ subcommands:** `git add . && git commit -m "x" && git push` → three sections, all independently checkable
- **Environment prefix:** `ENV_VAR=value git add` → existing `_ENV_PREFIX_RE` strip happens before compound split
- **Pipes within subcommands:** `git log --oneline | head -5 && git status` → split on `&&`, the pipe stays intact in the first part's token list

## Files Modified

| File | Changes |
|------|---------|
| `jacked/data/web/js/components/logs-gatekeeper.js` | `tokenizeForSelector` returns compound type; `showAlwaysAllowModal` renders multi-section layout |

## Non-Goals

- Changing the backend gatekeeper's compound evaluation logic
- Changing the `/api/claude-settings/permissions/rule` endpoint
- Handling pipes as separate approvable sections
