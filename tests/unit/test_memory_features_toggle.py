"""M7 tests: the memory-vault enable/disable engine, the dashboard Features
toggle wiring, the CLI install/uninstall parity, and the command/skill routing.

Every test isolates JACKED_HOME + JACKED_VAULT_DIR under tmp_path (plus a
neutral cwd so ``.remember`` discovery never walks up into the real repo), so
nothing touches the real ~/.claude, ~/jacked-vault, or any real git repo. Route
tests use the hermetic router TestClient pattern from
tests/unit/test_packs_endpoint.py; the guarded-app CSRF/Host coverage is copied
from there so a middleware regression fails here too. Engine tests run the real
``setup.enable`` / ``setup.disable`` against tmp vaults + repos.
"""
import json
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from rich.console import Console
from starlette.testclient import TestClient

import jacked.cli as cli
from jacked.api.routes import features
from jacked.api.routes.features import router
from jacked.memory import hooks_config
from jacked.memory import setup as memory_setup
from jacked.memory import vault as vault_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "jacked" / "data"
FOREIGN_POST_MERGE = "#!/bin/sh\n# some other tool's hook\necho foreign\n"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def menv(tmp_path, monkeypatch):
    """Isolated home + vault + neutral cwd + git identity."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "vault"
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setenv("JACKED_VAULT_DIR", str(vault))
    # Neutral cwd: no .git upward, so migrate discovery only sees planted repos.
    neutral = tmp_path / "cwd"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)
    # Quiet the Rich console the CLI installers print through.
    monkeypatch.setattr(cli, "console", Console(file=StringIO(), width=200))
    return SimpleNamespace(home=home, vault=vault, tmp=tmp_path)


def _make_repo(path, remote=None, with_remember=False, foreign_post_merge=False):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, text=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                       check=True, capture_output=True, text=True)
    if with_remember:
        rem = path / ".remember"
        rem.mkdir()
        (rem / "recent.md").write_text("# Recent\n\n## 2026-07-18\n\nDid a thing.\n", encoding="utf-8")
    if foreign_post_merge:
        hook = path / ".git" / "hooks" / "post-merge"
        hook.write_text(FOREIGN_POST_MERGE, encoding="utf-8")
        hook.chmod(0o755)
    return path


def _github_root(menv):
    """<home>/Github (what setup._default_roots scans non-interactively)."""
    root = menv.home / "Github"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _snapshot_tree(base: Path) -> dict:
    """relpath -> bytes for every file under ``base`` (for unchanged-tree checks)."""
    if not base.exists():
        return {}
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*")) if p.is_file()
    }


def _read_settings(home: Path) -> dict:
    p = home / ".claude" / "settings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _post_merge(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "post-merge"


# --------------------------------------------------------------------------- #
# A. Engine: setup.enable
# --------------------------------------------------------------------------- #

def test_enable_fresh_initializes_installs_entries_and_git_hooks(menv):
    root = _github_root(menv)
    repo = _make_repo(root / "acme-widget", "git@github.com:acme/acme-widget.git",
                      with_remember=True)

    result = memory_setup.enable(menv.home)

    # Vault initialized + state flipped on.
    assert result["initialized_now"] is True
    assert vault_mod.is_initialized(menv.vault)
    assert vault_mod.load_state(menv.home)["enabled"] is True
    assert result["vault_path"] == str(menv.vault)
    assert result["hooks_installed"] == ["memory_capture", "memory_recall"]

    # BOTH settings entries present, verified against real settings.json content.
    settings = _read_settings(menv.home)
    hooks = settings["hooks"]
    assert hooks_config.has_capture_entry(settings)
    # Capture pair: SessionEnd + PreCompact(auto), async.
    se = [e for e in hooks["SessionEnd"] if "memory_capture" in str(e)]
    pc = [e for e in hooks["PreCompact"] if "memory_capture" in str(e)]
    assert se and pc
    assert se[0]["hooks"][0]["async"] is True
    assert pc[0]["matcher"] == "auto"
    # Recall: single SYNCHRONOUS SessionStart entry (no async key).
    ss = [e for e in hooks["SessionStart"] if "memory_recall" in str(e)]
    assert len(ss) == 1
    assert "async" not in ss[0]["hooks"][0]

    # Our post-merge git hook landed in the mapped repo.
    assert result["git_hooks"], "expected the mapped repo in the git-hook summary"
    assert any(v.get("installed") for v in result["git_hooks"].values())
    assert _post_merge(repo).exists()
    from jacked.memory.githook import HOOK_MARKER
    assert HOOK_MARKER in _post_merge(repo).read_text(encoding="utf-8")

    # Migration count: the one planted .remember dir.
    assert result["migration_available"] == 1


def test_enable_is_idempotent(menv):
    root = _github_root(menv)
    _make_repo(root / "solo", "git@github.com:acme/solo.git")

    first = memory_setup.enable(menv.home)
    settings_after_first = _read_settings(menv.home)
    second = memory_setup.enable(menv.home)
    settings_after_second = _read_settings(menv.home)

    # Second enable neither re-initializes nor duplicates entries.
    assert first["initialized_now"] is True
    assert second["initialized_now"] is False
    assert settings_after_first == settings_after_second
    # Exactly one capture entry per event, one recall entry.
    hooks = settings_after_second["hooks"]
    assert len([e for e in hooks["SessionEnd"] if "memory_capture" in str(e)]) == 1
    assert len([e for e in hooks["SessionStart"] if "memory_recall" in str(e)]) == 1
    assert vault_mod.load_state(menv.home)["enabled"] is True


def test_enable_empty_github_creates_empty_vault(menv):
    # No <home>/Github at all -> vault still created, no repos, no git hooks.
    result = memory_setup.enable(menv.home)
    assert result["initialized_now"] is True
    assert vault_mod.is_initialized(menv.vault)
    assert result["git_hooks"] == {}
    assert result["migration_available"] == 0
    assert hooks_config.has_capture_entry(_read_settings(menv.home))


# --------------------------------------------------------------------------- #
# B. Engine: setup.disable
# --------------------------------------------------------------------------- #

def test_disable_removes_ours_and_leaves_vault_and_foreign_intact(menv):
    root = _github_root(menv)
    ours = _make_repo(root / "ours", "git@github.com:acme/ours.git")
    # A mapped repo that already carries a FOREIGN post-merge: enable must refuse
    # to clobber it, and disable must never remove it.
    foreign = _make_repo(root / "foreign", "git@github.com:acme/foreign.git",
                         foreign_post_merge=True)

    memory_setup.enable(menv.home)
    assert _post_merge(ours).exists()  # ours installed

    # Plant a foreign settings.json hook entry that disable must not touch.
    settings = _read_settings(menv.home)
    settings["hooks"].setdefault("SessionEnd", []).append(
        {"matcher": "", "hooks": [{"type": "command", "command": "other-tool --run"}]}
    )
    (menv.home / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))

    vault_before = _snapshot_tree(menv.vault)

    result = memory_setup.disable(menv.home)

    # State off, our settings entries gone.
    assert result["enabled"] is False
    assert vault_mod.load_state(menv.home)["enabled"] is False
    settings_after = _read_settings(menv.home)
    assert not hooks_config.has_capture_entry(settings_after)
    assert not any("memory_recall" in str(e)
                   for arr in settings_after["hooks"].values() for e in arr)

    # Our git hook removed; foreign git hook untouched; foreign settings entry kept.
    assert not _post_merge(ours).exists()
    assert _post_merge(foreign).read_text(encoding="utf-8") == FOREIGN_POST_MERGE
    assert any("other-tool --run" in str(e)
               for arr in settings_after["hooks"].values() for e in arr)

    # Vault files completely untouched by disable.
    assert _snapshot_tree(menv.vault) == vault_before
    assert vault_before  # sanity: there was a vault to leave intact


def test_disable_without_vault_only_clears_state(menv):
    # Never enabled: disable removes nothing, sets state off, no git hooks.
    result = memory_setup.disable(menv.home)
    assert result["enabled"] is False
    assert result["git_hooks"] == {}
    assert vault_mod.load_state(menv.home)["enabled"] is False


# --------------------------------------------------------------------------- #
# C. Route: hermetic app + GET/PUT
# --------------------------------------------------------------------------- #

def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _make_guarded_app() -> FastAPI:
    from jacked.api.security import HostValidationMiddleware, build_allowed_origins
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_middleware(HostValidationMiddleware)
    app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8321)
    return app


def test_put_round_trip_flips_everything_and_returns_payload(menv, monkeypatch):
    root = _github_root(menv)
    _make_repo(root / "acme-widget", "git@github.com:acme/acme-widget.git",
               with_remember=True)
    # GET detection reads the module SETTINGS_JSON; point it at our tmp home so
    # it sees exactly what the engine writes.
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")

    client = TestClient(_make_app())

    # Enable.
    resp = client.put("/api/features/hooks/memory_vault", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["name"] == "memory_vault"
    assert body["vault_path"] == str(menv.vault)
    assert body["migration_available"] == 1
    assert "git_hooks" in body and "groups" in body

    assert vault_mod.load_state(menv.home)["enabled"] is True
    assert hooks_config.has_capture_entry(_read_settings(menv.home))

    # GET reflects installed.
    feats = client.get("/api/features").json()
    mv = next(h for h in feats["hooks"] if h["name"] == "memory_vault")
    assert mv["installed"] is True
    assert mv["display_name"] == "Memory Vault"

    vault_before = _snapshot_tree(menv.vault)

    # Disable never touches vault content.
    resp = client.put("/api/features/hooks/memory_vault", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert not hooks_config.has_capture_entry(_read_settings(menv.home))
    assert vault_mod.load_state(menv.home)["enabled"] is False
    assert _snapshot_tree(menv.vault) == vault_before  # vault untouched

    feats = client.get("/api/features").json()
    mv = next(h for h in feats["hooks"] if h["name"] == "memory_vault")
    assert mv["installed"] is False


def test_put_response_carries_engine_payload(monkeypatch):
    """The route merges the engine's result into the PUT response (boundary
    test: the engine is faked so this stays hermetic and asserts passthrough)."""
    captured = {}

    def fake_enable(home):
        captured["home"] = home
        return {
            "vault_path": "/tmp/v", "initialized_now": True, "groups": {"g": []},
            "hooks_installed": ["memory_capture", "memory_recall"],
            "git_hooks": {"github.com/acme/x": {"installed": True}},
            "migration_available": 3,
        }

    monkeypatch.setattr("jacked.memory.setup.enable", fake_enable)

    client = TestClient(_make_app())
    body = client.put("/api/features/hooks/memory_vault", json={"enabled": True}).json()
    assert body["migration_available"] == 3
    assert body["git_hooks"] == {"github.com/acme/x": {"installed": True}}
    assert body["enabled"] is True
    # Home was resolved from $JACKED_HOME and threaded into the engine.
    assert captured["home"] == vault_mod.jacked_home()


def test_memory_vault_in_valid_hooks_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(features, "SETTINGS_JSON", tmp_path / "settings.json")
    assert "memory_vault" in features.VALID_HOOKS
    client = TestClient(_make_app())
    feats = client.get("/api/features").json()
    names = [h["name"] for h in feats["hooks"]]
    assert "memory_vault" in names
    mv = next(h for h in feats["hooks"] if h["name"] == "memory_vault")
    # No em-dash in the user-facing description.
    assert "—" not in mv["description"]
    assert mv["installed"] is False  # empty settings -> not installed


def test_put_memory_vault_foreign_origin_is_403(monkeypatch):
    monkeypatch.setattr("jacked.memory.setup.enable", lambda home: {})
    client = TestClient(_make_guarded_app())
    resp = client.put(
        "/api/features/hooks/memory_vault",
        json={"enabled": True},
        headers={"origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_put_memory_vault_untrusted_host_is_421(monkeypatch):
    monkeypatch.setattr("jacked.memory.setup.enable", lambda home: {})
    client = TestClient(_make_guarded_app())
    resp = client.put(
        "/api/features/hooks/memory_vault",
        json={"enabled": True},
        headers={"host": "evil.example.com"},
    )
    assert resp.status_code == 421
    assert resp.json()["error"]["code"] == "UNTRUSTED_HOST"


# --------------------------------------------------------------------------- #
# D. CLI install / uninstall parity
# --------------------------------------------------------------------------- #

def test_install_rewires_memory_hooks_when_enabled(menv):
    vault_mod.set_enabled(menv.home, True, vault=menv.vault)
    settings_path = menv.home / ".claude" / "settings.json"
    existing: dict = {"hooks": {}}

    cli._rewire_memory_hooks_on_install(menv.home, existing, settings_path)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert hooks_config.has_capture_entry(settings)
    assert any("memory_recall" in str(e)
               for arr in settings["hooks"].values() for e in arr)


def test_install_skips_memory_hooks_when_disabled(menv):
    # No state file -> reads disabled -> no-op, no settings written.
    settings_path = menv.home / ".claude" / "settings.json"
    existing: dict = {"hooks": {}}
    cli._rewire_memory_hooks_on_install(menv.home, existing, settings_path)
    assert not hooks_config.has_capture_entry(existing)
    assert not settings_path.exists()


def test_uninstall_removes_memory_hooks_unconditionally(menv):
    settings_path = menv.home / ".claude" / "settings.json"
    # Seed capture + recall + a foreign entry.
    settings: dict = {"hooks": {}}
    hooks_config.ensure_capture_entries(settings, '"jacked" _hook memory_capture')
    hooks_config.ensure_recall_entry(settings, '"jacked" _hook memory_recall')
    settings["hooks"].setdefault("SessionEnd", []).append(
        {"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]}
    )
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    assert cli._remove_memory_hooks(settings_path) is True

    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert not hooks_config.has_capture_entry(after)
    assert not any("memory_recall" in str(e)
                   for arr in after["hooks"].values() for e in arr)
    # Foreign entry survives.
    assert any("other-tool" in str(e)
               for arr in after["hooks"].values() for e in arr)


# --------------------------------------------------------------------------- #
# E. Command / skill routing files
# --------------------------------------------------------------------------- #

EDITED_MD = [
    DATA / "commands" / "remember.md",
    DATA / "commands" / "learn.md",
    DATA / "commands" / "goal-maker.md",
    DATA / "commands" / "bhag.md",
    DATA / "skills" / "dcr" / "SKILL.md",
    DATA / "skills" / "release" / "SKILL.md",
    DATA / "skills" / "checkpoint" / "SKILL.md",
]

# Tokens that uniquely identify a memory-vault block line (never appear in the
# legacy prose of these files), so the em-dash assertion scopes to added lines.
_VAULT_TOKENS = ("jacked memory", "memory vault", "memory migrate")


def _mid(p):
    """Test id: a skill's identity is its directory, not the SKILL.md filename."""
    return p.parent.name if p.name == "SKILL.md" else p.name


@pytest.mark.parametrize("path", EDITED_MD, ids=_mid)
def test_command_file_has_guarded_memory_block(path):
    text = path.read_text(encoding="utf-8")
    assert "jacked memory status --quiet" in text
    assert "jacked memory add" in text


@pytest.mark.parametrize("path", EDITED_MD, ids=_mid)
def test_memory_block_lines_have_no_em_dash(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if any(tok in line.lower() for tok in _VAULT_TOKENS):
            assert "—" not in line, f"em-dash in memory-vault line of {path.name}: {line!r}"


def test_remember_command_exists_with_frontmatter():
    p = DATA / "commands" / "remember.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "description:" in text.split("---", 2)[1]
    # Supersession note for the retired third-party plugin.
    assert "supersede" in text.lower()


# --------------------------------------------------------------------------- #
# F. Frontend: migration toast string
# --------------------------------------------------------------------------- #

def test_settings_js_has_migration_toast_and_guard():
    js = (DATA / "web" / "js" / "components" / "settings.js").read_text(encoding="utf-8")
    assert "run jacked memory migrate to import" in js
    assert "name === 'memory_vault'" in js
    assert "migration_available" in js
    # The toast copy must be em-dash free (user-facing).
    toast_line = next(ln for ln in js.splitlines() if "run jacked memory migrate to import" in ln)
    assert "—" not in toast_line


def test_remove_memory_hooks_tolerates_corrupt_settings(menv):
    """A corrupt settings.json must not abort uninstall: removal returns False,
    prints a notice, and leaves the file byte-identical (CSO hardening)."""
    import jacked.cli as cli_mod

    settings_path = menv.home / ".claude" / "settings.json"
    settings_path.write_text("{ this is not json", encoding="utf-8")
    before = settings_path.read_bytes()

    assert cli_mod._remove_memory_hooks(settings_path) is False
    assert settings_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# F1: settings.json corruption is refused, never clobbered
# --------------------------------------------------------------------------- #

def test_setup_enable_raises_on_corrupt_settings_and_leaves_it(menv):
    from jacked.memory.settings_io import SettingsUnreadableError

    path = menv.home / ".claude" / "settings.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SettingsUnreadableError):
        memory_setup.enable(menv.home)
    assert path.read_bytes() == before  # byte-identical: never clobbered


def test_enable_creates_settings_when_missing(menv):
    path = menv.home / ".claude" / "settings.json"
    if path.exists():
        path.unlink()
    memory_setup.enable(menv.home)
    assert path.exists()
    assert hooks_config.has_capture_entry(_read_settings(menv.home))


def test_put_memory_vault_503_on_corrupt_settings(menv, monkeypatch):
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    path = menv.home / ".claude" / "settings.json"
    path.write_text("{ corrupt settings", encoding="utf-8")
    before = path.read_bytes()

    client = TestClient(_make_app())
    resp = client.put("/api/features/hooks/memory_vault", json={"enabled": True})
    assert resp.status_code == 503
    assert "unreadable" in resp.json()["error"]["message"]
    assert path.read_bytes() == before  # file untouched


# --------------------------------------------------------------------------- #
# W6: the generic settings surface adopts the same wipe-guard (refuse, degrade)
# --------------------------------------------------------------------------- #

def test_env_put_503_on_corrupt_settings_and_file_untouched(menv, monkeypatch):
    """A corrupt settings.json makes an env PUT (a read-modify-write) refuse with
    503 and never clobber the file with a fresh one."""
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    path = menv.home / ".claude" / "settings.json"
    path.write_text("{ not valid json at all", encoding="utf-8")
    before = path.read_bytes()

    client = TestClient(_make_app())
    resp = client.put(
        "/api/claude-settings/env/CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
        json={"enabled": True},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SETTINGS_UNREADABLE"
    assert path.read_bytes() == before  # never clobbered


@pytest.mark.parametrize("call", [
    lambda c: c.put("/api/claude-settings/key/alwaysThinkingEnabled", json={"value": True}),
    lambda c: c.put("/api/claude-settings/plugins/x@official", json={"enabled": False}),
    lambda c: c.put("/api/claude-settings/permissions", json={"allow": ["Bash(ls)"]}),
])
def test_all_settings_mutations_503_on_corrupt(menv, monkeypatch, call):
    """Every RMW settings mutation refuses (503) on a corrupt file rather than
    clobbering it."""
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    path = menv.home / ".claude" / "settings.json"
    path.write_text("{ broken", encoding="utf-8")
    before = path.read_bytes()

    resp = call(TestClient(_make_app()))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SETTINGS_UNREADABLE"
    assert path.read_bytes() == before


def test_get_features_still_200_on_corrupt_settings(menv, monkeypatch):
    """A corrupt settings.json degrades the read-only feature list (200 + a
    settings_unreadable flag), never a 500."""
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    (menv.home / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")

    client = TestClient(_make_app())
    resp = client.get("/api/features")
    assert resp.status_code == 200
    assert resp.json()["settings_unreadable"] is True


def test_get_claude_settings_still_200_on_corrupt_settings(menv, monkeypatch):
    """GET /claude-settings degrades to empty defaults + a flag on a corrupt
    file rather than 500ing the settings panel."""
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    (menv.home / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")

    client = TestClient(_make_app())
    resp = client.get("/api/claude-settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings_unreadable"] is True
    assert body["permissions"]["defaultMode"] == "default"  # empty defaults


# --------------------------------------------------------------------------- #
# F8: GET /features carries the memory vault health object when installed
# --------------------------------------------------------------------------- #

def test_get_features_carries_memory_vault_health(menv, monkeypatch):
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    client = TestClient(_make_app())
    client.put("/api/features/hooks/memory_vault", json={"enabled": True})

    vault_mod.update_state(menv.home, lambda s: s.update({
        "last_capture_error": "triage failed", "capture_failures": 2,
    }))

    feats = client.get("/api/features").json()
    mv = next(h for h in feats["hooks"] if h["name"] == "memory_vault")
    assert mv["installed"] is True
    assert "health" in mv and mv["health"] is not None
    assert mv["health"]["last_capture_error"] == "triage failed"
    assert mv["health"]["capture_failures"] == 2


def test_get_features_no_health_when_not_installed(menv, monkeypatch):
    monkeypatch.setattr(features, "SETTINGS_JSON", menv.home / ".claude" / "settings.json")
    client = TestClient(_make_app())
    feats = client.get("/api/features").json()
    mv = next(h for h in feats["hooks"] if h["name"] == "memory_vault")
    assert mv["installed"] is False
    assert "health" not in mv


# --------------------------------------------------------------------------- #
# F8 / F13: settings.js health line + git-hook skipped toast (source assertions)
# --------------------------------------------------------------------------- #

def test_settings_js_has_memory_health_line():
    js = (DATA / "web" / "js" / "components" / "settings.js").read_text(encoding="utf-8")
    assert "h.name === 'memory_vault' && h.installed && h.health" in js
    assert "capture failing:" in js
    assert "sync failing:" in js
    for marker in ("capture failing:", "sync failing:"):
        line = next(ln for ln in js.splitlines() if marker in ln)
        assert "—" not in line  # user-facing copy stays em-dash free


def test_settings_js_has_git_hooks_skipped_toast():
    js = (DATA / "web" / "js" / "components" / "settings.js").read_text(encoding="utf-8")
    assert "res.git_hooks" in js
    assert "skipped post-merge hook install" in js
    line = next(ln for ln in js.splitlines() if "skipped post-merge hook install" in ln)
    assert "—" not in line
    assert "'warning'" in line


def test_init_already_initialized_reinstalls_hooks(menv):
    """`jacked memory init` on an ALREADY-initialized vault still finishes the
    enable (reinstalls stripped hooks) — the wave-3 pin for the already-init
    wiring path."""
    from click.testing import CliRunner

    from jacked.cli import main
    from jacked.memory import settings_io

    root = _github_root(menv)
    _make_repo(root / "widget", "git@github.com:acme/widget.git")
    r1 = CliRunner().invoke(main, ["memory", "init", "--root", str(root), "--yes"])
    assert r1.exit_code == 0, r1.output

    # Strip the hooks out-of-band, then re-run init on the existing vault.
    sp = settings_io.settings_path(menv.home)
    settings = settings_io.read_settings(sp)
    hooks_config.remove_capture_entries(settings)
    hooks_config.remove_recall_entries(settings)
    settings_io.write_settings(sp, settings)
    assert not hooks_config.has_capture_entry(settings_io.read_settings(sp))

    r2 = CliRunner().invoke(main, ["memory", "init", "--root", str(root), "--yes"])
    assert r2.exit_code == 0, r2.output
    after = settings_io.read_settings(sp)
    assert hooks_config.has_capture_entry(after)
    assert hooks_config.has_recall_entry(after)


def test_detect_diverts_to_jacked_home_when_settings_json_is_stock(tmp_path, monkeypatch):
    """Pin the $JACKED_HOME divergence branch of memory_vault detection.

    When SETTINGS_JSON sits at the STOCK location (Path.home()-derived) but
    $JACKED_HOME redirects the memory engine elsewhere, detection must read
    the engine's file — that is the original "checkbox misreports its own
    toggle under a redirected home" bug. The sibling test above pins the
    other branch (a re-pinned SETTINGS_JSON is authoritative); this one
    keeps the divert body from becoming silently deletable dead code.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # SETTINGS_JSON == the stock location under the (patched) home: empty file.
    stock = fake_home / ".claude" / "settings.json"
    stock.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(features, "SETTINGS_JSON", stock)

    # The engine's home is redirected — and ITS settings has the capture entry.
    engine_home = tmp_path / "engine-home"
    (engine_home / ".claude").mkdir(parents=True)
    engine_settings: dict = {}
    hooks_config.ensure_capture_entries(engine_settings, "jacked _hook memory_capture")
    (engine_home / ".claude" / "settings.json").write_text(
        json.dumps(engine_settings), encoding="utf-8"
    )
    monkeypatch.setenv("JACKED_HOME", str(engine_home))

    assert features._detect_hook_installed({}, "memory_vault") is True
