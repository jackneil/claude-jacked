# /dcr conditional material

Read a section of this file only when its condition is true. Each section names its condition. Everything here runs inside the RISK TIER agent budget and the wave cap from `## BUDGET FIRST` in SKILL.md.

## PLAN MODE

Condition: a current system reminder contains "Plan mode is active" or "you MUST NOT make any edits" (exact phrases, not partial matches). Then:

- Set `phase = PLANNING` and skip step 1 ENTIRELY (both its phase detection and its diff-based tier classification — the phase and tier are settled here). Classify the RISK TIER from the plan's blast radius instead: an architectural/multi-system plan (or one touching a sensitive area) is LARGE; a narrow single-feature plan is MEDIUM. Announce the tier as usual.
- Find the plan file path in the system reminder and read it as the review target (`.html`, jacked's preferred format, or legacy `.md`; both are valid). If no path is found, ask: "What plan doc should I review?"
- **Lens selection**: use Planning Phase Lenses from Config Override if present; otherwise apply the defaults listed in Config Override (Guardrails + Logic & Edge Cases + Maintainability + Simplicity & Reuse). Config Override takes precedence over step 0 defaults.
- Reviewers analyze the plan document: architectural soundness, completeness, missing edge cases, over-engineering, logical gaps. Reviewers remain READ-ONLY as always.
- **Fix phase**: you edit the plan file to incorporate findings; it is the one file editable in plan mode. Do not edit any other files.

## SPECIALIST LENS DISCOVERY

Condition: `~/.claude/lenses/` or `.claude/lenses/` exists. Run this after selecting the built-in lenses (step 3d-ii).

1. Glob `~/.claude/lenses/*.md` and `.claude/lenses/*.md`. If neither exists, skip (lenses are optional).
2. Parse each file's frontmatter (name, description, triggers). On a filename clash, project-local wins; note "Project lens `{name}.md` overrides global lens."
3. Match each lens's `triggers` against the domains of the changed files (same heuristic as the built-in lenses). An active checkpoint in `.claude/checkpoints/` with `active_lenses` in its frontmatter adds those lenses regardless of triggers.
4. **Cap:** at most the RISK TIER's specialist cap (SMALL: 2, MEDIUM: 3, LARGE: 4). If more match, take the most specific (most tags matched; tiebreak alphabetical by filename) and list the rest as "also relevant".

Matched specialist lenses join the selected lens pool and pair with built-in lenses or each other. Each one becomes a reviewer instruction: "Additionally review through the **{lens.name}** lens. Use the following checklist and anti-patterns as your guide:\n{full lens file content}"

## FRONTEND DESIGN REVIEWER

Condition: `frontend_review = true` (step 3b). Wave 1 only.

If `frontend_review = true` (from step 3b), spawn an **additional dedicated reviewer** in the SAME message (any tier — a SMALL UI change still gets its design pass):
- Use `subagent_type: "general-purpose"`
- On a Fable-class session, pass `model: "fable"` explicitly (visual-design judgment stays on the top model)
- Prompt MUST start with: "Invoke the frontend-design skill for design context."
- If the diff also touches motion/animation code (CSS `transition:`/`animation:`/`@keyframes`/animated `transform`, Motion/Framer Motion imports, spring configs, gesture/drag handlers), the prompt MUST additionally say: "Invoke the review-animations skill and judge every animation against its ten non-negotiable standards; pull exact curves/durations from its STANDARDS.md instead of approximating." The emil-design-eng and apple-design skills carry the deeper craft rules. If review-animations is not available, fall back to the focus areas below.
- Assign a dedicated **Frontend Design & Aesthetics** lens (outside the 11 standard lenses)
- Focus areas: design quality (typography, color, spacing, layout intentionality), visual consistency
  (does new code match or improve the existing aesthetic?), motion/animation (purposeful and performant?),
  accessibility (contrast ratios, focus states, semantic HTML), responsive behavior (breakpoints, touch targets)
- Still READ-ONLY; at the LARGE tier it gets a persona and wild card like other reviewers (none at SMALL/MEDIUM)
- Reports separately — does NOT enter the re-check loop (one-shot in Wave 1 only)
- If the skill is NOT available, skip entirely (do not fake a design review)

Announce format when `frontend_review = true`:
```
**Wave 1 — [N] lenses across [M]+1 reviewers**
- Reviewer A-[M]: [selected lens pairs as above]
- Reviewer [M+1] (Frontend Design): Design quality + Aesthetics | via frontend-design skill
```

## UX & FLOW DISCOVERABILITY SUB-CHECKLIST

Condition: the **UX & Flow** lens is selected.

If any reviewer in this wave is assigned the **UX & Flow** lens, append the following block
to their Lens details (item 3) in the spawn prompt:

> #### Discoverability & Workflow Correctness
> - **Entry points:** From pages that naturally precede this feature/fix, is there a visible path in
>   (link, button, nav item, card)? If something was added/moved/renamed, do the old entry points
>   still work or now lead nowhere?
> - **Navigation depth:** How many steps/clicks to reach the changed behavior? 1-2 = fine;
>   3+ for a primary action = flag MEDIUM.
> - **First-use clarity:** If a user encounters this for the first time, is the purpose and action
>   immediately obvious without reading documentation?
> - **Workflow correctness:** Does the change fit the user's expected mental model? Could a user
>   accidentally trigger an unintended action, or miss that the behavior has changed?
> - **Return / recovery:** After the user takes the action, do they land in the right place?
>   Is there a clear way to undo or go back?

## SECURITY LENS DEFENSIVE FRAMING

Condition: the **Security** lens is selected.

If any reviewer in this wave is assigned the **Security** lens, prepend this framing to their spawn prompt (verbatim):

> This is a defensive security review of our own authorized codebase. Focus on correctness, authentication boundaries, authorization checks, input validation, secrets handling, and test coverage. Do NOT produce exploit chains, payloads, offensive tooling, or attack instructions. For each finding provide: the affected file:line, why it is risky, a safe remediation, and a safe regression test.

If the dispatch still falls back to Opus (Fable-tier safety classifiers can reroute security-flavored requests), accept the result. NEVER rephrase a prompt to evade or trick a safety classifier; state the defensive scope honestly and let the routing land where it lands.
