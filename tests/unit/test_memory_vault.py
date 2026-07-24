"""Tests for the M1 memory-vault core: vault.py + the `jacked memory` CLI group.

Every test isolates JACKED_HOME and JACKED_VAULT_DIR under tmp_path so nothing
touches the real ~/.claude or ~/jacked-vault. Vault git commits get a git
identity via env (and vault.py injects a `-c` fallback of its own), so commits
work in CI. Rich CLI output is captured by swapping cli.console for a
StringIO-backed Console, mirroring tests/test_cli_packs.py.
"""
import json
import subprocess
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked import memory
from jacked.cli import main
from jacked.memory import vault as vault_mod

# Repo root, so a spawned `python -c` subprocess resolves the editable jacked.
REPO_ROOT_VAULT = Path(__file__).resolve().parents[2]


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
    # Git identity so vault commits succeed even on a bare CI runner.
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)

    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))
    return SimpleNamespace(home=home, vault=vault, tmp=tmp_path, buf=buf, monkeypatch=monkeypatch)


def _make_repo(path, remote=None):
    """A minimal git repo (optionally with an origin remote)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True, text=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                       check=True, capture_output=True, text=True)
    return path


def _log_count(vault):
    r = subprocess.run(["git", "-C", str(vault), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0)


def _fixture_root(env):
    """A root dir with three repos: two 'hank-*' (cluster) + one 'claude-jacked'."""
    root = env.tmp / "Github"
    _make_repo(root / "hank-captable", "git@github.com:jackneil/hank-captable.git")
    _make_repo(root / "hank-scholar", "https://github.com/jackneil/hank-scholar.git")
    _make_repo(root / "claude-jacked", "git@github.com:jackneil/claude-jacked.git")
    return root


def _init_with(env, root, extra=None):
    args = ["memory", "init", "--root", str(root), "--yes", *(extra or [])]
    return CliRunner().invoke(main, args)


def _init_empty(env):
    empty = env.tmp / "emptyroot"
    empty.mkdir()
    r = _init_with(env, empty)
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    return r


def _today():
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------- #
# normalize_remote / repo_identity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url,expected", [
    ("git@github.com:jackneil/claude-jacked.git", "github.com/jackneil/claude-jacked"),
    ("https://github.com/jackneil/hank-scholar.git", "github.com/jackneil/hank-scholar"),
    ("https://user:pass@github.com/owner/repo.git", "github.com/owner/repo"),
    ("ssh://git@gitlab.com/team/proj", "gitlab.com/team/proj"),
    ("https://github.com/Owner/Repo", "github.com/owner/repo"),
    ("", ""),
])
def test_normalize_remote(url, expected):
    assert vault_mod.normalize_remote(url) == expected


def test_repo_identity_remote_and_fallback(env):
    with_remote = _make_repo(env.tmp / "r1", "git@github.com:acme/widget.git")
    assert vault_mod.repo_identity(with_remote) == "github.com/acme/widget"
    # No remote -> sanitized dir name.
    no_remote = _make_repo(env.tmp / "LonelyRepo")
    assert vault_mod.repo_identity(no_remote) == "lonelyrepo"
    # Not a repo at all -> sanitized dir name.
    plain = env.tmp / "just_a_dir"
    plain.mkdir()
    assert vault_mod.repo_identity(plain) == "just-a-dir"


# --------------------------------------------------------------------------- #
# scan / suggest
# --------------------------------------------------------------------------- #

def test_scan_and_suggest_groups(env):
    root = _fixture_root(env)
    discovered = vault_mod.scan_roots([root])
    idents = {i for _, i in discovered}
    assert idents == {
        "github.com/jackneil/hank-captable",
        "github.com/jackneil/hank-scholar",
        "github.com/jackneil/claude-jacked",
    }
    groups = vault_mod.suggest_groups(discovered)
    # Shared org + shared 'hank' prefix clusters into one group.
    assert set(groups["hank"]) == {
        "github.com/jackneil/hank-captable",
        "github.com/jackneil/hank-scholar",
    }
    # The odd one out is its own solo group named after the repo.
    assert groups["claude-jacked"] == ["github.com/jackneil/claude-jacked"]


def test_suggest_groups_unique_names_on_collision(env):
    # Two different orgs, same 'hank' token -> the second cluster's name is
    # disambiguated instead of clobbering the first.
    discovered = [
        (env.tmp / "a", "github.com/orgA/hank-x"),
        (env.tmp / "b", "github.com/orgA/hank-y"),
        (env.tmp / "c", "github.com/orgB/hank-p"),
        (env.tmp / "d", "github.com/orgB/hank-q"),
    ]
    groups = vault_mod.suggest_groups(discovered)
    assert "hank" in groups
    assert "hank-2" in groups
    assert len(groups) == 2


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

def test_init_writes_vault_skeleton_and_git(env):
    root = _fixture_root(env)
    r = _init_with(env, root)
    assert r.exit_code == 0, r.output + env.buf.getvalue()

    vault = env.vault
    assert (vault / ".git").is_dir()
    assert (vault / "README.md").exists()
    assert (vault / "strategy").is_dir()
    assert (vault / "archive").is_dir()
    assert (vault / "vault.json").exists()

    cfg = json.loads((vault / "vault.json").read_text())
    assert cfg["version"] == 1
    assert "hank" in cfg["groups"]
    assert cfg["repo_map"]["github.com/jackneil/hank-captable"] == "hank"
    # Group skeletons exist.
    assert (vault / "groups" / "hank" / "index.md").exists()
    assert (vault / "groups" / "hank" / "hot.md").exists()
    # Exactly one initial commit.
    assert _log_count(vault) == 1

    # init flips the state enabled and records the vault path.
    st = memory.load_state(env.home)
    assert st["enabled"] is True
    assert st["vault_dir"] == str(vault)


def test_init_is_idempotent_and_safe(env):
    root = _fixture_root(env)
    assert _init_with(env, root).exit_code == 0
    before = (env.vault / "vault.json").read_text()
    commits_before = _log_count(env.vault)

    env.buf.truncate(0)
    env.buf.seek(0)
    r = _init_with(env, root)
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "already initialized" in out
    # Nothing clobbered, no new commit.
    assert (env.vault / "vault.json").read_text() == before
    assert _log_count(env.vault) == commits_before


def test_init_empty_root_creates_empty_vault(env):
    _init_empty(env)
    cfg = json.loads((env.vault / "vault.json").read_text())
    assert cfg["groups"] == {}
    assert (env.vault / ".git").is_dir()


# --------------------------------------------------------------------------- #
# W5: `jacked memory init` enables the feature end-to-end (hooks + git hooks)
# --------------------------------------------------------------------------- #

def test_init_installs_hooks_and_reports(env):
    root = _fixture_root(env)
    r = _init_with(env, root)
    assert r.exit_code == 0, r.output + env.buf.getvalue()

    # settings.json now carries BOTH the capture and recall hook entries.
    settings_text = (env.home / ".claude" / "settings.json").read_text()
    assert "memory_capture" in settings_text
    assert "memory_recall" in settings_text

    # The post-merge git hook landed in a mapped repo.
    assert (root / "hank-captable" / ".git" / "hooks" / "post-merge").exists()

    # The output tells the user hooks were installed.
    assert "hooks installed" in env.buf.getvalue().lower()


def test_init_corrupt_settings_creates_vault_but_aborts_on_hooks(env):
    # A pre-existing but unreadable settings.json must NOT be clobbered.
    settings_path = env.home / ".claude" / "settings.json"
    corrupt = "{ this is not valid json"
    settings_path.write_text(corrupt, encoding="utf-8")

    empty = env.tmp / "emptyroot2"
    empty.mkdir()
    r = _init_with(env, empty)

    assert r.exit_code == 2
    out = r.output + env.buf.getvalue()
    assert "settings.json is unreadable" in out
    # The vault WAS created (init ran)...
    assert (env.vault / "vault.json").exists()
    # ...but the corrupt settings.json is left exactly as it was (never clobbered).
    assert settings_path.read_text() == corrupt


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #

def test_add_writes_note_index_and_commit(env):
    _init_empty(env)
    commits = _log_count(env.vault)

    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "Test decision",
        "--group", "hank", "--repos", "github.com/jackneil/hank-captable",
        "--tags", "candidate,milestone", "--body", "We chose X because Y.",
    ])
    assert r.exit_code == 0, r.output + env.buf.getvalue()

    note = env.vault / "groups" / "hank" / "decision" / "test-decision.md"
    assert note.exists()
    meta, body = vault_mod.parse_frontmatter(note.read_text())
    assert meta["type"] == "decision"
    assert meta["group"] == "hank"
    assert meta["repos"] == ["github.com/jackneil/hank-captable"]
    assert meta["tags"] == ["candidate", "milestone"]
    assert meta["created"] == _today()
    assert "We chose X because Y." in body

    index = (env.vault / "groups" / "hank" / "index.md").read_text()
    assert "[Test decision](decision/test-decision.md)" in index
    assert "We chose X because Y." in index  # first-sentence hook

    # A new vault commit landed and the drift counter advanced.
    assert _log_count(env.vault) == commits + 1
    assert memory.load_state(env.home)["drift_added"] == 1


def test_add_auto_group_from_cwd(env):
    root = _fixture_root(env)
    assert _init_with(env, root).exit_code == 0
    # Work from inside a mapped repo; no --group -> resolves to its group.
    env.monkeypatch.chdir(root / "hank-scholar")
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "progress", "--title", "Did a thing",
        "--body", "Landed the parser.",
    ])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert (env.vault / "groups" / "hank" / "progress" / "did-a-thing.md").exists()
    # repos defaulted to the current repo identity.
    meta, _ = vault_mod.read_note(
        env.vault / "groups" / "hank" / "progress" / "did-a-thing.md"
    )
    assert meta["repos"] == ["github.com/jackneil/hank-scholar"]


def test_add_unmapped_repo_creates_solo_group(env):
    _init_empty(env)
    loner = _make_repo(env.tmp / "loner", "git@github.com:someone/loner.git")
    env.monkeypatch.chdir(loner)
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "reference", "--title", "A ref",
        "--body", "Useful link.",
    ])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert (env.vault / "groups" / "loner" / "reference" / "a-ref.md").exists()
    cfg = json.loads((env.vault / "vault.json").read_text())
    assert cfg["repo_map"]["github.com/someone/loner"] == "loner"


def test_add_slug_sanitizes_traversal(env):
    _init_empty(env)
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "../../etc/passwd & danger!",
        "--group", "hank", "--body", "Nope.",
    ])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    decision_dir = env.vault / "groups" / "hank" / "decision"
    files = list(decision_dir.glob("*.md"))
    assert len(files) == 1
    name = files[0].name
    assert "/" not in name and ".." not in name
    assert name == "etc-passwd-danger.md"
    # Nothing escaped the group dir.
    assert files[0].resolve().is_relative_to((env.vault / "groups" / "hank").resolve())


def test_add_duplicate_slug_gets_suffix(env):
    _init_empty(env)
    for _ in range(2):
        r = CliRunner().invoke(main, [
            "memory", "add", "--type", "decision", "--title", "Same title",
            "--group", "hank", "--body", "Body text here.",
        ])
        assert r.exit_code == 0, r.output + env.buf.getvalue()
    decision_dir = env.vault / "groups" / "hank" / "decision"
    names = sorted(p.name for p in decision_dir.glob("*.md"))
    assert names == ["same-title-2.md", "same-title.md"]


def test_add_body_from_stdin(env):
    _init_empty(env)
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "vision", "--title", "Big idea",
        "--group", "strat", "--body", "-",
    ], input="Streamed body content.\n")
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    _, body = vault_mod.read_note(env.vault / "groups" / "strat" / "vision" / "big-idea.md")
    assert "Streamed body content." in body


def test_add_before_init_fails_cleanly(env):
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "x",
        "--group", "g", "--body", "y",
    ])
    assert r.exit_code == 2
    assert "not initialized" in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# W4: add_note joins the vault-write lock contract; CLI git-timeout stays clean
# --------------------------------------------------------------------------- #

def test_add_note_commit_acquires_vault_lock_but_commit_false_does_not(env):
    import contextlib

    vault_mod.init_vault(env.vault, [], {})
    vault_mod.ensure_group(env.vault, "g")

    calls: list[str] = []
    real_lock = vault_mod.vault_write_lock

    @contextlib.contextmanager
    def _recording_lock(vault):
        calls.append(str(vault))
        with real_lock(vault):
            yield

    env.monkeypatch.setattr(vault_mod, "vault_write_lock", _recording_lock)

    # commit=True -> the commit path takes the vault-write lock exactly once.
    vault_mod.add_note(env.vault, env.home, note_type="decision", title="Locked add",
                       group="g", repos=["g"], tags=[], body="body", commit=True)
    assert len(calls) == 1

    # commit=False -> the caller owns the lock; add_note must NOT double-acquire.
    calls.clear()
    vault_mod.add_note(env.vault, env.home, note_type="decision", title="Batched add",
                       group="g", repos=["g"], tags=[], body="body", commit=False)
    assert calls == []


def test_cli_add_git_timeout_prints_clean_failure(env):
    _init_empty(env)

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    env.monkeypatch.setattr(vault_mod, "_run_git", _timeout)
    r = CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "Timeout note",
        "--group", "hank", "--body", "body",
    ])
    assert r.exit_code == 2
    out = r.output + env.buf.getvalue()
    assert "vault git operation timed out or failed" in out
    # A clean SystemExit, never a raw TimeoutExpired traceback.
    assert not isinstance(r.exception, subprocess.TimeoutExpired)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def _seed_search_vault(env):
    _init_empty(env)
    # 'zephyr' lives in the SECOND sentence, so it is NOT in the index hook
    # (first sentence) -- lets the note-body tier + --type filter be exercised.
    CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "D one",
        "--group", "g1", "--body", "First sentence. Contains zephyr keyword.",
    ])
    CliRunner().invoke(main, [
        "memory", "add", "--type", "convention", "--title", "C one",
        "--group", "g1", "--body", "Another first. Also zephyr keyword here.",
    ])
    CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "Faraway",
        "--group", "g2", "--body", "Nothing to see. Just plain text.",
    ])


def test_search_finds_body_keyword(env):
    _seed_search_vault(env)
    res = memory.search(env.vault, "zephyr")
    paths = {r[0] for r in res}
    assert any("g1/decision/d-one.md" in p for p in paths)
    assert any("g1/convention/c-one.md" in p for p in paths)


def test_search_type_filter(env):
    _seed_search_vault(env)
    res = memory.search(env.vault, "zephyr", note_type="convention")
    paths = {r[0] for r in res}
    assert paths and all("convention" in p for p in paths)
    assert not any("decision" in p for p in paths)


def test_search_group_scope(env):
    _seed_search_vault(env)
    # 'zephyr' only exists in g1; scoping to g2 yields nothing.
    assert memory.search(env.vault, "zephyr", group="g2") == []
    assert memory.search(env.vault, "zephyr", group="g1")


def test_search_hot_and_index_tiers(env):
    _seed_search_vault(env)
    (env.vault / "groups" / "g1" / "hot.md").write_text(
        "# Hot: g1\n\nWorking on hotword right now.\n", encoding="utf-8"
    )
    # hot.md tier
    hot = memory.search(env.vault, "hotword")
    assert hot and hot[0][0].endswith("g1/hot.md")
    # index tier: the title appears in the index line
    idx = memory.search(env.vault, "D one")
    assert any(p.endswith("g1/index.md") for p, _, _ in idx)


def test_search_limit(env):
    _init_empty(env)
    for i in range(5):
        CliRunner().invoke(main, [
            "memory", "add", "--type", "reference", "--title", f"Note {i}",
            "--group", "g", "--body", "shared token appears here.",
        ])
    res = memory.search(env.vault, "shared", limit=3)
    assert len(res) == 3


def test_cli_search_prints_path_line(env):
    _seed_search_vault(env)
    env.buf.truncate(0)
    env.buf.seek(0)
    r = CliRunner().invoke(main, ["memory", "search", "zephyr"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "zephyr" in out
    assert "decision/d-one.md:" in out or "convention/c-one.md:" in out


def test_cli_search_no_match(env):
    _seed_search_vault(env)
    env.buf.truncate(0)
    env.buf.seek(0)
    r = CliRunner().invoke(main, ["memory", "search", "nonexistentxyz"])
    assert r.exit_code == 0
    assert "No matches" in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# frontmatter parse / serialize / validate
# --------------------------------------------------------------------------- #

def test_frontmatter_round_trip():
    meta = {
        "type": "decision",
        "repos": ["github.com/a/b", "github.com/a/c"],
        "group": "hank",
        "created": "2026-07-18",
        "updated": "2026-07-18",
        "tags": ["candidate"],
    }
    text = vault_mod.render_note(meta, "The body.\n\nSecond paragraph.")
    parsed, body = vault_mod.parse_frontmatter(text)
    assert parsed == meta
    assert body.strip() == "The body.\n\nSecond paragraph."


def test_frontmatter_parses_inline_lists():
    text = (
        "---\n"
        "type: convention\n"
        "repos: [github.com/a/b, github.com/a/c]\n"
        "group: g\n"
        "created: 2026-07-18\n"
        "updated: 2026-07-18\n"
        "tags: []\n"
        "---\n\n"
        "Body here.\n"
    )
    meta, body = vault_mod.parse_frontmatter(text)
    assert meta["repos"] == ["github.com/a/b", "github.com/a/c"]
    assert meta["tags"] == []
    assert body.strip() == "Body here."


def test_frontmatter_tolerates_missing_optional_and_no_frontmatter():
    meta, body = vault_mod.parse_frontmatter("just a body, no frontmatter")
    assert meta == {}
    assert body == "just a body, no frontmatter"

    text = "---\ntype: reference\nrepos:\n  - x\ngroup: g\ncreated: d\nupdated: d\n---\nB"
    meta, _ = vault_mod.parse_frontmatter(text)
    assert "tags" not in meta  # optional key simply absent
    assert meta["repos"] == ["x"]


def test_validate_note_enforces_schema():
    good = {
        "type": "decision", "repos": ["r"], "group": "g",
        "created": "d", "updated": "d", "tags": [],
    }
    vault_mod.validate_note(good)  # no raise
    with pytest.raises(ValueError):
        vault_mod.validate_note({**good, "type": "bogus"})
    with pytest.raises(ValueError):
        vault_mod.validate_note({**good, "repos": []})
    with pytest.raises(ValueError):
        vault_mod.validate_note({**good, "group": ""})


# --------------------------------------------------------------------------- #
# state: load/save/normalize
# --------------------------------------------------------------------------- #

def test_state_default_when_missing(env):
    st = memory.load_state(env.home)
    assert st["version"] == 1
    assert st["enabled"] is False
    assert st["triage_model"] == "haiku"
    assert st["drift_threshold"] == 15
    assert st["retry_queue"] == []
    assert st["processed_merges"] == {}


def test_state_round_trip_and_atomic(env):
    st = memory.load_state(env.home)
    st["enabled"] = True
    st["drift_added"] = 3
    memory.save_state(env.home, st)
    again = memory.load_state(env.home)
    assert again["enabled"] is True
    assert again["drift_added"] == 3
    # No leftover temp files from the atomic write.
    claude = env.home / ".claude"
    assert not list(claude.glob(".jacked-memory.json.*.tmp"))


def test_state_tolerates_corrupt_file(env):
    path = vault_mod.state_path(env.home)
    path.write_text("{ this is not json", encoding="utf-8")
    st = memory.load_state(env.home)
    assert st == vault_mod._default_state()


def test_state_preserves_unknown_keys(env):
    path = vault_mod.state_path(env.home)
    path.write_text(json.dumps({
        "version": 99, "enabled": True, "future_flag": "keep me",
    }), encoding="utf-8")
    st = memory.load_state(env.home)
    assert st["future_flag"] == "keep me"
    assert st["version"] == 99  # newer version label retained
    memory.save_state(env.home, st)
    reloaded = json.loads(path.read_text())
    assert reloaded["future_flag"] == "keep me"


def test_bump_drift(env):
    assert memory.bump_drift(env.home) == 1
    assert memory.bump_drift(env.home, 2) == 3


# --------------------------------------------------------------------------- #
# status + --quiet
# --------------------------------------------------------------------------- #

def test_status_quiet_exit_codes(env):
    # Before init: not initialized + not enabled -> exit 1.
    r = CliRunner().invoke(main, ["memory", "status", "--quiet"])
    assert r.exit_code == 1
    assert env.buf.getvalue() == ""

    _init_empty(env)
    r = CliRunner().invoke(main, ["memory", "status", "--quiet"])
    assert r.exit_code == 0

    # Disable -> exit 1 again.
    memory.set_enabled(env.home, False)
    r = CliRunner().invoke(main, ["memory", "status", "--quiet"])
    assert r.exit_code == 1


def test_status_renders_groups_and_counts(env):
    _init_empty(env)
    CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "One",
        "--group", "hank", "--body", "Body.",
    ])
    env.buf.truncate(0)
    env.buf.seek(0)
    r = CliRunner().invoke(main, ["memory", "status"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "hank" in out
    assert "enabled" in out
    assert str(env.vault) in out


# --------------------------------------------------------------------------- #
# no em-dash in user-facing CLI output (mirrors test_cli_packs.py:640)
# --------------------------------------------------------------------------- #

def test_no_em_dash_in_command_output(env):
    root = _fixture_root(env)
    _init_with(env, root)
    CliRunner().invoke(main, [
        "memory", "add", "--type", "decision", "--title", "A decision",
        "--group", "hank", "--body", "We picked A over B.",
    ])
    CliRunner().invoke(main, ["memory", "status"])
    assert "—" not in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# F7: slugify collision fix (empty-after-sanitize -> deterministic note-<hash>)
# --------------------------------------------------------------------------- #

def test_slugify_empty_returns_deterministic_hash():
    import hashlib

    slug = vault_mod.slugify("..")
    expected = "note-" + hashlib.sha1(b"..").hexdigest()[:8]
    assert slug == expected
    # Stable across calls.
    assert vault_mod.slugify("..") == expected


def test_slugify_distinct_non_ascii_names_stay_distinct():
    # Two different CJK repo names both sanitize to empty; they must NOT collide
    # onto one slug/group (the pre-fix "note" constant merged them).
    a = vault_mod.slugify("東京")      # Tokyo
    b = vault_mod.slugify("大阪")      # Osaka
    assert a != b
    assert a.startswith("note-") and b.startswith("note-")
    # Deterministic per input.
    assert vault_mod.slugify("東京") == a


def test_slugify_normal_ascii_unchanged():
    assert vault_mod.slugify("Hank Captable") == "hank-captable"
    assert vault_mod.slugify("../../etc/passwd") == "etc-passwd"


# --------------------------------------------------------------------------- #
# F8: capture health fields in default state + status
# --------------------------------------------------------------------------- #

def test_default_state_has_health_fields(env):
    st = vault_mod.load_state(env.home)
    assert st["last_capture"] is None
    assert st["last_capture_error"] is None
    assert st["capture_failures"] == 0


def test_normalize_coerces_bad_health_fields(env):
    path = vault_mod.state_path(env.home)
    path.write_text(json.dumps({
        "version": 1, "capture_failures": "lots",
        "last_capture": 123, "last_capture_error": ["nope"],
    }), encoding="utf-8")
    st = vault_mod.load_state(env.home)
    assert st["capture_failures"] == 0
    assert st["last_capture"] is None
    assert st["last_capture_error"] is None


def test_status_surfaces_health(env):
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)
    vault_mod.update_state(env.home, lambda s: s.update({
        "last_capture": "2026-07-18T10:00:00Z",
        "last_capture_error": "triage failed",
        "capture_failures": 3,
    }))
    st = vault_mod.status(env.vault, env.home)
    assert st["last_capture"] == "2026-07-18T10:00:00Z"
    assert st["last_capture_error"] == "triage failed"
    assert st["capture_failures"] == 3


# --------------------------------------------------------------------------- #
# F3: cross-process update_state (concurrent writers keep both updates)
# --------------------------------------------------------------------------- #

def test_update_state_concurrent_writers_keep_both(env):
    # Two subprocesses each bump a DIFFERENT key under the state lock; both must
    # survive (a naive load/save would lose one).
    import sys
    import textwrap

    vault_mod.save_state(env.home, vault_mod.load_state(env.home))
    prog = textwrap.dedent(
        """
        import os, sys
        os.environ["JACKED_HOME"] = sys.argv[1]
        from jacked.memory import vault as v
        key = sys.argv[2]
        for _ in range(20):
            v.update_state(None, lambda s: s.__setitem__(key, int(s.get(key, 0)) + 1))
        """
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", prog, str(env.home), key],
                         cwd=str(REPO_ROOT_VAULT))
        for key in ("drift_added", "capture_failures")
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    st = vault_mod.load_state(env.home)
    assert st["drift_added"] == 20
    assert st["capture_failures"] == 20


def test_file_lock_timeout_yields_with_warning(env, caplog):
    import threading

    lock_path = env.home / ".claude" / "held.lock"
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with vault_mod._file_lock(lock_path, timeout=5.0):
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5)

    # A second acquire with a tiny timeout can't get the lock; it must warn and
    # yield anyway (fail-open) rather than block or raise.
    ran = []
    with caplog.at_level("WARNING"):
        with vault_mod._file_lock(lock_path, timeout=0.2):
            ran.append(True)
    release.set()
    t.join(timeout=10)

    assert ran == [True]
    assert any("unavailable" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# F9: durable memory failure log (rotating handler attached once)
# --------------------------------------------------------------------------- #

def test_ensure_memory_file_logging_attaches_once(env):
    import contextlib
    import logging

    log = logging.getLogger(vault_mod.MEMORY_LOGGER_NAME)
    # The logger is a global singleton; a handler left by another test points at a
    # stale home and would make ensure() short-circuit. Clear ours first.
    stale = [h for h in log.handlers if getattr(h, "_jacked_memory_handler", False)]
    for h in stale:
        log.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    try:
        vault_mod.ensure_memory_file_logging(env.home)
        vault_mod.ensure_memory_file_logging(env.home)  # idempotent
        ours = [h for h in log.handlers if getattr(h, "_jacked_memory_handler", False)]
        assert len(ours) == 1
        log.warning("a durable memory warning")
        assert vault_mod.memory_log_path(env.home).exists()
        assert "a durable memory warning" in vault_mod.memory_log_path(env.home).read_text(encoding="utf-8")
    finally:
        for h in [h for h in log.handlers if getattr(h, "_jacked_memory_handler", False)]:
            log.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()


# --------------------------------------------------------------------------- #
# F12: `jacked memory status` honesty cross-check
# --------------------------------------------------------------------------- #

def test_status_warns_when_enabled_but_capture_hook_missing(env):
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)
    # settings.json exists but carries NO capture hook entry.
    (env.home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    r = CliRunner().invoke(main, ["memory", "status"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "capture hook is not installed" in out
    assert "—" not in out  # user-facing, em-dash free


def test_status_warns_when_settings_unreadable(env):
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)
    (env.home / ".claude" / "settings.json").write_text("{ corrupt json", encoding="utf-8")

    r = CliRunner().invoke(main, ["memory", "status"])
    assert r.exit_code == 0
    assert "unreadable" in env.buf.getvalue()
