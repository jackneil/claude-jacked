"""Tests for M6: migrating the ``.remember`` plugin history into the vault, and
retiring the plugin.

The binding invariant is data integrity: sources are read-only (byte + mtime
unchanged), imported entry counts are verified against source, and any mismatch
fails the repo loud with nothing landing in the vault. Env isolation mirrors
test_memory_vault.py -- JACKED_HOME / JACKED_VAULT_DIR under tmp_path, a git
identity via env, and cli.console swapped for a StringIO Console.
"""
import json
import subprocess
from datetime import datetime
from io import StringIO
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked.cli import main
from jacked.memory import migrate as migrate_mod
from jacked.memory import vault as vault_mod
from tests._platform import requires_symlinks


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


def _make_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                       check=True, capture_output=True, text=True)
    return path


def _log_count(vault):
    r = subprocess.run(["git", "-C", str(vault), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0)


def _neutral_cwd(env):
    """chdir to a non-repo dir so ``discover``'s cwd-repo fallback picks up
    nothing (the real claude-jacked/.remember must never leak into a test)."""
    d = env.tmp / "neutral"
    d.mkdir(exist_ok=True)
    env.monkeypatch.chdir(d)
    return d


def _current_day():
    return datetime.now().strftime("%Y-%m-%d")


# The recon shapes, faithfully reproduced (NOT Jack's real data). Entry counts by
# the plugin header regex are annotated per file.
_NOW_MD = "\n## 11:21 | feature/x\nLive buffer entry.\n"                       # 1 entry
_TODAY_DONE = "## 08:00 | master\nOld done entry.\n## 08:48-09:22 | master\nRange entry.\n"  # 2
_TODAY_PAST = "## 10:00 | master\nA past non-done day.\n"                       # 1
_RECENT_MALFORMED = (                                                           # 1 (## date)
    "# Recent\n\n"
    "```\n\n"
    "# Recent\n\n"
    "## 2026-07-15\n"
    "Released v0.82.0.\n\n"
    "## Identity Candidates\n"
    "- IDENTITY CANDIDATE: A candidate line.\n"
)
_ARCHIVE = (                                                                    # 2 (## Week of)
    "# Archive\n\n"
    "## Week of 2026-07-08\nWeek one summary.\n\n"
    "## Week of 2026-07-01\nWeek two summary.\n"
)
_CORE = (                                                                       # 1 opaque blob; 2 candidates
    "# Core Memories\n\n"
    "## Chose SQLite over Postgres\nSingle-node deploy, so SQLite.\n\n"
    "## Renamed billing to ledger\nDomain language alignment.\n"
)
_IDENTITY = "# Identity\n\n## Who I Am\nA coding agent.\n\n## Values\nCorrectness.\n"  # 1 opaque blob


def _today_current():
    # 2 entries: a 24h header and a 12h header.
    return "## 07:01 | master\nDid a thing.\n## 9:41 AM | main\nMorning thing.\n"


def _write_remember(remember_dir, *, extra_marker=""):
    """Build a realistic ``.remember`` mirroring the recon. ``extra_marker`` is
    embedded in recent.md so a targeted tamper can single out this repo."""
    remember_dir.mkdir(parents=True, exist_ok=True)
    (remember_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    (remember_dir / "now.md").write_text(_NOW_MD, encoding="utf-8")
    (remember_dir / f"today-{_current_day()}.md").write_text(_today_current(), encoding="utf-8")
    (remember_dir / "today-2026-06-26.done.md").write_text(_TODAY_DONE, encoding="utf-8")
    (remember_dir / "today-2026-06-27.md").write_text(_TODAY_PAST, encoding="utf-8")
    recent = _RECENT_MALFORMED
    if extra_marker:
        recent = recent + f"\n{extra_marker}\n"
    (remember_dir / "recent.md").write_text(recent, encoding="utf-8")
    (remember_dir / "archive.md").write_text(_ARCHIVE, encoding="utf-8")
    (remember_dir / "core-memories.md").write_text(_CORE, encoding="utf-8")
    (remember_dir / "identity.md").write_text(_IDENTITY, encoding="utf-8")
    return remember_dir


def _fixture_repo(env, root_name="Github", repo_name="myrepo", org="acme", **kw):
    root = env.tmp / root_name
    repo = _make_repo(root / repo_name, f"git@github.com:{org}/{repo_name}.git")
    _write_remember(repo / ".remember", **kw)
    return root, repo


def _init(env, root):
    r = CliRunner().invoke(main, ["memory", "init", "--root", str(root), "--yes"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    return r


def _snapshot(remember_dir):
    """{relpath: (bytes, st_mtime_ns)} for every file under a .remember dir."""
    snap = {}
    for p in sorted(remember_dir.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(remember_dir))] = (p.read_bytes(), st.st_mtime_ns)
    return snap


def _remember_plugin_retired(env) -> bool:
    """True iff settings.json has disabled the remember plugin (enabledPlugins
    entry set to False). `memory init` now creates settings.json for the hooks,
    so the retirement check reads the plugin entry, not the file's existence."""
    path = env.home / ".claude" / "settings.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("enabledPlugins", {}).get(migrate_mod.REMEMBER_PLUGIN_ID) is False


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #

def test_discover_finds_only_dirs_with_recognizable_files(env):
    _neutral_cwd(env)
    root = env.tmp / "Github"
    # A repo with a real .remember.
    good = _make_repo(root / "good", "git@github.com:acme/good.git")
    _write_remember(good / ".remember")
    # A repo whose .remember has NO recognizable memory files (just logs).
    empty = _make_repo(root / "empty", "git@github.com:acme/empty.git")
    (empty / ".remember" / "logs").mkdir(parents=True)
    (empty / ".remember" / "notes.txt").write_text("junk", encoding="utf-8")
    # A repo with no .remember at all.
    _make_repo(root / "plain", "git@github.com:acme/plain.git")

    found = migrate_mod.discover_remember_dirs([root])
    repos = {p.name for p, _ in found}
    assert repos == {"good"}


def test_discover_includes_cwd_repo_outside_roots(env):
    root = env.tmp / "Github"
    root.mkdir()
    outside = _make_repo(env.tmp / "elsewhere" / "solo", "git@github.com:acme/solo.git")
    _write_remember(outside / ".remember")
    env.monkeypatch.chdir(outside)
    found = migrate_mod.discover_remember_dirs([root])  # root is empty
    assert any(p.resolve() == outside.resolve() for p, _ in found)


# --------------------------------------------------------------------------- #
# full migrate
# --------------------------------------------------------------------------- #

def test_full_migrate_lands_files_with_matching_counts(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    commits_before = _log_count(env.vault)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])

    assert report["repos_migrated"] == 1
    assert report["repos_failed"] == 0
    rep = report["repos"][str(repo)]
    assert rep["status"] == "migrated"

    # Every source file's staged count equals its source count.
    files = rep["files"]
    for name, counts in files.items():
        assert counts["staged_entries"] == counts["source_entries"], name
    # The specific counts we constructed.
    cur = f"today-{_current_day()}.md"
    assert files["today-2026-06-26.done.md"]["source_entries"] == 2
    assert files["today-2026-06-27.md"]["source_entries"] == 1
    assert files[cur]["source_entries"] == 2         # today's own entries, sans fold-in
    assert files["recent.md"]["source_entries"] == 1
    assert files["archive.md"]["source_entries"] == 2
    assert files["core-memories.md"]["source_entries"] == 1  # opaque blob
    assert files["identity.md"]["source_entries"] == 1       # opaque blob
    assert files["now.md"]["source_entries"] == 1

    # Files landed in the right episodic dir.
    edir = env.vault / "groups" / "myrepo" / "episodic" / "myrepo"
    for name in ("today-2026-06-26.done.md", "today-2026-06-27.md", cur,
                 "recent.md", "archive.md", "core-memories.md", "identity.md"):
        assert (edir / name).exists(), name

    # Candidate notes created, counted, and committed.
    assert report["candidates_created"] == 2
    assert rep["candidates_created"] == 2
    assert _log_count(env.vault) == commits_before + 1


def test_sources_are_untouched(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    before = _snapshot(repo / ".remember")

    migrate_mod.migrate(env.vault, env.home, roots=[root])

    after = _snapshot(repo / ".remember")
    assert before == after  # bytes AND st_mtime_ns unchanged for every source file


def test_malformed_recent_migrated_verbatim(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    migrate_mod.migrate(env.vault, env.home, roots=[root])

    src = (repo / ".remember" / "recent.md").read_bytes()
    landed = (env.vault / "groups" / "myrepo" / "episodic" / "myrepo" / "recent.md").read_bytes()
    assert landed == src
    # The doubled header + stray fence + Identity Candidates block survived.
    text = landed.decode("utf-8")
    assert text.count("# Recent") == 2
    assert "```" in text
    assert "## Identity Candidates" in text


def test_core_memories_become_candidate_notes(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    migrate_mod.migrate(env.vault, env.home, roots=[root])

    decision_dir = env.vault / "groups" / "myrepo" / "decision"
    notes = sorted(decision_dir.glob("*.md"))
    assert len(notes) == 2
    titles = set()
    for note in notes:
        meta, _body = vault_mod.read_note(note)
        assert "candidate" in meta["tags"]
        assert "from-core-memories" in meta["tags"]
        assert meta["type"] == "decision"
        assert meta["repos"] == ["github.com/acme/myrepo"]
        titles.add(vault_mod.slugify(note.stem))
    assert titles == {"chose-sqlite-over-postgres", "renamed-billing-to-ledger"}


def test_migrate_writes_candidate_notes_before_core_memories_move(env):
    """core-memories.md must land in the vault AFTER its candidate notes. If it
    moved first and an interrupt hit before the notes were written, a rerun's
    byte-identical idempotency check would skip re-staging it and never re-extract
    the candidates -- silent loss. This pins the notes-before-move ordering."""
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)

    events: list[tuple[str, str]] = []
    real_move = migrate_mod.shutil.move
    real_add = vault_mod.add_note

    def _spy_move(src, dst):
        events.append(("move", str(dst)))
        return real_move(src, dst)

    def _spy_add(*a, **k):
        events.append(("note", str(k.get("title"))))
        return real_add(*a, **k)

    env.monkeypatch.setattr(migrate_mod.shutil, "move", _spy_move)
    env.monkeypatch.setattr(vault_mod, "add_note", _spy_add)

    migrate_mod.migrate(env.vault, env.home, roots=[root])

    move_idx = next(i for i, (kind, name) in enumerate(events)
                    if kind == "move" and name.endswith("core-memories.md"))
    note_idxs = [i for i, (kind, _n) in enumerate(events) if kind == "note"]
    assert note_idxs, "expected candidate note writes from core-memories.md"
    # Every candidate note is written BEFORE core-memories.md lands in the vault.
    assert max(note_idxs) < move_idx
    # ...and core-memories.md still landed.
    assert (env.vault / "groups" / "myrepo" / "episodic" / "myrepo" / "core-memories.md").exists()


def test_now_folded_into_current_today_as_addition(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    report = migrate_mod.migrate(env.vault, env.home, roots=[root])

    cur = f"today-{_current_day()}.md"
    landed = (env.vault / "groups" / "myrepo" / "episodic" / "myrepo" / cur).read_text(encoding="utf-8")
    # now.md content folded in under the marker; the today file's own entries kept.
    assert "## migrated-now" in landed
    assert "Live buffer entry." in landed
    assert "Did a thing." in landed        # original today entry preserved
    assert "Morning thing." in landed

    rep = report["repos"][str(repo)]
    # Tracked as an ADDITION: now.md is its own report line, and the today file's
    # per-file count is NOT inflated by the fold-in.
    assert rep["files"]["now.md"]["folded_into"] == cur
    assert rep["files"]["now.md"]["source_entries"] == 1
    assert rep["files"]["now.md"]["staged_entries"] == 1
    assert rep["files"][cur]["source_entries"] == 2
    assert rep["files"][cur]["staged_entries"] == 2


def test_now_folded_into_fresh_today_when_absent(env):
    """A repo whose only content is a live now.md still migrates: now folds into a
    freshly-created current-day today file."""
    root = env.tmp / "Github"
    repo = _make_repo(root / "onlynow", "git@github.com:acme/onlynow.git")
    (repo / ".remember").mkdir(parents=True)
    (repo / ".remember" / "now.md").write_text("\n## 09:00 | main\nJust the buffer.\n", encoding="utf-8")
    _init(env, root)
    _neutral_cwd(env)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])
    assert report["repos_migrated"] == 1
    cur = f"today-{_current_day()}.md"
    landed = (env.vault / "groups" / "onlynow" / "episodic" / "onlynow" / cur).read_text(encoding="utf-8")
    assert landed.startswith("## migrated-now")
    assert "Just the buffer." in landed
    assert report["repos"][str(repo)]["files"]["now.md"]["staged_entries"] == 1


# --------------------------------------------------------------------------- #
# collisions
# --------------------------------------------------------------------------- #

def test_collision_gets_migrated_suffix_never_overwrites(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)

    # Pre-existing vault content with the same name as a source file.
    edir = env.vault / "groups" / "myrepo" / "episodic" / "myrepo"
    edir.mkdir(parents=True, exist_ok=True)
    sentinel = "# Recent\n\n## 2026-01-01\nPre-existing vault content.\n"
    (edir / "recent.md").write_text(sentinel, encoding="utf-8")

    migrate_mod.migrate(env.vault, env.home, roots=[root])

    # Original untouched; migrated copy got the .migrated-2 suffix.
    assert (edir / "recent.md").read_text(encoding="utf-8") == sentinel
    assert (edir / "recent.migrated-2.md").exists()
    assert (edir / "recent.migrated-2.md").read_bytes() == (repo / ".remember" / "recent.md").read_bytes()


# --------------------------------------------------------------------------- #
# verification failure (tamper) -> nothing lands, CLI exits 2
# --------------------------------------------------------------------------- #

def test_tampered_verify_fails_repo_nothing_lands(env):
    root, repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    commits_before = _log_count(env.vault)

    # The staged-count function is made to lie: every verification mismatches.
    env.monkeypatch.setattr(migrate_mod, "_staged_entry_count", lambda *a, **k: 9999)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])
    rep = report["repos"][str(repo)]
    assert rep["status"] == "failed"
    assert report["repos_migrated"] == 0
    assert report["repos_failed"] == 1

    # NOTHING from this repo landed, and no vault commit was made.
    assert not (env.vault / "groups" / "myrepo" / "episodic").exists()
    assert _log_count(env.vault) == commits_before


def test_cli_migrate_exits_2_on_verification_failure(env):
    root, _repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    commits_before = _log_count(env.vault)
    env.monkeypatch.setattr(migrate_mod, "_staged_entry_count", lambda *a, **k: 9999)

    r = CliRunner().invoke(main, ["memory", "migrate", "--yes"])
    assert r.exit_code == 2
    out = env.buf.getvalue()
    assert "MISMATCH" in out or "failed" in out.lower()
    assert _log_count(env.vault) == commits_before  # no commit


def test_multi_repo_one_fails_other_still_migrates(env):
    root = env.tmp / "Github"
    repo_a = _make_repo(root / "alpha", "git@github.com:acme/alpha.git")
    _write_remember(repo_a / ".remember")
    repo_b = _make_repo(root / "bravo", "git@github.com:acme/bravo.git")
    _write_remember(repo_b / ".remember", extra_marker="SENTINEL_B")
    _init(env, root)
    _neutral_cwd(env)

    real = migrate_mod._staged_entry_count

    def _fake(path, *, region=None):
        if "SENTINEL_B" in migrate_mod._read_text(path):
            return 4242  # only bravo's recent.md carries the sentinel
        return real(path, region=region)

    env.monkeypatch.setattr(migrate_mod, "_staged_entry_count", _fake)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])
    assert report["repos_migrated"] == 1
    assert report["repos_failed"] == 1
    assert report["repos"][str(repo_a)]["status"] == "migrated"
    assert report["repos"][str(repo_b)]["status"] == "failed"

    # alpha landed; bravo did not.
    assert (env.vault / "groups" / "alpha" / "episodic" / "alpha" / "archive.md").exists()
    assert not (env.vault / "groups" / "bravo" / "episodic").exists()


# --------------------------------------------------------------------------- #
# plugin retirement
# --------------------------------------------------------------------------- #

def test_retire_flips_only_the_plugin_key(env):
    settings = env.home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "enabledPlugins": {"other@x": True},
        "someKey": 42,
        "env": {"A": "B"},
    }), encoding="utf-8")

    res = migrate_mod.retire_remember_plugin(env.home)
    assert res["plugin"] == "remember@claude-plugins-official"
    assert res["enabled"] is False
    assert res["changed"] is True

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["enabledPlugins"]["remember@claude-plugins-official"] is False
    # Nothing else touched.
    assert data["enabledPlugins"]["other@x"] is True
    assert data["someKey"] == 42
    assert data["env"] == {"A": "B"}


def test_retire_is_idempotent(env):
    migrate_mod.retire_remember_plugin(env.home)
    res2 = migrate_mod.retire_remember_plugin(env.home)
    assert res2["changed"] is False
    assert res2["previous"] is False


def test_retire_uses_explicit_false_not_pop(env):
    """Matches features.toggle_claude_plugin: the key is present and False, never
    simply absent (Claude Code treats absent as enabled-by-default)."""
    migrate_mod.retire_remember_plugin(env.home)
    data = json.loads((env.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "remember@claude-plugins-official" in data["enabledPlugins"]
    assert data["enabledPlugins"]["remember@claude-plugins-official"] is False


# --------------------------------------------------------------------------- #
# CLI retirement behavior (non-TTY, --keep-plugin)
# --------------------------------------------------------------------------- #

def test_cli_non_tty_never_retires_prints_notice(env):
    root, _repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)

    r = CliRunner().invoke(main, ["memory", "migrate", "--yes"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "Not retiring the remember plugin" in out
    # The remember plugin was NOT retired: no disable entry in settings.json.
    # (settings.json itself exists because `memory init` installs the hooks.)
    assert not _remember_plugin_retired(env)


def test_cli_keep_plugin_never_offers(env):
    root, _repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)

    r = CliRunner().invoke(main, ["memory", "migrate", "--yes", "--keep-plugin"])
    assert r.exit_code == 0
    out = env.buf.getvalue().lower()
    assert "retir" not in out  # neither the offer nor the non-TTY notice
    assert not _remember_plugin_retired(env)


def test_cli_migrate_no_remember_dirs_is_clean_noop(env):
    empty = env.tmp / "emptyroot"
    empty.mkdir()
    _init(env, empty)
    _neutral_cwd(env)
    env.buf.truncate(0)
    env.buf.seek(0)

    r = CliRunner().invoke(main, ["memory", "migrate", "--yes"])
    assert r.exit_code == 0
    assert "No .remember directories found" in env.buf.getvalue()


def test_cli_migrate_requires_init(env):
    _neutral_cwd(env)
    r = CliRunner().invoke(main, ["memory", "migrate", "--yes"])
    assert r.exit_code == 2
    assert "not initialized" in env.buf.getvalue()


def test_cli_migrate_output_has_no_em_dash(env):
    root, _repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    env.buf.truncate(0)
    env.buf.seek(0)
    CliRunner().invoke(main, ["memory", "migrate", "--yes"])
    assert "—" not in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# status migration line
# --------------------------------------------------------------------------- #

def test_status_shows_migration_line_when_remember_exists(env):
    root, _repo = _fixture_repo(env)
    _init(env, root)
    _neutral_cwd(env)
    env.buf.truncate(0)
    env.buf.seek(0)

    r = CliRunner().invoke(main, ["memory", "status"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "migration" in out
    assert ".remember" in out


def test_status_no_migration_line_when_none(env):
    empty = env.tmp / "emptyroot"
    empty.mkdir()
    _init(env, empty)
    _neutral_cwd(env)
    env.buf.truncate(0)
    env.buf.seek(0)

    r = CliRunner().invoke(main, ["memory", "status"])
    assert r.exit_code == 0
    assert "migration:" not in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# Symlinked sources are refused (CSO hardening)
# --------------------------------------------------------------------------- #

@requires_symlinks
def test_symlinked_source_file_is_skipped_never_copied(env):
    """A symlink planted in .remember (e.g. today-x.md -> a sensitive file)
    migrates NOTHING for that link: the target's content must never be copied
    into the vault. Regular sibling files still migrate, and the skip is loud
    in the report."""
    root, repo = _fixture_repo(env)
    secret = env.tmp / "sensitive.txt"
    secret.write_text("## 01:00 | x\nTOP-SECRET-CONTENT\n", encoding="utf-8")
    link = repo / ".remember" / "today-2026-06-01.md"
    link.symlink_to(secret)
    _init(env, root)
    _neutral_cwd(env)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])
    rep = report["repos"][str(repo)]

    assert rep["status"] == "migrated"
    assert "today-2026-06-01.md" in rep.get("skipped_symlinks", [])
    assert "today-2026-06-01.md" not in rep["files"]
    # The secret content is nowhere in the vault.
    vault_blob = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in env.vault.rglob("*.md") if p.is_file()
    )
    assert "TOP-SECRET-CONTENT" not in vault_blob
    # The symlink itself is untouched on the source side.
    assert link.is_symlink()


@requires_symlinks
def test_symlinked_now_md_is_skipped(env):
    root, repo = _fixture_repo(env)
    secret = env.tmp / "sensitive2.txt"
    secret.write_text("## 02:00 | x\nNOW-SECRET\n", encoding="utf-8")
    now = repo / ".remember" / "now.md"
    now.unlink()
    now.symlink_to(secret)
    _init(env, root)
    _neutral_cwd(env)

    report = migrate_mod.migrate(env.vault, env.home, roots=[root])
    rep = report["repos"][str(repo)]
    assert "now.md" in rep.get("skipped_symlinks", [])
    vault_blob = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in env.vault.rglob("*.md") if p.is_file()
    )
    assert "NOW-SECRET" not in vault_blob


# --------------------------------------------------------------------------- #
# F6: migrate idempotency (a second run imports nothing, makes no commit)
# --------------------------------------------------------------------------- #

def test_migrate_twice_is_idempotent(env):
    _neutral_cwd(env)
    root, repo = _fixture_repo(env)
    _init(env, root)
    vault = env.vault

    r1 = migrate_mod.migrate(vault, env.home, roots=[root])
    assert r1["repos_migrated"] == 1
    commits_after_first = _log_count(vault)

    r2 = migrate_mod.migrate(vault, env.home, roots=[root])
    assert r2["repos_migrated"] == 0
    assert r2["total_files"] == 0
    assert r2["candidates_created"] == 0
    # No new vault commit on the idempotent re-run.
    assert _log_count(vault) == commits_after_first

    rep = r2["repos"][str(repo)]
    assert rep["status"] == "skipped"
    assert rep["reason"] == "already migrated"
    assert rep["already_imported"]  # names of the files skipped


def test_migrate_new_file_after_first_only_that_lands(env):
    _neutral_cwd(env)
    root, repo = _fixture_repo(env)
    _init(env, root)
    vault = env.vault
    migrate_mod.migrate(vault, env.home, roots=[root])

    # Add a NEW past-day file to the source, then migrate again.
    (repo / ".remember" / "today-2026-06-20.md").write_text(
        "## 07:00 | master\nBrand new past day.\n", encoding="utf-8")
    r2 = migrate_mod.migrate(vault, env.home, roots=[root])

    assert r2["repos_migrated"] == 1
    rep = r2["repos"][str(repo)]
    assert rep["status"] == "migrated"
    # Only the new file is imported this run; the rest are already-imported.
    assert "today-2026-06-20.md" in rep["files"]
    assert "recent.md" not in rep["files"]
    assert "recent.md" in rep["already_imported"]

    _identity, group, repo_short = migrate_mod._resolve_target(vault, repo)
    landed = vault / "groups" / group / "episodic" / repo_short / "today-2026-06-20.md"
    assert landed.exists()


def test_migrate_twice_creates_no_duplicate_candidate_notes(env):
    _neutral_cwd(env)
    root, repo = _fixture_repo(env)
    _init(env, root)
    vault = env.vault
    migrate_mod.migrate(vault, env.home, roots=[root])

    _identity, group, _repo_short = migrate_mod._resolve_target(vault, repo)
    decision_dir = vault / "groups" / group / "decision"
    notes_after_first = sorted(p.name for p in decision_dir.glob("*.md"))

    migrate_mod.migrate(vault, env.home, roots=[root])
    notes_after_second = sorted(p.name for p in decision_dir.glob("*.md"))
    assert notes_after_first == notes_after_second  # no duplicate candidates


# --------------------------------------------------------------------------- #
# F1: retirement aborts loudly on a corrupt settings.json
# --------------------------------------------------------------------------- #

def test_retire_remember_plugin_raises_on_corrupt_settings(env):
    from jacked.memory.settings_io import SettingsUnreadableError

    path = env.home / ".claude" / "settings.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SettingsUnreadableError):
        migrate_mod.retire_remember_plugin(env.home)
    assert path.read_bytes() == before  # untouched
