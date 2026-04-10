# Compound Command Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split compound shell commands (&&, ||, ;) into separately approvable sections in the always-allow modal.

**Architecture:** Modify tokenizeForSelector to detect compound operators and return a compound type with per-subcommand token arrays. Modify showAlwaysAllowModal to render multiple token selector sections when compound type is detected. One modal, multiple rules added in batch.

**Tech Stack:** Vanilla JS (no framework), existing modal/API patterns

---

## File Map

| File | Role | Tasks |
|------|------|-------|
| `jacked/data/web/js/components/logs-gatekeeper.js` | Tokenizer + modal | 1, 2 |

---

### Task 1: Tokenizer - Detect and Split Compound Commands

**Files:**
- Modify: `jacked/data/web/js/components/logs-gatekeeper.js:7,43-80`

- [ ] **Step 1: Add compound split regex at line 7 (after _ENV_PREFIX_RE) and replace tokenizeForSelector**

Add the regex, then replace the function to detect compound operators (&&, ||, ;) and split into a parts array. Each part gets its own tokens and recommendedIndex. If split yields only one part, fall through to simple token handling. Pipes (|) are NOT split - they stay as one unit.

The compound return type: `{ type: 'compound', parts: [{ tokens, operator, recommendedIndex }, ...] }`

- [ ] **Step 2: Verify simple commands still work in the dashboard**

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/logs-gatekeeper.js
git commit -m "feat: tokenizer splits compound commands on &&, ||, ;"
```

---

### Task 2: Modal - Multi-Section Layout for Compound Commands

**Files:**
- Modify: `jacked/data/web/js/components/logs-gatekeeper.js:127-537`

- [ ] **Step 1: Add compound branch to showAlwaysAllowModal**

Add `const isCompound = tokenData.type === 'compound'` after the existing `isPath` check. Declare `let partStates = null` at the top for the button handler to access.

When isCompound, render:
- Instruction text: "This is a compound command. Set allow boundaries for each part:"
- For each part in tokenData.parts:
  - Operator separator (&&, ||, ;) as dim centered text between sections
  - Checkbox (default checked) + "Part N" label
  - Token pill row (reuse existing renderTokens pattern - clickable pills with boundary, wildcard star indicator)
  - Pattern code display per part
- Summary div at bottom showing all enabled patterns
- updateCompoundPattern function that refreshes individual pattern displays and summary

Use escapeHtml() on all pattern text displayed. Use textContent for plain text, createElement for DOM building (matching existing patterns in the file).

- [ ] **Step 2: Update Add Rule button handler for batch POSTs**

When isCompound and partStates exists:
- Collect all enabled (checked) parts
- POST each pattern to /api/claude-settings/permissions/rule with the selected scope
- Show toast: "Added N rules: pattern1, pattern2"

When not compound: existing single-rule logic unchanged.

Update button text to "Add Rules" for compound commands.

- [ ] **Step 3: Test manually**

1. Start jacked webux
2. Navigate to Logs > Gatekeeper
3. Find a compound command (git add . && git commit -m "msg")
4. Click "always allow"
5. Verify: modal shows two sections separated by &&
6. Verify: each section has its own clickable token pills
7. Verify: unchecking a section grays it out
8. Verify: "Add Rules" button creates rules for all checked sections
9. Verify: simple (non-compound) commands still work as before

- [ ] **Step 4: Run backend tests**

```bash
uv run python -m pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/js/components/logs-gatekeeper.js
git commit -m "feat: compound command approval - multi-section token selector

Compound commands (&&, ||, ;) now show separate token selector sections
in the always-allow modal. Each subcommand can be independently approved
with its own boundary. One click adds all selected rules."
```

---

## Self-Review

1. **Spec coverage:** Tokenizer split, modal multi-section, batch POST, pipe exclusion, edge cases (trailing operator, 3+ parts) - all covered.
2. **Placeholder scan:** Task 2 Step 1 has detailed rendering instructions rather than exact code blocks due to the security hook blocking innerHTML examples. The implementer has enough detail plus the existing renderTokens pattern in the same file to follow.
3. **Type consistency:** partStates used consistently. _patternForBoundary reused. _COMPOUND_SPLIT_RE defined once.
