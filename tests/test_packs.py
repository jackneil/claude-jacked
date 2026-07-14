"""Tests for jacked.packs — skill-pack registry, state, and npx orchestration.

No network and no real npx. A fake ``npx`` shim (a small Python script written
into tmp_path, run via its shebang) emulates the verified vercel-labs/skills
behavior: ``add`` creates ~/.agents/skills/<name>/SKILL.md + a relative symlink
under ~/.claude/skills and updates the lockfile; ``remove`` deletes them + the
lock entry; ``update`` touches updatedAt. A JSON "scenario" (pointed at by the
SKILLS_SCENARIO env var) lets a test make the shim succeed, silently skip a
named skill while still exiting 0 (the rc=0 gotcha), exit non-zero, or hang.
"""
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jacked import packs

DATA_ROOT = Path(packs.__file__).resolve().parent / "data"


# --------------------------------------------------------------------------- #
# Fake npx shim
# --------------------------------------------------------------------------- #

# Plain (non-f, raw) string: `\n` here must survive into the generated .py as a
# two-char escape, so the shim interprets it as a newline at run time.
_SHIM_BODY = r'''
import json, os, shutil, sys, time
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


home = Path(os.environ["SKILLS_TEST_HOME"])
scenario = {}
sp = os.environ.get("SKILLS_SCENARIO")
if sp and os.path.exists(sp):
    scenario = json.loads(Path(sp).read_text())

argv = sys.argv[1:]

# Record every invocation so tests can assert command shape / ordering.
try:
    log = home / ".npx-calls.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as fh:
        fh.write(json.dumps(argv) + "\n")
except OSError:
    pass

mode = scenario.get("mode", "normal")
skip = set(scenario.get("skip", []))

if mode == "hang":
    time.sleep(30)
    sys.exit(0)
if mode == "exit_nonzero":
    sys.stderr.write(scenario.get("stderr", "fake npx error"))
    sys.exit(int(scenario.get("exit_code", 1)))

try:
    i = argv.index("skills")
    sub = argv[i + 1]
except (ValueError, IndexError):
    sys.exit(0)


def tokens_after(flag):
    out = []
    if flag in argv:
        for t in argv[argv.index(flag) + 1:]:
            if t.startswith("-"):
                break
            out.append(t)
    return out


lock_path = home / ".agents" / ".skill-lock.json"


def read_lock():
    try:
        return json.loads(lock_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 3, "skills": {}, "dismissed": {}}


def write_lock(lock):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2))


if sub == "add":
    source = argv[i + 2]
    lock = read_lock()
    lock.setdefault("skills", {})
    for name in tokens_after("--skill"):
        if name in skip:
            continue
        canon = home / ".agents" / "skills" / name
        canon.mkdir(parents=True, exist_ok=True)
        (canon / "SKILL.md").write_text("# " + name + "\nfake skill\n")
        claude_skills = home / ".claude" / "skills"
        claude_skills.mkdir(parents=True, exist_ok=True)
        link = claude_skills / name
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)
        target = os.path.join("..", "..", ".agents", "skills", name)
        try:
            os.symlink(target, link)
        except OSError:
            shutil.copytree(canon, link)
        now = _now()
        entry = lock["skills"].get(name, {})
        entry.update({
            "source": source,
            "sourceType": "github",
            "sourceUrl": "https://github.com/" + source,
            "skillPath": name,
            "skillFolderHash": "deadbeef",
            "installedAt": entry.get("installedAt", now),
            "updatedAt": now,
        })
        lock["skills"][name] = entry
    write_lock(lock)
    sys.exit(0)

if sub == "remove":
    names = [t for t in argv[i + 2:] if not t.startswith("-")]
    lock = read_lock()
    for name in names:
        if name in skip:
            continue
        link = home / ".claude" / "skills" / name
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)
        canon = home / ".agents" / "skills" / name
        if canon.is_dir():
            shutil.rmtree(canon)
        elif canon.is_symlink() or canon.is_file():
            canon.unlink()
        lock.get("skills", {}).pop(name, None)
    write_lock(lock)
    sys.exit(0)

if sub == "update":
    lock = read_lock()
    now = _now()
    for entry in lock.get("skills", {}).values():
        if isinstance(entry, dict):
            entry["updatedAt"] = now
    write_lock(lock)
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    """A tmp HOME + a fake npx shim wired into jacked.packs.find_npx."""
    home = tmp_path / "home"
    home.mkdir()

    shim = tmp_path / "npx"
    shim.write_text(f"#!{sys.executable}\n" + _SHIM_BODY, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)

    monkeypatch.setattr(packs, "find_npx", lambda: str(shim))
    scenario_path = tmp_path / "scenario.json"

    def set_scenario(**kw):
        scenario_path.write_text(json.dumps(kw), encoding="utf-8")
        monkeypatch.setenv("SKILLS_SCENARIO", str(scenario_path))
        monkeypatch.setenv("SKILLS_TEST_HOME", str(home))

    set_scenario(mode="normal")
    return SimpleNamespace(
        home=home, shim=shim, set_scenario=set_scenario,
        calls_log=home / ".npx-calls.log",
    )


def _pack(skills=("alpha", "beta"), source="acme/skills", name="testpack"):
    return packs.Pack(
        name=name, display_name=f"{name.title()} Display",
        description="test pack", source=source,
        homepage=f"https://github.com/{source}", skills=tuple(skills),
    )


def _calls(env):
    if not env.calls_log.exists():
        return []
    return [json.loads(line) for line in env.calls_log.read_text().splitlines() if line]


def _delete_skill(home, name):
    link = home / ".claude" / "skills" / name
    if link.is_symlink() or link.exists():
        link.unlink()
    canon = home / ".agents" / "skills" / name
    if canon.exists():
        import shutil
        shutil.rmtree(canon)


def _seed_installed(home, name, source):
    """Manually place a skill on disk + a lockfile entry (bypasses the shim)."""
    canon = home / ".agents" / "skills" / name
    canon.mkdir(parents=True, exist_ok=True)
    (canon / "SKILL.md").write_text("seed\n")
    claude_skills = home / ".claude" / "skills"
    claude_skills.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.join("..", "..", ".agents", "skills", name), claude_skills / name)
    lock_path = home / ".agents" / ".skill-lock.json"
    try:
        lock = json.loads(lock_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        lock = {"version": 3, "skills": {}, "dismissed": {}}
    lock.setdefault("skills", {})[name] = {
        "source": source, "sourceType": "github",
        "installedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    }
    lock_path.write_text(json.dumps(lock, indent=2))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_registry_load_happy():
    reg = packs.load_registry(DATA_ROOT)
    assert set(reg) == {"marketing", "design-extras"}
    mk = reg["marketing"]
    assert mk.source == "coreyhaines31/marketingskills"
    assert isinstance(mk.skills, tuple) and "pricing" in mk.skills
    assert reg["design-extras"].skills == ("improve-animations",)


def test_registry_load_missing_file(tmp_path):
    assert packs.load_registry(tmp_path) == {}


def test_registry_load_malformed(tmp_path):
    (tmp_path / "packs.json").write_text("{not json", encoding="utf-8")
    assert packs.load_registry(tmp_path) == {}


def test_no_em_dash_in_descriptions():
    reg = packs.load_registry(DATA_ROOT)
    for p in reg.values():
        assert "—" not in p.description
        assert "—" not in p.display_name


def test_pack_skill_names_unique_and_no_jacked_collision():
    reg = packs.load_registry(DATA_ROOT)
    seen: dict[str, str] = {}
    for p in reg.values():
        for s in p.skills:
            assert s not in seen, f"skill {s!r} duplicated across packs {seen.get(s)} + {p.name}"
            seen[s] = p.name

    vendored = {d.name for d in (DATA_ROOT / "skills").iterdir() if d.is_dir()}
    clashes = set(seen) & vendored
    assert not clashes, f"pack skills shadow jacked-vendored skills: {clashes}"


# --------------------------------------------------------------------------- #
# Enable-state
# --------------------------------------------------------------------------- #

def test_state_roundtrip_and_atomic(tmp_path):
    home = tmp_path
    assert packs.load_state(home) == {}
    packs.set_enabled(home, "marketing", True)
    state = packs.load_state(home)
    assert state["version"] == 1
    assert "marketing" in state["enabled"]
    assert "enabled_at" in state["enabled"]["marketing"]
    assert packs.enabled_pack_names(home) == ["marketing"]
    # atomic write leaves no stray tmp file behind
    assert not (home / ".claude" / "jacked-packs.json.tmp").exists()
    assert (home / ".claude" / "jacked-packs.json").exists()


def test_state_disable_drops_entry(tmp_path):
    home = tmp_path
    packs.set_enabled(home, "marketing", True)
    packs.set_enabled(home, "design-extras", True)
    packs.set_enabled(home, "marketing", False)
    assert packs.enabled_pack_names(home) == ["design-extras"]


def test_state_corrupt_tolerated(tmp_path):
    home = tmp_path
    p = home / ".claude" / "jacked-packs.json"
    p.parent.mkdir(parents=True)
    p.write_text("{broken", encoding="utf-8")
    assert packs.load_state(home) == {}
    assert packs.enabled_pack_names(home) == []
    # a subsequent set_enabled recovers cleanly over the corrupt file
    packs.set_enabled(home, "marketing", True)
    assert packs.enabled_pack_names(home) == ["marketing"]


# --------------------------------------------------------------------------- #
# install_pack
# --------------------------------------------------------------------------- #

def test_install_pack_happy(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta"))
    res = packs.install_pack(p, env.home, include_codex=False)
    assert res.ok is True
    assert res.installed == ["alpha", "beta"]
    assert res.missing == []
    for name in p.skills:
        assert (env.home / ".claude" / "skills" / name / "SKILL.md").exists()
        assert (env.home / ".agents" / "skills" / name / "SKILL.md").exists()


def test_install_pack_silently_skipped(skills_env):
    env = skills_env
    env.set_scenario(mode="normal", skip=["beta"])
    p = _pack(skills=("alpha", "beta"))
    res = packs.install_pack(p, env.home, include_codex=False)
    assert res.ok is False
    assert res.installed == ["alpha"]
    assert res.missing == ["beta"]
    assert "beta" in res.message and "packs update" in res.message


def test_install_pack_nonzero_exit(skills_env):
    env = skills_env
    env.set_scenario(mode="exit_nonzero", exit_code=2, stderr="upstream boom")
    res = packs.install_pack(_pack(), env.home, include_codex=False)
    assert res.ok is False
    assert "exited 2" in res.message
    assert "upstream boom" in res.message


def test_install_pack_timeout(skills_env):
    env = skills_env
    env.set_scenario(mode="hang")
    res = packs.install_pack(_pack(), env.home, include_codex=False, timeout=1)
    assert res.ok is False
    assert "Timed out" in res.message


def test_install_pack_npx_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "find_npx", lambda: None)
    res = packs.install_pack(_pack(), tmp_path, include_codex=False)
    assert res.ok is False
    assert res.message == packs._NPX_MISSING_MSG
    assert res.missing == list(_pack().skills)


def test_codex_flag_changes_agent_list(skills_env):
    npx = "/fake/npx"
    p = _pack(skills=("alpha",))
    without = packs._add_command(npx, p, include_codex=False)
    with_codex = packs._add_command(npx, p, include_codex=True)
    assert without[-2:] == ["-a", "claude-code"]
    assert with_codex[-3:] == ["-a", "claude-code", "codex"]
    assert "codex" not in without

    # ...and it reaches the real subprocess command line.
    env = skills_env
    packs.install_pack(p, env.home, include_codex=True)
    add_call = next(c for c in _calls(env) if "add" in c)
    assert "codex" in add_call
    assert add_call[add_call.index("-a") + 1:] == ["claude-code", "codex"]


# --------------------------------------------------------------------------- #
# remove_pack
# --------------------------------------------------------------------------- #

def test_remove_pack_own_source(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta"))
    packs.install_pack(p, env.home, include_codex=False)
    res = packs.remove_pack(p, env.home)
    assert res.ok is True
    assert sorted(res.removed) == ["alpha", "beta"]
    assert res.skipped == []
    for name in p.skills:
        assert not (env.home / ".claude" / "skills" / name).exists()
        assert not (env.home / ".agents" / "skills" / name).exists()


def test_remove_pack_refuses_foreign_source(skills_env):
    env = skills_env
    # "alpha" is on disk but installed from a DIFFERENT repo than the pack claims.
    _seed_installed(env.home, "alpha", source="someone/else")
    p = _pack(skills=("alpha",), source="acme/skills")
    res = packs.remove_pack(p, env.home)
    assert res.ok is True
    assert res.removed == []
    assert res.skipped == ["alpha"]
    # the foreign skill survives, and no npx remove was ever invoked
    assert (env.home / ".claude" / "skills" / "alpha").exists()
    assert _calls(env) == []


def test_remove_pack_no_lockfile(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta"))
    res = packs.remove_pack(p, env.home)
    assert res.ok is True
    assert res.removed == []
    assert sorted(res.skipped) == ["alpha", "beta"]
    assert "Nothing removed" in res.message
    assert _calls(env) == []


# --------------------------------------------------------------------------- #
# update_packs
# --------------------------------------------------------------------------- #

def test_update_packs_repair_reinstalls_missing(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta", "gamma"))
    packs.install_pack(p, env.home, include_codex=False)
    _delete_skill(env.home, "beta")  # drift: files gone, lock entry lingers

    res = packs.update_packs([p], env.home, include_codex=False)
    assert res.ok is True
    assert "beta" not in res.missing
    for name in p.skills:
        assert (env.home / ".claude" / "skills" / name / "SKILL.md").exists()
    calls = _calls(env)
    kinds = ["update" if "update" in c else "add" if "add" in c else "?" for c in calls]
    assert "update" in kinds and kinds.index("update") < len(kinds) - 1
    assert "add" in kinds[kinds.index("update") + 1:]  # repair add ran after update


def test_update_packs_repair_still_missing(skills_env):
    env = skills_env
    env.set_scenario(mode="normal", skip=["beta"])  # upstream keeps failing on beta
    p = _pack(skills=("alpha", "beta"))
    packs.install_pack(p, env.home, include_codex=False)  # alpha only

    res = packs.update_packs([p], env.home, include_codex=False)
    assert res.ok is False
    assert res.missing == ["beta"]
    assert "still missing" in res.message


def test_update_packs_npx_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(packs, "find_npx", lambda: None)
    res = packs.update_packs([_pack()], tmp_path, include_codex=False)
    assert res.ok is False
    assert res.message == packs._NPX_MISSING_MSG


# --------------------------------------------------------------------------- #
# pack_status
# --------------------------------------------------------------------------- #

def test_pack_status_installed(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta"))
    packs.install_pack(p, env.home, include_codex=False)
    st = packs.pack_status(p, env.home)
    assert st["installed_count"] == 2
    assert st["total"] == 2
    assert st["source"] == p.source
    for row in st["skills"]:
        assert row["installed"] is True
        assert row["source_ok"] is True
        assert row["updated_at"] is not None


def test_pack_status_not_installed(tmp_path):
    p = _pack(skills=("alpha", "beta"))
    st = packs.pack_status(p, tmp_path)
    assert st["installed_count"] == 0
    for row in st["skills"]:
        assert row["installed"] is False
        assert row["source_ok"] is None
        assert row["updated_at"] is None


def test_pack_status_foreign_source(skills_env):
    env = skills_env
    _seed_installed(env.home, "alpha", source="someone/else")
    p = _pack(skills=("alpha",), source="acme/skills")
    st = packs.pack_status(p, env.home)
    row = st["skills"][0]
    assert row["installed"] is True       # on disk
    assert row["source_ok"] is False      # but from a different repo
    assert st["installed_count"] == 1
