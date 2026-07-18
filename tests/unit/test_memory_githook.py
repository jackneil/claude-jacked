"""Tests for M3: post-merge git-hook install/remove + merge distillation.

Two surfaces:
  - jacked.memory.githook       per-repo post-merge hook install/remove
  - jacked.memory.merge_capture the ORIG_HEAD..HEAD distillation engine

Env isolation mirrors test_memory_vault.py (JACKED_HOME + JACKED_VAULT_DIR under
tmp_path, git identity via env). The claude-call boundary is monkeypatched --
no real ``claude`` or ``gh`` is ever invoked; merge tests build REAL tmp git
repos so ORIG_HEAD/merge-commit behavior is exercised for real.
"""
import subprocess
from io import StringIO
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked import memory
from jacked.cli import main
from jacked.memory import capture, githook, merge_capture
from jacked.memory import vault as vault_mod


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "vault"
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setenv("JACKED_VAULT_DIR", str(vault))
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)
    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))
    return SimpleNamespace(home=home, vault=vault, tmp=tmp_path, buf=buf, monkeypatch=monkeypatch)


def _git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), *args], check=check,
                          capture_output=True, text=True)


def _git_repo(path, remote=None):
    """A minimal git repo with .git/hooks (optionally with an origin remote)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    (path / ".git" / "hooks").mkdir(exist_ok=True)
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


def _bare_git_dir(path):
    """Just an empty .git/hooks skeleton (enough for install_post_merge)."""
    (path / ".git" / "hooks").mkdir(parents=True)
    return path


def _init_vault(env, groups=None, roots=None):
    """Init + enable an empty (or mapped) vault."""
    vault_mod.init_vault(env.vault, [str(r) for r in (roots or [])], groups or {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)
    return env.vault


def _merge_repo(path, remote="git@github.com:acme/widget.git"):
    """Real repo on master with a feature branch ready to be merged (no-ff)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    _git(path, "symbolic-ref", "HEAD", "refs/heads/master")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.com")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    _git(path, "checkout", "-q", "-b", "feature/x")
    (path / "feature.txt").write_text("new feature\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "add feature")
    _git(path, "checkout", "-q", "master")
    return path


def _do_merge(path, branch="feature/x", message=None):
    _git(path, "merge", "--no-ff", "-m", message or f"Merge branch '{branch}'", branch)


def _decision_notes(vault):
    return sorted((vault / "groups").glob("*/decision/*.md"))


def _vault_commits(vault):
    r = _git(vault, "rev-list", "--count", "HEAD", check=False)
    return int((r.stdout or "0").strip() or 0)


@pytest.fixture
def no_gh(monkeypatch):
    """Neutralize the gh PR fetch so merge tests never shell out to gh."""
    monkeypatch.setattr(merge_capture, "_fetch_pr", lambda repo, pr: (None, None, False))


def _mock_distill(monkeypatch, raw):
    """Monkeypatch the single claude -p call to return a canned string (or None)."""
    monkeypatch.setattr(capture, "call_claude", lambda prompt, model: raw)


# --------------------------------------------------------------------------- #
# Template file
# --------------------------------------------------------------------------- #

def test_template_exists_and_shape():
    src = githook._GIT_HOOKS_DIR / githook._TEMPLATE_NAME
    assert src.exists()
    content = src.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh")
    assert githook.HOOK_MARKER in content
    assert "# jacked-memory-hook" in content
    assert "jacked memory capture-merge" in content
    assert "exit 0" in content


def test_template_deploys_with_guardrails_glob():
    # deploy_templates globs git-hooks/*.sh; our template must match so it is
    # deployed to the global copy alongside the pre-push templates.
    from jacked import guardrails
    names = {p.name for p in guardrails.HOOK_TEMPLATES.glob("*.sh")}
    assert githook._TEMPLATE_NAME in names


# --------------------------------------------------------------------------- #
# githook install / remove
# --------------------------------------------------------------------------- #

def test_install_writes_executable_extensionless_hook(env):
    repo = _bare_git_dir(env.tmp / "repo")
    res = githook.install_post_merge(repo)
    assert res["installed"] is True
    hook = repo / ".git" / "hooks" / "post-merge"
    assert hook.exists()
    assert hook.name == "post-merge"  # extensionless
    content = hook.read_text(encoding="utf-8")
    assert githook.HOOK_MARKER in content
    # Executable bit set (Unix).
    import os
    assert os.access(hook, os.X_OK)


def test_install_no_git_dir(env):
    res = githook.install_post_merge(env.tmp / "not-a-repo")
    assert res["installed"] is False
    assert "no .git" in res["reason"]


def test_install_idempotent(env):
    repo = _bare_git_dir(env.tmp / "repo")
    first = githook.install_post_merge(repo)
    assert first["installed"] is True
    before = (repo / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")

    second = githook.install_post_merge(repo)
    assert second["installed"] is False
    assert "already installed" in second["reason"]
    after = (repo / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")
    assert before == after


def test_install_refreshes_our_stale_hook(env):
    repo = _bare_git_dir(env.tmp / "repo")
    hook = repo / ".git" / "hooks" / "post-merge"
    # A marker-bearing but stale (different) version of our hook.
    hook.write_text("#!/bin/sh\n# jacked-memory-hook\necho old\nexit 0\n", encoding="utf-8")
    res = githook.install_post_merge(repo)
    assert res["installed"] is True  # refreshed in place, no force needed
    assert "capture-merge" in hook.read_text(encoding="utf-8")


def test_install_refuses_foreign_hook_without_force(env):
    repo = _bare_git_dir(env.tmp / "repo")
    hook = repo / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\necho someone elses hook\n", encoding="utf-8")
    res = githook.install_post_merge(repo)
    assert res["installed"] is False
    assert "already exists" in res["reason"]
    # Foreign content untouched.
    assert "someone elses hook" in hook.read_text(encoding="utf-8")


def test_install_force_overwrites_foreign_hook(env):
    repo = _bare_git_dir(env.tmp / "repo")
    hook = repo / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\necho someone elses hook\n", encoding="utf-8")
    res = githook.install_post_merge(repo, force=True)
    assert res["installed"] is True
    assert githook.HOOK_MARKER in hook.read_text(encoding="utf-8")


def test_install_refuses_husky_framework(env):
    repo = _bare_git_dir(env.tmp / "repo")
    (repo / ".husky").mkdir()
    res = githook.install_post_merge(repo)
    assert res["installed"] is False
    assert ".husky" in res["reason"]


def test_install_refuses_core_hookspath(env):
    repo = _bare_git_dir(env.tmp / "repo")
    (repo / ".git" / "config").write_text("[core]\n\thooksPath = .githooks\n", encoding="utf-8")
    res = githook.install_post_merge(repo)
    assert res["installed"] is False
    assert "core.hooksPath" in res["reason"]


def test_install_force_beats_framework(env):
    repo = _bare_git_dir(env.tmp / "repo")
    (repo / ".husky").mkdir()
    res = githook.install_post_merge(repo, force=True)
    assert res["installed"] is True


def test_remove_deletes_ours(env):
    repo = _bare_git_dir(env.tmp / "repo")
    githook.install_post_merge(repo)
    assert githook.remove_post_merge(repo) is True
    assert not (repo / ".git" / "hooks" / "post-merge").exists()


def test_remove_leaves_foreign_untouched(env):
    repo = _bare_git_dir(env.tmp / "repo")
    hook = repo / ".git" / "hooks" / "post-merge"
    hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
    assert githook.remove_post_merge(repo) is False
    assert hook.exists()
    assert "foreign" in hook.read_text(encoding="utf-8")


def test_remove_no_hook_is_false(env):
    repo = _bare_git_dir(env.tmp / "repo")
    assert githook.remove_post_merge(repo) is False


# --------------------------------------------------------------------------- #
# bulk install/remove across mapped repos
# --------------------------------------------------------------------------- #

def test_install_for_mapped_repos_surfaces_skips(env):
    root = env.tmp / "Github"
    a = _git_repo(root / "a", "git@github.com:acme/a.git")
    b = _git_repo(root / "b", "git@github.com:acme/b.git")
    _git_repo(root / "c", "git@github.com:acme/c.git")  # unmapped
    ident_a = vault_mod.repo_identity(a)
    ident_b = vault_mod.repo_identity(b)

    # b already has a FOREIGN post-merge hook -> should be skipped.
    (b / ".git" / "hooks" / "post-merge").write_text("#!/bin/sh\necho theirs\n", encoding="utf-8")

    _init_vault(env, groups={"ga": [ident_a], "gb": [ident_b]}, roots=[root])

    results = githook.install_for_mapped_repos(env.vault, [root])
    # Only mapped repos appear; the unmapped repo c is absent.
    assert set(results) == {ident_a, ident_b}
    assert results[ident_a]["installed"] is True
    assert results[ident_b]["installed"] is False
    assert results[ident_b]["skipped"] is True
    assert "already exists" in results[ident_b]["reason"]
    # a got our hook; b's foreign hook is intact.
    assert githook.HOOK_MARKER in (a / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")
    assert "theirs" in (b / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")


def test_remove_for_mapped_repos(env):
    root = env.tmp / "Github"
    a = _git_repo(root / "a", "git@github.com:acme/a.git")
    ident_a = vault_mod.repo_identity(a)
    _init_vault(env, groups={"ga": [ident_a]}, roots=[root])
    githook.install_for_mapped_repos(env.vault, [root])

    results = githook.remove_for_mapped_repos(env.vault, [root])
    assert results[ident_a]["removed"] is True
    assert not (a / ".git" / "hooks" / "post-merge").exists()


# --------------------------------------------------------------------------- #
# deterministic metadata (pure functions)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("subject,pr,branch", [
    ("Merge pull request #12 from o/b", "12", "o/b"),
    ("Merge pull request #345 from jackneil/feature/skill-packs", "345", "jackneil/feature/skill-packs"),
    ("Merge branch 'feature/x'", None, "feature/x"),
    ("Merge branch 'hotfix' into master", None, "hotfix"),
    ("just a normal commit", None, None),
])
def test_parse_subject(subject, pr, branch):
    assert merge_capture._parse_subject(subject) == (pr, branch)


def test_fetch_pr_gh_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(merge_capture.shutil, "which", lambda name: None)
    assert merge_capture._fetch_pr(tmp_path, "12") == (None, None, False)


def test_fetch_pr_no_pr_number(tmp_path):
    assert merge_capture._fetch_pr(tmp_path, None) == (None, None, False)


def test_fetch_pr_gh_failure_tolerated(monkeypatch, tmp_path):
    monkeypatch.setattr(merge_capture.shutil, "which", lambda name: "/usr/bin/gh")

    def boom(*a, **k):
        raise OSError("no gh")
    monkeypatch.setattr(merge_capture.subprocess, "run", boom)
    assert merge_capture._fetch_pr(tmp_path, "12") == (None, None, False)


def test_prune_processed_drops_old(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    processed = {"oldsha": old, "recentsha": recent, "junksha": "not-a-date"}
    pruned = merge_capture._prune_processed(processed)
    assert "oldsha" not in pruned          # >90d dropped
    assert "recentsha" in pruned           # <90d kept
    assert "junksha" in pruned             # unparseable kept (never silently dropped)


def test_note_from_result_variants():
    assert merge_capture._note_from_result(None) is None
    assert merge_capture._note_from_result("nope") is None
    assert merge_capture._note_from_result({"skip": True}) == {"skip": True}
    assert merge_capture._note_from_result({"title": "", "body": "x"}) is None
    assert merge_capture._note_from_result({"title": "t", "body": ""}) is None
    good = merge_capture._note_from_result(
        {"title": "T", "body": "B", "tags": ["a", "", 3]}
    )
    assert good["title"] == "T" and good["body"] == "B" and good["tags"] == ["a"]


# --------------------------------------------------------------------------- #
# merge_capture end-to-end (real tmp repos)
# --------------------------------------------------------------------------- #

def test_capture_merge_writes_one_note(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo)
    _mock_distill(monkeypatch,
                  '{"title": "Chose the widget parser", "body": "We landed a new parser.", "tags": ["parser"]}')
    commits_before = _vault_commits(env.vault)

    merge_capture.capture_merge(str(repo))

    notes = _decision_notes(env.vault)
    assert len(notes) == 1
    meta, body = vault_mod.read_note(notes[0])
    assert meta["type"] == "decision"
    assert meta["group"] == "widget"
    assert meta["repos"] == ["github.com/acme/widget"]
    assert "candidate" in meta["tags"] and "merge" in meta["tags"]
    assert "parser" in meta["tags"]
    assert "We landed a new parser." in body
    # Exactly one vault commit for the run.
    assert _vault_commits(env.vault) == commits_before + 1


def test_capture_merge_dedupes_same_merge(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo)
    _mock_distill(monkeypatch, '{"title": "One", "body": "Body.", "tags": []}')

    merge_capture.capture_merge(str(repo))
    merge_capture.capture_merge(str(repo))  # second run: sha already processed

    assert len(_decision_notes(env.vault)) == 1
    st = memory.load_state(env.home)
    assert len(st["processed_merges"]) == 1


def test_capture_merge_skips_feature_branch(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo)               # merge landed on master, ORIG_HEAD set
    _git(repo, "checkout", "-q", "feature/x")  # HEAD is now a feature branch
    _mock_distill(monkeypatch, '{"title": "X", "body": "Y.", "tags": []}')

    merge_capture.capture_merge(str(repo))
    assert _decision_notes(env.vault) == []


def test_capture_merge_missing_orig_head_no_crash(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")  # committed on master, never merged
    _mock_distill(monkeypatch, '{"title": "X", "body": "Y.", "tags": []}')
    # ORIG_HEAD is unset -> clean return, no note, no exception.
    merge_capture.capture_merge(str(repo))
    assert _decision_notes(env.vault) == []


def test_capture_merge_fallback_on_distill_failure(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo, message="Merge branch 'feature/x'")
    _mock_distill(monkeypatch, None)  # distillation unavailable

    merge_capture.capture_merge(str(repo))

    notes = _decision_notes(env.vault)
    assert len(notes) == 1
    meta, body = vault_mod.read_note(notes[0])
    assert "auto-fallback" in meta["tags"]
    assert "candidate" in meta["tags"] and "merge" in meta["tags"]
    # Deterministic body references the merged branch + changed file.
    assert "feature/x" in body
    assert "feature.txt" in body


def test_capture_merge_pr_subject_gh_absent(env, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo, message="Merge pull request #12 from o/b")
    # gh truly absent -> tolerated; distillation still runs.
    monkeypatch.setattr(merge_capture.shutil, "which", lambda name: None)
    _mock_distill(monkeypatch, '{"title": "Landed PR 12", "body": "PR 12 body.", "tags": []}')

    merge_capture.capture_merge(str(repo))
    notes = _decision_notes(env.vault)
    assert len(notes) == 1
    meta, _ = vault_mod.read_note(notes[0])
    assert "candidate" in meta["tags"] and "merge" in meta["tags"]


def test_capture_merge_prunes_old_processed(env, no_gh, monkeypatch):
    _init_vault(env)
    # Seed an old processed_merges entry that a run must prune.
    st = memory.load_state(env.home)
    st["processed_merges"] = {"oldsha": "2000-01-01T00:00:00Z"}
    memory.save_state(env.home, st)

    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo)
    _mock_distill(monkeypatch, '{"title": "New", "body": "Body.", "tags": []}')

    merge_capture.capture_merge(str(repo))
    processed = memory.load_state(env.home)["processed_merges"]
    assert "oldsha" not in processed        # pruned
    assert len(processed) == 1              # only the new merge remains


def test_capture_merge_disabled_writes_nothing(env, no_gh, monkeypatch):
    _init_vault(env)
    memory.set_enabled(env.home, False)     # feature off
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo)
    _mock_distill(monkeypatch, '{"title": "X", "body": "Y.", "tags": []}')
    merge_capture.capture_merge(str(repo))
    assert _decision_notes(env.vault) == []


def test_capture_merge_multiple_merges_one_note_each(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo, message="Merge branch 'feature/x'")
    # A second feature branch + merge, all reachable in one ORIG_HEAD..HEAD window
    # requires ORIG_HEAD to span both; simulate a pull bringing two merges by
    # resetting ORIG_HEAD to the very first commit before capturing.
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "feature/y")
    (repo / "y.txt").write_text("y\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add y")
    _git(repo, "checkout", "-q", "master")
    _do_merge(repo, branch="feature/y")
    _git(repo, "update-ref", "ORIG_HEAD", first)  # window now spans both merges

    _mock_distill(monkeypatch, '{"title": "A merge", "body": "Body.", "tags": []}')
    merge_capture.capture_merge(str(repo))

    assert len(_decision_notes(env.vault)) == 2
    assert len(memory.load_state(env.home)["processed_merges"]) == 2


# --------------------------------------------------------------------------- #
# capture-merge CLI (always exits 0)
# --------------------------------------------------------------------------- #

def test_cli_capture_merge_exits_zero_on_success(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")  # no merge -> nothing to do
    r = CliRunner().invoke(main, ["memory", "capture-merge", "--repo", str(repo)])
    assert r.exit_code == 0


def test_cli_capture_merge_exits_zero_when_everything_raises(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr("jacked.memory.merge_capture.capture_merge", boom)
    r = CliRunner().invoke(main, ["memory", "capture-merge", "--repo", str(env.tmp)])
    assert r.exit_code == 0


def test_pr_merge_note_carries_pr_provenance_tag(env, monkeypatch):
    """A note distilled from a PR merge is tagged pr-<n>: its content may
    originate from a contributor, and recall/librarian treat provenance-tagged
    candidates accordingly (CSO hardening)."""
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo, message="Merge pull request #77 from o/b")
    monkeypatch.setattr(merge_capture.shutil, "which", lambda name: None)
    _mock_distill(monkeypatch, '{"title": "Landed PR 77", "body": "PR body.", "tags": ["x"]}')

    merge_capture.capture_merge(str(repo))
    notes = _decision_notes(env.vault)
    assert len(notes) == 1
    meta, _ = vault_mod.read_note(notes[0])
    assert "pr-77" in meta["tags"]
    assert "candidate" in meta["tags"] and "merge" in meta["tags"]


def test_branch_merge_note_has_no_pr_tag(env, no_gh, monkeypatch):
    _init_vault(env)
    repo = _merge_repo(env.tmp / "widget")
    _do_merge(repo, message="Merge branch 'feature/x'")
    _mock_distill(monkeypatch, '{"title": "Landed branch", "body": "Body.", "tags": []}')

    merge_capture.capture_merge(str(repo))
    notes = _decision_notes(env.vault)
    assert len(notes) == 1
    meta, _ = vault_mod.read_note(notes[0])
    assert not any(t.startswith("pr-") for t in meta["tags"])


# --------------------------------------------------------------------------- #
# F10: post-merge PATH hardening (@JACKED_BIN@ substituted at install time)
# --------------------------------------------------------------------------- #

def test_template_has_jacked_bin_placeholder():
    src = githook._GIT_HOOKS_DIR / githook._TEMPLATE_NAME
    content = src.read_text(encoding="utf-8")
    assert "@JACKED_BIN@" in content
    assert githook.HOOK_MARKER in content
    assert "command -v jacked" in content  # PATH fallback branch


def test_install_substitutes_absolute_jacked_path(env, monkeypatch):
    monkeypatch.setattr(githook, "find_bin", lambda name: "/opt/tools/jacked")
    repo = _bare_git_dir(env.tmp / "repo")
    res = githook.install_post_merge(repo)
    assert res["installed"] is True
    content = (repo / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")
    assert 'JACKED_BIN="/opt/tools/jacked"' in content
    assert "@JACKED_BIN@" not in content  # placeholder fully substituted


def test_install_refresh_in_place_compares_substituted(env, monkeypatch):
    monkeypatch.setattr(githook, "find_bin", lambda name: "/opt/tools/jacked")
    repo = _bare_git_dir(env.tmp / "repo")
    assert githook.install_post_merge(repo)["installed"] is True
    # Same resolved path -> already installed (compares SUBSTITUTED content).
    second = githook.install_post_merge(repo)
    assert second["installed"] is False
    assert "already installed" in second["reason"]
    # A changed resolved path -> refresh in place.
    monkeypatch.setattr(githook, "find_bin", lambda name: "/opt/tools/jacked-new")
    third = githook.install_post_merge(repo)
    assert third["installed"] is True
    content = (repo / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")
    assert 'JACKED_BIN="/opt/tools/jacked-new"' in content


def test_install_empty_jacked_bin_when_unresolved(env, monkeypatch):
    monkeypatch.setattr(githook, "find_bin", lambda name: None)
    repo = _bare_git_dir(env.tmp / "repo")
    githook.install_post_merge(repo)
    content = (repo / ".git" / "hooks" / "post-merge").read_text(encoding="utf-8")
    assert 'JACKED_BIN=""' in content
    assert "command -v jacked" in content  # PATH fallback still present
