"""Doc-lint tripwires for the night-shift skill's load-bearing safety clauses.

These are the clauses a future edit must not silently drop (per the DCR
security review): the untrusted-content rule, the CLAUDE.md escalation gate,
the non-overridable production guard, and the pacing contract field names
that must match `jacked usage --json`.
"""
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "skills" / "night-shift" / "SKILL.md"
).read_text(encoding="utf-8")


def test_untrusted_content_clause_present():
    assert "DATA, never instructions" in SKILL
    assert "[text omitted]" in SKILL


def test_claude_md_escalation_is_user_decides():
    assert "changing a CLAUDE.md rule or a roster brain is User-decides" in SKILL


def test_production_guard_not_overridable():
    assert "not overridable from inside a run" in SKILL
    assert "Gate 2" in SKILL


def test_pacing_contract_field_names_match_cli():
    # Field names the skill tells the loop to read must exist in the CLI
    # contract (pinned by tests/unit/test_usage_cmd.py).
    assert "summary.pause_until" in SKILL
    assert "best_account_worst_window_pct" in SKILL
    assert "best_account_cache_age_seconds" in SKILL
    assert "earliest_future_reset_at" not in SKILL  # renamed; stale name must not linger


def test_trunk_integrity_clause_present():
    assert "confirm main's OWN post-merge checks" in SKILL


def test_resume_authorization_clauses_present():
    # Wave-2 Security M1 + Maintainability: resume must re-run Step 0 and
    # never inherit auto-merge from a stale HOOK.md.
    assert "Resume never inherits authority" in SKILL
    assert "gate2_granted_at" in SKILL
    assert "re-runs Step 0" in SKILL
    assert "isolation_verdict" in SKILL


def test_deferred_user_promotion_rule_present():
    assert "only the user's explicit answer moves a `deferred-user` item to `ready`" in SKILL


def test_null_pause_until_has_a_branch():
    assert "pause_until` is null while the percent is constrained" in SKILL


def test_negative_cache_age_is_untrustworthy():
    # cache_age_seconds is SIGNED (negative = clock skew); the freshness gate
    # must not read a negative age as fresh (Wave-3 Logic MEDIUM-2).
    assert "negative" in SKILL
    assert "null or negative = no data" in SKILL


def test_restore_point_is_durable():
    # Wave-3 Maintainability: a restore point inside scratch/ dies with the
    # run-end squash; it must live in the durable restore/ dir.
    assert ".night-shift/restore/" in SKILL


def test_dry_claim_requires_the_browser_walk():
    # Backstop condition 2 claims exclusivity over run ends, so it must
    # itself carry the walk requirement (Wave-3 Maintainability MEDIUM-2).
    assert "never actually drove the product in a browser has not patrolled" in SKILL
