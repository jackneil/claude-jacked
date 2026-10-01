# /dcr CODEX DISPATCH

Read this file only when the ENGINE CHECK in SKILL.md returned `"engine": "codex"` with `"usable": true`. It replaces Task spawns for the non-carve-out reviewers in EVERY wave of this run, re-check waves included. Use the `model`, `effort`, `keep_on_claude`, and `schema_path` values from the `jacked dcr engine --json` output.

The engine never moves judgment. You, the parent, keep lens selection, finding validation, the fix phase, and the verdict. Codex reviewers count against the RISK TIER agent budget exactly like Task reviewers.

## Carve-outs

Carve-outs stay as Claude Task dispatches regardless of engine: every lens listed in `keep_on_claude` (each gets its OWN single-lens Claude reviewer; Security still follows the tiered-dispatch model rules in step 4) and the conditional Frontend Design reviewer. Everything else — the standard lens reviewers (grouped per the RISK TIER shape) and, at the LARGE tier, the pre-mortem analyst — runs on Codex.

**Carve-out BEFORE grouping:** remove the `keep_on_claude` lenses from the lens pool FIRST (each becomes its own single-lens Claude reviewer), then group the REMAINING lenses for the Codex reviewers per the RISK TIER shape (step 4 applies to this reduced pool — at SMALL that is one consolidated Codex reviewer carrying all remaining lenses). Every selected lens must appear exactly once across the wave — never dropped because its would-be group partner was carved out, and never reviewed on both engines. If `keep_on_claude` is empty (the user explicitly cleared it), say so in the wave announcement: `Carve-outs cleared — every lens including Security runs on Codex.`

## Per-reviewer procedure

For each Codex-engine reviewer in a wave:

1. **Write the complete reviewer brief to a scratchpad file** (e.g. `<scratchpad>/dcr-wave1-reviewer-A.md`). Identical content to the Task prompt you would have written (SPAWNING INSTRUCTIONS items 1-13 as applicable: READ-ONLY, the assigned lenses + lens details, phase, persona/wild card at the LARGE tier, PROJECT_CONTEXT, evidence requirement, the full DO NOT FLAG list, scope and provenance, re-check context on wave 2+, pre-mortem instructions for the pre-mortem analyst). Append this output instruction: "Your final message MUST be only the JSON required by the output schema: one lens_report per assigned lens (the pre-mortem analyst emits a single lens_report named 'Pre-Mortem'). Put each finding's concrete trigger — the specific input, state, or call path — in `trigger`, the exact location in `file`/`line_start`/`line_end`, and `introduced_by_branch` (true when the defect lives in lines or behavior this diff changed)."
2. **Launch the job** with Bash `run_in_background: true` (these are CLI processes, not subagents):
   ```
   codex exec --sandbox read-only --ephemeral --cd "<repo root>" \
     -m "<model>" -c model_reasoning_effort="<effort>" \
     --output-schema "<schema_path>" \
     -o "<scratchpad>/dcr-wave1-reviewer-A.out.json" \
     - < "<scratchpad>/dcr-wave1-reviewer-A.md"
   ```
   Launch ALL Codex jobs for the wave first, then spawn the wave's Claude carve-out Task calls in the same step so everything runs in parallel.
3. **Collect**: as each job exits, Read its `.out.json` and parse the findings. A reviewer's lens_reports slot into the wave results exactly like a Task reviewer's report.
4. **Per-job failure** = non-zero exit, missing or empty output file, or unparseable JSON. Respawn THAT reviewer once as a Claude Task subagent with the same brief and announce: `Reviewer [X] failed on Codex ([short reason]) — re-ran on Claude.` Never drop a lens silently and never count a failed reviewer as PASS. If one job is still running long after the rest of the wave finished (roughly 15+ minutes), treat it as hung: kill it and use the same Claude fallback. A wave with a failed job that has not been re-run yet is INCOMPLETE, not clean.

## Validation and reporting

Codex findings enter FINDING VALIDATION (step 8b) exactly like Claude findings. Validation is mandatory for every Codex CRITICAL/MEDIUM: a cheaper review model is safe only because the parent adjudicates each finding against the real code before the fix phase.

Add `Engine: Codex ([model], effort [effort])` to each wave announcement, and fill the report's `**Engine:**` line with the reviewer count and the number of Claude fallbacks.
