# Command Audit — March 2026

Audit of all 31 jacked entities against native Claude Code features (Feb-March 2026 releases).

## Methodology

Compared each jacked command/skill/agent against:
- Claude Code native features (Auto-Memory, Agent Teams, Session Memory, --resume, /remember, Remote Control, Tasks, Plugin Marketplace)
- Gary Tan's gstack (9 commands)
- Plugin ecosystem (9,000+ plugins as of March 2026)

---

## Verdict Summary

| Entity | Type | Verdict | Reason |
|--------|------|---------|--------|
| `/dcr` | command | **KEEP** | No native equivalent. Parallel recursive review with randomized lenses is unique |
| `/dc` | command | **KEEP** | No native equivalent. Phase-aware double-check with adversarial grill mode |
| `/qa` | command | **KEEP** | No native equivalent. Browser-based QA testing with Chrome DevTools/Playwright |
| `/ux` | command | **KEEP** | No native equivalent. Multi-agent parallel UX review |
| `/release` | command | **KEEP** | Tailored to jacked's GitHub Actions + PyPI publishing workflow |
| `/pr` | command | **KEEP** | Wraps `pr-workflow-checker` agent. Native `gh` exists but this adds context awareness |
| `/jacked-setup` | command | **KEEP** | Unique — generates repo-specific config overlays for faster command runs |
| `/swarm-research` | command | **KEEP** | Unique — divergent research with synthesis and devil's advocacy |
| `/whats-next` | command | **KEEP** | Unique — lifecycle-aware roadmap advisor. Doesn't compete with native Tasks |
| `/techdebt` | command | **KEEP** | No native equivalent. Concrete scanning (TODOs, oversized files, missing tests) |
| `/audit-rules` | command | **KEEP** | No native equivalent. CLAUDE.md dedup/contradiction checker |
| `/redo` | command | **KEEP** | No native equivalent. Safe scratch-and-rebuild workflow |
| `/learn` | command | **EVALUATE** | Overlaps with native Auto-Memory + /remember. But jacked's is more structured (graduation, dedup, version-controlled) |
| `/swarm` | command | **EVALUATE** | Thin wrapper around native Agent Teams. May not add enough value |
| `jacked` | skill | **KEEP** | Core differentiator. Cross-machine semantic search has no native equivalent |
| `dcr` | skill | **KEEP** | Thin dispatcher for /dcr command |
| `qa` | skill | **KEEP** | Thin dispatcher for /qa command |
| `ux` | skill | **KEEP** | Thin dispatcher for /ux command |
| `swarm-research` | skill | **KEEP** | Thin dispatcher for /swarm-research command |
| `whats-next` | skill | **KEEP** | Thin dispatcher for /whats-next command |
| `claude-md-optimizer` | skill | **KEEP** | No native equivalent. Token efficiency auditing for CLAUDE.md |
| All 10 agents | agents | **KEEP** | Internal implementation details, not user-facing surface area |

---

## Detailed Assessment: EVALUATE Items

### `/learn` — Overlaps with Auto-Memory

**What jacked does:** Extracts lessons from conversations, writes to CLAUDE.md or lessons.md with strike counters ([1x], [2x], [3x]), graduates frequently-hit lessons to permanent rules. Dedup detection across 3 files.

**What native does:** Auto-Memory/MEMORY.md automatically persists learnings. `/remember` promotes patterns to permanent config.

**Key differences:**
- Jacked's output is version-controlled (CLAUDE.md is in git) — shareable with teammates
- Jacked has a structured graduation path (lessons.md → CLAUDE.md)
- Jacked deduplicates across project CLAUDE.md, global CLAUDE.md, and lessons.md
- Native Auto-Memory is implicit and automatic; jacked's is explicit and auditable

**Recommendation:** KEEP but document the distinction. `/learn` is the "auditable, version-controlled" version of memory. Native Auto-Memory is the "zero-effort" version. They serve different needs. Add a note to /learn's description clarifying when to use it vs native memory.

### `/swarm` — Thin Wrapper Around Native Agent Teams

**What jacked does:** Uses `TeamCreate`, `TaskCreate`, `SendMessage` (native primitives) to parallelize work. Adds:
- File-level isolation rules (no two teammates edit the same file)
- Scaling heuristics (3-8 teammates based on complexity)
- Test-after-all-finish workflow

**What native does:** Agent Teams provides the same primitives directly.

**Key differences:**
- `/swarm` adds opinionated orchestration (file isolation, scaling, testing workflow)
- Without `/swarm`, users must manually orchestrate teams

**Recommendation:** KEEP for now but monitor. The orchestration layer adds genuine value. If Claude Code ships higher-level team orchestration natively, deprecate. Add a comment in the command noting it wraps native Agent Teams.

---

## Surface Area Assessment

**Current count:** 31 entities (14 commands + 7 skills + 10 agents)

**User-facing surface:** 14 commands + 2 standalone skills (jacked, claude-md-optimizer) = **16 user-facing entities**

The other 5 skills are thin dispatchers and 10 agents are internal — they don't add cognitive load.

**Recommendation:** 16 user-facing entities is within the proposed 15-18 budget. No immediate cuts needed, but adding new commands should require deprecating one.

---

## Action Items

1. **Add distinction notes to `/learn`** — Clarify in the description when to use /learn vs native Auto-Memory
2. **Add native-wrapper note to `/swarm`** — Acknowledge it wraps Agent Teams, explain the value-add
3. **Establish deprecation policy** — Write a short policy: announcement → deprecation warning (1 release) → removal (next release)
4. **Implement `--core` install** — Progressive disclosure for new users (essentials: /dc, /dcr, /learn, /pr, /release, /redo)
5. **Set surface area budget** — Cap at 18 user-facing entities. New command = deprecate one.

---

## What NOT to Build (confirmed by this audit)

- `/handoff` — Triple-solved by native Session Memory + --resume + Auto-Memory
- `/changelog` (standalone) — Embed in `/release` instead (see future-work.md Priority 2)
- Custom state persistence for `/whats-next` — Competes with native Tasks
- Chat dashboard via `--sdk-url` — At high risk of being superseded by official Agent SDK + Remote Control
