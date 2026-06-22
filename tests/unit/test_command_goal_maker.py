"""Content-presence regression tests for the /goal-maker command — forges a
hardcore, overnight-sized /goal brief from the current working context, with a
default-PR / opt-in-merge toggle.

These guard the contract: default is PR-only (merge is opt-in via the `merge`
arg), auto-merge is always CI-gated and uses a true merge commit, the brief
inherits /whats-next's hard 4,000-char measurement + file-backed fallback +
turn backstop + DATA-only rule, and the UI/UX/TDD quality bars are baked in.
Like the other command tests, these are string-presence checks on the LLM
instruction document, not runtime-behavior assertions.
"""

from pathlib import Path

import pytest

CMD = Path(__file__).resolve().parents[2] / "jacked" / "data" / "commands" / "goal-maker.md"


@pytest.fixture(scope="module")
def gm() -> str:
    return CMD.read_text(encoding="utf-8")


def test_goal_maker_command_exists():
    assert CMD.exists(), "jacked/data/commands/goal-maker.md must exist (ships via the install glob)"


def test_frontmatter_marks_it_deliberate_and_distinguishes_siblings(gm: str):
    head = gm[: gm.index("---", 3) + 3] if gm.startswith("---") else gm[:800]
    assert "description:" in head
    low = head.lower()
    assert "never auto-triggered" in low or "deliberately invoked" in low  # deliberate, like /bhag
    assert "/whats-next" in head and "/bhag" in head                       # names both siblings
    assert "pr" in low and "merge" in low                                  # the toggle is advertised


def test_default_is_pr_merge_is_opt_in(gm: str):
    assert "MERGE mode" in gm and "PR mode" in gm
    # PR is the default and safe path; merge is reached only via the `merge` arg
    assert "default" in gm.lower()
    assert "`merge`" in gm
    # the PR-mode brief must explicitly NOT merge
    assert "NOT merged" in gm or "never merges" in gm.lower()


def test_merge_mode_is_ci_gated_and_uses_true_merge_commit(gm: str):
    assert "green CI" in gm                                  # never merge a red/unverified build
    assert "gh pr merge --merge" in gm                       # pinned merge method (true merge commit)
    assert "`--squash`" in gm and "`--admin`" in gm          # forbids history-rewrite / protection bypass
    assert "force-push" in gm                                # never force-push / rewrite history
    assert "WAIT for all CI checks" in gm                    # block on CI, never merge on pending


def test_inherits_whatsnext_brief_engine(gm: str):
    # hard char gate, measured not eyeballed
    assert "wc -c" in gm
    assert "4,000" in gm
    assert ".claude/goals/" in gm
    # file-backed fallback for briefs that won't fit
    assert "pointer-goal" in gm
    # bounded unattended run
    assert "backstop" in gm.lower()
    assert "BLOCKED" in gm
    # the brief skeleton
    assert "Deliver:" in gm and "Verify" in gm and "DONE when:" in gm


def test_sources_from_current_context_not_a_fresh_decision(gm: str):
    low = gm.lower()
    assert "this conversation" in low                        # primary source is the live session
    assert "spec" in low and "plan" in low                   # plus a referenced spec/plan
    # it explicitly does NOT re-decide the work (that's /whats-next)
    assert "do not re-decide" in low or "already know" in low


def test_quality_bars_are_baked_in(gm: str):
    assert "TDD" in gm
    assert "/qa" in gm and "/ux" in gm                       # browser QA
    assert "make-interfaces-feel-better" in gm               # front-end design / UX detail
    assert "/cso" in gm                                      # security gate
    assert "/dcr" in gm                                      # review gate


def test_plan_ahead_next_steps(gm: str):
    # the brief must name what comes after this run so /whats-next can pick up
    assert "Next:" in gm
    assert "/whats-next" in gm


def test_security_data_only_rule(gm: str):
    assert "DATA" in gm
    assert "ignore previous" in gm.lower() or "text omitted" in gm.lower()


def test_separate_command_rationale(gm: str):
    low = gm.lower()
    assert "auto-triggering skill" in low or "auto-trigger" in low
    assert "merges to" in low or "auto-merge" in low         # documents WHY it's gated behind a typed name
