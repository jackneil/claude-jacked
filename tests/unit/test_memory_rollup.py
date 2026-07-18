"""Tests for M5: deterministic episodic rollup + the mark-groomed / rollup CLI
+ the SessionEnd rollup tail-call + the memory-librarian skill.

Isolation mirrors test_memory_vault.py / test_memory_capture_hook.py: JACKED_HOME
and JACKED_VAULT_DIR under tmp_path, a git identity via env so vault commits work
on a bare CI runner, and cli.console swapped for a StringIO-backed Console.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked.cli import main
from jacked.memory import capture
from jacked.memory import rollup as rollup_mod
from jacked.memory import vault as vault_mod

EM_DASH = "—"


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
    # Never look like a subagent session (capture guards on these).
    monkeypatch.delenv("CLAUDE_CODE_PARENT_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AGENT_TYPE", raising=False)

    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))
    return SimpleNamespace(home=home, vault=vault, tmp=tmp_path, buf=buf, monkeypatch=monkeypatch)


def _init_enabled_vault(env):
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)


def _commit_count(vault):
    r = subprocess.run(["git", "-C", str(vault), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0)


def _utc_today():
    return datetime.now(timezone.utc).date()


def _days_ago(n):
    return (_utc_today() - timedelta(days=n)).isoformat()


def _edir(env, group="myrepo", repo="myrepo"):
    return env.vault / "groups" / group / "episodic" / repo


def _write_today(env, date_str, blocks, group="myrepo", repo="myrepo"):
    """Write a today-<date>.md episodic file from ``[(hhmm, branch, sentence)]``."""
    edir = _edir(env, group, repo)
    edir.mkdir(parents=True, exist_ok=True)
    text = "".join(f"## {hhmm} | {branch}\n{sentence}\n\n" for hhmm, branch, sentence in blocks)
    path = edir / f"today-{date_str}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _write_file(env, name, text, group="myrepo", repo="myrepo"):
    edir = _edir(env, group, repo)
    edir.mkdir(parents=True, exist_ok=True)
    path = edir / name
    path.write_text(text, encoding="utf-8")
    return path


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


# --------------------------------------------------------------------------- #
# Small parsing helpers (unit-testable in isolation)
# --------------------------------------------------------------------------- #

def test_monday_of_is_a_monday_on_or_before():
    for offset in range(0, 21):
        d = _utc_today() - timedelta(days=offset)
        monday = rollup_mod._monday_of(d)
        assert monday.weekday() == 0
        assert monday <= d
        assert (d - monday).days < 7


def test_day_sections_parses_valid_headers_only():
    text = (
        "# Recent\n\n"
        "## 2026-07-10\n### 09:00 | master\nreal day\n\n"
        "## Identity Candidates\n- IDENTITY CANDIDATE: noise\n"
    )
    sections = rollup_mod._day_sections(text)
    assert len(sections) == 1
    d, header, body = sections[0]
    assert d.isoformat() == "2026-07-10"
    assert header == "## 2026-07-10"
    assert "real day" in body


def test_demote_h2_only_touches_level_two():
    text = "## 09:15 | master\ndid work\n### already deep\n# top"
    out = rollup_mod._demote_h2(text)
    assert "### 09:15 | master" in out
    assert "### already deep" in out       # unchanged (already H3)
    assert "\n# top" in out                # H1 left verbatim, never demoted here
    assert "\n## 09:15" not in out         # the H2 entry was demoted


# --------------------------------------------------------------------------- #
# Step 1: past today files -> recent.md; current file untouched
# --------------------------------------------------------------------------- #

def test_past_files_roll_to_recent_current_untouched(env):
    _init_enabled_vault(env)
    d2, d1 = _days_ago(2), _days_ago(1)
    p2 = _write_today(env, d2, [("09:15", "master", "Did the parser.")])
    p1 = _write_today(env, d1, [("10:20", "master", "Wrote the tests.")])
    today_path = _write_today(env, _utc_today().isoformat(), [("11:00", "master", "Live work.")])
    today_before = _read(today_path)

    summary = rollup_mod.rollup(env.vault, env.home)

    assert summary["days_rolled"] == 2
    assert summary["sections_archived"] == 0
    assert summary["skipped_unparseable"] == 0

    recent = _read(_edir(env) / "recent.md")
    assert recent.startswith("# Recent")
    # Both day sections present, entries demoted to ### (verbatim body kept).
    assert f"## {d2}" in recent and f"## {d1}" in recent
    assert "### 09:15 | master" in recent and "Did the parser." in recent
    assert "### 10:20 | master" in recent and "Wrote the tests." in recent
    # No un-demoted H2 time entry survived.
    assert "## 09:15 | master" not in [ln for ln in recent.splitlines()]

    # Past files renamed to .done.md (content preserved), originals gone.
    assert not p2.exists() and not p1.exists()
    assert (_edir(env) / f"today-{d2}.done.md").exists()
    assert (_edir(env) / f"today-{d1}.done.md").exists()

    # Current-day file untouched.
    assert today_path.exists()
    assert _read(today_path) == today_before
    assert not (_edir(env) / f"today-{_utc_today().isoformat()}.done.md").exists()


# --------------------------------------------------------------------------- #
# Step 2: recent -> archive boundary (7 stays, 8 moves)
# --------------------------------------------------------------------------- #

def test_archive_boundary_seven_stays_eight_moves(env):
    _init_enabled_vault(env)
    stay, move = _days_ago(7), _days_ago(8)
    recent = (
        "# Recent\n\n"
        f"## {stay}\n### 09:00 | master\nstays here\n\n"
        f"## {move}\n### 10:00 | master\nmoves to archive\n"
    )
    _write_file(env, "recent.md", recent)

    summary = rollup_mod.rollup(env.vault, env.home)

    assert summary["sections_archived"] == 1
    assert summary["days_rolled"] == 0

    recent_after = _read(_edir(env) / "recent.md")
    assert f"## {stay}" in recent_after and "stays here" in recent_after
    assert f"## {move}" not in recent_after and "moves to archive" not in recent_after

    archive = _read(_edir(env) / "archive.md")
    monday = rollup_mod._monday_of(rollup_mod._parse_date(move))
    assert archive.startswith("# Archive")
    assert f"## Week of {monday.isoformat()}" in archive
    assert "moves to archive" in archive
    # The moved day nests one level deeper under the week header.
    assert f"### {move}" in archive and "#### 10:00 | master" in archive


def test_archive_appends_to_existing_week_section(env):
    _init_enabled_vault(env)
    move = _days_ago(8)
    monday = rollup_mod._monday_of(rollup_mod._parse_date(move))
    existing_archive = (
        "# Archive\n\n"
        f"## Week of {monday.isoformat()}\n"
        "### 2026-01-01\n#### 08:00 | master\nold archived stuff\n"
    )
    _write_file(env, "archive.md", existing_archive)
    _write_file(env, "recent.md", f"# Recent\n\n## {move}\n### 09:00 | master\nfresh archive day\n")

    rollup_mod.rollup(env.vault, env.home)

    archive = _read(_edir(env) / "archive.md")
    # Exactly ONE week header for that monday: appended, not duplicated.
    assert archive.count(f"## Week of {monday.isoformat()}") == 1
    assert "old archived stuff" in archive
    assert "fresh archive day" in archive


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def test_idempotent_second_run_is_a_noop(env):
    _init_enabled_vault(env)
    _write_today(env, _days_ago(2), [("09:00", "master", "recent day")])       # stays
    _write_today(env, _days_ago(9), [("10:00", "master", "old day")])          # archives

    first = rollup_mod.rollup(env.vault, env.home)
    assert first["changed"] is True
    recent_after_1 = _read(_edir(env) / "recent.md")
    archive_after_1 = _read(_edir(env) / "archive.md")
    commits_after_1 = _commit_count(env.vault)

    second = rollup_mod.rollup(env.vault, env.home)
    assert second == {
        "days_rolled": 0, "sections_archived": 0, "skipped_unparseable": 0,
        "dirs": 1, "changed": False,
    }
    # Byte-identical vault files, no new vault commit.
    assert _read(_edir(env) / "recent.md") == recent_after_1
    assert _read(_edir(env) / "archive.md") == archive_after_1
    assert _commit_count(env.vault) == commits_after_1


# --------------------------------------------------------------------------- #
# Malformed / tolerated content
# --------------------------------------------------------------------------- #

def test_malformed_today_file_left_and_counted(env):
    _init_enabled_vault(env)
    edir = _edir(env)
    edir.mkdir(parents=True, exist_ok=True)
    bad = edir / f"today-{_days_ago(3)}.md"
    bad.write_text("just some prose with no headers at all\n", encoding="utf-8")

    summary = rollup_mod.rollup(env.vault, env.home)

    assert summary["skipped_unparseable"] >= 1
    assert summary["days_rolled"] == 0
    # Left exactly where it was: not renamed, not dropped.
    assert bad.exists()
    assert not (edir / f"today-{_days_ago(3)}.done.md").exists()
    assert not (edir / "recent.md").exists()


def test_unparseable_recent_section_kept_and_counted(env):
    _init_enabled_vault(env)
    move = _days_ago(9)
    recent = (
        "# Recent\n\n"
        "## Identity Candidates\n- IDENTITY CANDIDATE: keep me\n\n"
        f"## {move}\n### 10:00 | master\narchive me\n"
    )
    _write_file(env, "recent.md", recent)

    summary = rollup_mod.rollup(env.vault, env.home)

    assert summary["skipped_unparseable"] >= 1
    recent_after = _read(_edir(env) / "recent.md")
    # The non-day section is untouched and still present.
    assert "## Identity Candidates" in recent_after
    assert "IDENTITY CANDIDATE: keep me" in recent_after
    # The real day section still archived.
    assert "archive me" in _read(_edir(env) / "archive.md")


def test_duplicated_recent_header_tolerated(env):
    _init_enabled_vault(env)
    move = _days_ago(10)
    recent = (
        "# Recent\n"
        "# Recent\n\n"
        f"## {move}\n### 09:00 | master\ndoubled-header day\n"
    )
    _write_file(env, "recent.md", recent)

    # Must not crash; the day still archives.
    summary = rollup_mod.rollup(env.vault, env.home)
    assert summary["sections_archived"] == 1
    recent_after = _read(_edir(env) / "recent.md")
    assert recent_after.count("# Recent") == 2  # both headers preserved
    assert "doubled-header day" in _read(_edir(env) / "archive.md")


# --------------------------------------------------------------------------- #
# State + commit bookkeeping
# --------------------------------------------------------------------------- #

def test_last_rollup_set_and_single_commit(env):
    _init_enabled_vault(env)
    _write_today(env, _days_ago(2), [("09:00", "master", "one day")])

    before = _commit_count(env.vault)
    rollup_mod.rollup(env.vault, env.home)

    assert _commit_count(env.vault) == before + 1  # exactly one rollup commit
    st = vault_mod.load_state(env.home)
    assert st["last_rollup"]
    # ISO-ish UTC stamp.
    assert st["last_rollup"].endswith("Z")


def test_rollup_uninitialized_vault_is_clean_noop(env):
    # No init: rollup returns zeros, no crash, no commit attempt.
    summary = rollup_mod.rollup(env.vault, env.home)
    assert summary["changed"] is False
    assert summary["days_rolled"] == 0
    assert not (env.vault / "groups").exists()


# --------------------------------------------------------------------------- #
# mark-groomed
# --------------------------------------------------------------------------- #

def test_mark_groomed_resets_drift_and_stamps(env):
    vault_mod.bump_drift(env.home, 12)
    assert vault_mod.load_state(env.home)["drift_added"] == 12

    r = CliRunner().invoke(main, ["memory", "mark-groomed"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()

    st = vault_mod.load_state(env.home)
    assert st["drift_added"] == 0
    assert st["last_groomed"] and st["last_groomed"].endswith("Z")
    out = env.buf.getvalue()
    assert "groomed" in out.lower()
    assert EM_DASH not in out


def test_mark_groomed_function_directly(env):
    vault_mod.bump_drift(env.home, 5)
    st = vault_mod.mark_groomed(env.home)
    assert st["drift_added"] == 0
    assert st["last_groomed"]


# --------------------------------------------------------------------------- #
# CLI rollup
# --------------------------------------------------------------------------- #

def test_cli_rollup_uninitialized_exits_zero(env):
    r = CliRunner().invoke(main, ["memory", "rollup"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "not initialized" in out.lower()
    assert EM_DASH not in out


def test_cli_rollup_prints_summary(env):
    _init_enabled_vault(env)
    _write_today(env, _days_ago(2), [("09:00", "master", "cli day")])

    r = CliRunner().invoke(main, ["memory", "rollup"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    out = env.buf.getvalue()
    assert "Rollup complete" in out
    assert "1 day(s) rolled" in out
    assert EM_DASH not in out


# --------------------------------------------------------------------------- #
# SessionEnd rollup tail-call
# --------------------------------------------------------------------------- #

def _user_line(text, ts="2026-07-18T10:00:00Z"):
    return json.dumps({
        "type": "user", "uuid": str(uuid4()), "timestamp": ts,
        "message": {"role": "user", "content": text},
    })


def _asst_line(text, ts="2026-07-18T10:00:01Z"):
    return json.dumps({
        "type": "assistant", "uuid": str(uuid4()), "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _transcript(env, n_human=3):
    lines = []
    for i in range(n_human):
        lines.append(_user_line(f"Implement feature {i} with tests and docs, please."))
        lines.append(_asst_line(f"Done with feature {i}; tests green."))
    p = env.tmp / "t.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _cwd(env, name="caprepo"):
    d = env.tmp / name
    d.mkdir(exist_ok=True)
    return d


def _payload(transcript, cwd, event="SessionEnd"):
    return {
        "hook_event_name": event, "session_id": "sess-abcdef123456",
        "transcript_path": str(transcript), "cwd": str(cwd),
    }


def _mock_triage(env, response):
    env.monkeypatch.setattr(capture, "call_claude", lambda prompt, model: response)


def test_session_end_invokes_rollup(env):
    _init_enabled_vault(env)
    _mock_triage(env, json.dumps({"episodic": "Did work.", "semantic": None}))
    calls = []
    env.monkeypatch.setattr(
        rollup_mod, "rollup",
        lambda v, h, only_repo_short=None: calls.append((v, h, only_repo_short)),
    )

    capture.session_end(_payload(_transcript(env), _cwd(env)))

    assert len(calls) == 1
    assert str(calls[0][0]) == str(env.vault)
    # The tail-call is scoped to the session's repo_short (never the whole vault).
    assert calls[0][2] == "caprepo"


def test_session_end_tail_call_rolls_a_past_file(env):
    _init_enabled_vault(env)
    _mock_triage(env, json.dumps({"episodic": "Did work.", "semantic": None}))
    # A past-day episodic file in the capture's own group/repo (cwd 'caprepo').
    past = _write_today(env, _days_ago(2), [("08:00", "caprepo", "yesterday work")],
                        group="caprepo", repo="caprepo")

    capture.session_end(_payload(_transcript(env), _cwd(env)))

    # The tail-call rolled the past file into recent and marked it done.
    assert not past.exists()
    assert (_edir(env, "caprepo", "caprepo") / f"today-{_days_ago(2)}.done.md").exists()
    recent = _read(_edir(env, "caprepo", "caprepo") / "recent.md")
    assert recent and "yesterday work" in recent


def test_rollup_exception_does_not_break_capture(env):
    _init_enabled_vault(env)
    _mock_triage(env, json.dumps({"episodic": "Captured fine.", "semantic": None}))

    def _boom(vault, home, only_repo_short=None):
        raise RuntimeError("rollup blew up")

    env.monkeypatch.setattr(rollup_mod, "rollup", _boom)

    # Must not raise: the capture's episodic entry still lands (exit-0 semantics).
    capture.session_end(_payload(_transcript(env), _cwd(env)))

    edir = _edir(env, "caprepo", "caprepo")
    text = "".join(f.read_text(encoding="utf-8") for f in edir.glob("today-*.md"))
    assert "Captured fine." in text


def test_pre_compact_does_not_invoke_rollup(env):
    _init_enabled_vault(env)
    _mock_triage(env, json.dumps({"episodic": "Mid-session.", "semantic": None}))
    calls = []
    env.monkeypatch.setattr(
        rollup_mod, "rollup",
        lambda v, h, only_repo_short=None: calls.append((v, h, only_repo_short)),
    )

    capture.pre_compact(_payload(_transcript(env), _cwd(env), event="PreCompact"))

    assert calls == []


# --------------------------------------------------------------------------- #
# Librarian skill file
# --------------------------------------------------------------------------- #

def test_librarian_skill_exists_and_valid():
    skill = Path(__file__).resolve().parents[2] / "jacked" / "data" / "skills" / "memory-librarian" / "SKILL.md"
    assert skill.exists(), f"missing {skill}"
    text = skill.read_text(encoding="utf-8")
    # Frontmatter with name + description.
    assert text.startswith("---")
    fm = text.split("---", 2)[1]
    assert "name: memory-librarian" in fm
    assert "description:" in fm
    # Mentions the wrap-up command and never uses an em-dash.
    assert "mark-groomed" in text
    assert EM_DASH not in text


# --------------------------------------------------------------------------- #
# F4: atomic + ordered writes (archive.md BEFORE recent.md)
# --------------------------------------------------------------------------- #

def _local_today():
    return datetime.now().strftime("%Y-%m-%d")


def test_rollup_writes_archive_before_recent(env):
    _init_enabled_vault(env)
    edir = _edir(env)
    edir.mkdir(parents=True, exist_ok=True)
    # A recent.md day older than the archive boundary forces BOTH an archive.md
    # write and a recent.md rewrite in the same dir.
    (edir / "recent.md").write_text(
        f"# Recent\n\n## {_days_ago(9)}\n### 09:00 | master\nold day\n",
        encoding="utf-8",
    )

    order = []
    real = rollup_mod._write_if_differs

    def _spy(path, text):
        order.append(path.name)
        return real(path, text)

    env.monkeypatch.setattr(rollup_mod, "_write_if_differs", _spy)
    summary = rollup_mod.rollup(env.vault, env.home)

    assert summary["sections_archived"] == 1
    assert "archive.md" in order and "recent.md" in order
    # Add-before-remove: archive.md is written before recent.md.
    assert order.index("archive.md") < order.index("recent.md")


def test_write_if_differs_is_atomic_and_leaves_no_tmp(env):
    edir = _edir(env)
    edir.mkdir(parents=True, exist_ok=True)
    target = edir / "recent.md"
    assert rollup_mod._write_if_differs(target, "# Recent\n\nhi\n") is True
    assert target.read_text(encoding="utf-8") == "# Recent\n\nhi\n"
    # No writer-unique temp left behind.
    assert not list(edir.glob(".recent.md.*.tmp"))
    # Idempotent second call is a no-op.
    assert rollup_mod._write_if_differs(target, "# Recent\n\nhi\n") is False


# --------------------------------------------------------------------------- #
# F5: local clock (current-day file never rolled) + only_repo_short scoping
# --------------------------------------------------------------------------- #

def test_local_today_file_is_never_rolled(env):
    _init_enabled_vault(env)
    edir = _edir(env)
    p = _write_today(env, _local_today(), [("11:00", "master", "live work today")])
    summary = rollup_mod.rollup(env.vault, env.home)
    assert p.exists()  # still the live today file
    assert summary["days_rolled"] == 0
    assert not (edir / f"today-{_local_today()}.done.md").exists()


def test_rollup_only_repo_short_scopes_to_one_dir(env):
    _init_enabled_vault(env)
    pa = _write_today(env, _days_ago(2), [("08:00", "m", "repo A past")], group="ra", repo="ra")
    pb = _write_today(env, _days_ago(2), [("08:00", "m", "repo B past")], group="rb", repo="rb")

    summary = rollup_mod.rollup(env.vault, env.home, only_repo_short="ra")

    assert summary["dirs"] == 1
    assert not pa.exists()  # ra rolled
    assert pb.exists()      # rb untouched
    assert _read(_edir(env, "rb", "rb") / "recent.md") is None
