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
import shutil
import stat
import sys
import tempfile
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


# Back-compat: prefer the explicit test-home env, but fall back to $HOME —
# jacked now overrides the child HOME to the same tree (see _run_skills), so a
# future test could drop SKILLS_TEST_HOME entirely and this shim still works.
home = Path(os.environ.get("SKILLS_TEST_HOME") or os.environ["HOME"])
scenario = {}
sp = os.environ.get("SKILLS_SCENARIO")
if sp and os.path.exists(sp):
    scenario = json.loads(Path(sp).read_text())

# Record the env jacked handed us so tests can assert HOME/USERPROFILE.
try:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".child-env.json").write_text(json.dumps({
        "HOME": os.environ.get("HOME"),
        "USERPROFILE": os.environ.get("USERPROFILE"),
    }))
except OSError:
    pass

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
if mode == "read_stdin":
    # DEVNULL -> read() returns "" immediately; an inherited TTY/pipe could
    # block. Record what we saw, then fall through to normal handling.
    data = sys.stdin.read()
    try:
        (home / ".stdin-eof").write_text("eof" if data == "" else "data")
    except OSError:
        pass

# The CLI spec is pinned (skills@<version>), so match either the bare token or
# a version-pinned one -- never assume an unpinned "skills".
try:
    i = next(k for k, t in enumerate(argv) if t == "skills" or t.startswith("skills@"))
    sub = argv[i + 1]
except (StopIteration, IndexError):
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
    assert packs.load_state(home) == {"version": 2, "packs": {}}
    packs.set_enabled(home, "marketing", True)
    state = packs.load_state(home)
    assert state["version"] == 2
    assert state["packs"]["marketing"]["state"] == "enabled"
    assert state["packs"]["marketing"]["at"]
    assert packs.enabled_pack_names(home) == ["marketing"]
    assert packs.pack_state(home, "marketing") == "enabled"
    # atomic write leaves no stray tmp file behind (any name)
    leftovers = [p for p in (home / ".claude").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert (home / ".claude" / "jacked-packs.json").exists()


def test_save_state_tmp_is_writer_unique(tmp_path, monkeypatch):
    """Two concurrent writers must never share a tmp path: a shared name lets
    one writer os.replace the other's half-written file away. Capture the tmp
    names two saves generate and assert they differ."""
    home = tmp_path
    seen = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(packs.tempfile, "mkstemp", spy_mkstemp)
    packs.save_state(home, {"version": 2, "packs": {}})
    packs.save_state(home, {"version": 2, "packs": {"marketing": {"state": "enabled"}}})
    assert len(seen) == 2 and seen[0] != seen[1]
    # both tmps were consumed by os.replace
    assert not Path(seen[0]).exists() and not Path(seen[1]).exists()


def test_save_state_cleans_tmp_on_write_failure(tmp_path, monkeypatch):
    """A failed write must not leave the unique tmp behind."""
    home = tmp_path
    (home / ".claude").mkdir(parents=True, exist_ok=True)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(packs.os, "replace", boom)
    with pytest.raises(OSError):
        packs.save_state(home, {"version": 2, "packs": {}})
    leftovers = [p for p in (home / ".claude").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_state_disable_is_durable_not_dropped(tmp_path):
    """Disable must RECORD a durable 'disabled' state (not drop the entry), so a
    default-on pack the user removed is not silently reinstalled on next
    install. This is the whole point of the v2 three-state model."""
    home = tmp_path
    packs.set_enabled(home, "marketing", True)
    packs.set_enabled(home, "design-extras", True)
    packs.set_enabled(home, "marketing", False)
    # marketing is no longer explicitly ENABLED...
    assert packs.enabled_pack_names(home) == ["design-extras"]
    # ...but it IS explicitly disabled (durable), not simply forgotten.
    assert packs.pack_state(home, "marketing") == "disabled"
    assert packs.pack_state(home, "design-extras") == "enabled"


def test_v1_state_migrated_to_v2_on_load(tmp_path):
    """A v1 state file ({"version":1,"enabled":{name:{enabled_at}}}) is migrated
    in memory: enabled entries become state=enabled, none become disabled."""
    home = tmp_path
    p = home / ".claude" / "jacked-packs.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "version": 1,
        "enabled": {"marketing": {"enabled_at": "2026-01-01T00:00:00Z"}},
    }), encoding="utf-8")
    state = packs.load_state(home)
    assert state["version"] == 2
    assert state["packs"]["marketing"] == {"state": "enabled", "at": "2026-01-01T00:00:00Z"}
    assert packs.pack_state(home, "marketing") == "enabled"
    assert packs.enabled_pack_names(home) == ["marketing"]


def test_state_corrupt_tolerated(tmp_path):
    home = tmp_path
    p = home / ".claude" / "jacked-packs.json"
    p.parent.mkdir(parents=True)
    p.write_text("{broken", encoding="utf-8")
    assert packs.load_state(home) == {"version": 2, "packs": {}}
    assert packs.enabled_pack_names(home) == []
    # a subsequent set_enabled recovers cleanly over the corrupt file
    packs.set_enabled(home, "marketing", True)
    assert packs.enabled_pack_names(home) == ["marketing"]


def test_state_newer_version_preserves_unknown_fields_and_warns(tmp_path, caplog):
    """A file written by a future jacked (v3 with fields this build doesn't
    know) must NOT be silently truncated on write-back: unknown top-level keys
    and unknown per-entry fields survive the load->mutate->save round-trip, and
    a warning is logged (doctrine: never silently drop data)."""
    home = tmp_path
    p = home / ".claude" / "jacked-packs.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "version": 3,
        "profiles": {"work": ["marketing"]},          # unknown top-level key
        "packs": {
            "marketing": {"state": "enabled", "at": "x", "pinned_sha": "abc123"},
        },
    }), encoding="utf-8")

    with caplog.at_level("WARNING"):
        packs.load_state(home)
    assert any("newer than this build" in r.getMessage() for r in caplog.records)

    # A write-back (set_enabled on a DIFFERENT pack) must keep the v3 extras.
    packs.set_enabled(home, "design-extras", True)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["profiles"] == {"work": ["marketing"]}          # top-level key kept
    assert raw["packs"]["marketing"]["pinned_sha"] == "abc123"  # per-entry field kept
    assert raw["packs"]["design-extras"]["state"] == "enabled"  # our write applied


def test_effective_enablement_default_on_and_opt_out(tmp_path):
    """effective enablement: default-on unless explicitly disabled; default-off
    (opt-in) unless explicitly enabled."""
    home = tmp_path
    on = _pack(name="on", skills=("a",))
    on = packs.Pack(**{**on.__dict__, "default": True})
    off = _pack(name="off", skills=("b",))
    off = packs.Pack(**{**off.__dict__, "default": False})
    registry = {"on": on, "off": off}

    # Fresh: default-on installs, opt-in doesn't.
    assert packs.is_effectively_enabled(on, home) is True
    assert packs.is_effectively_enabled(off, home) is False
    assert packs.effective_enabled_pack_names(home, registry) == ["on"]

    # Disable the default one, enable the opt-in one: effective set flips.
    packs.set_enabled(home, "on", False)
    packs.set_enabled(home, "off", True)
    assert packs.is_effectively_enabled(on, home) is False
    assert packs.is_effectively_enabled(off, home) is True
    assert packs.effective_enabled_pack_names(home, registry) == ["off"]


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
    without = packs._add_command(npx, p, list(p.skills), include_codex=False)
    with_codex = packs._add_command(npx, p, list(p.skills), include_codex=True)
    assert without[-2:] == ["-a", "claude-code"]
    assert with_codex[-3:] == ["-a", "claude-code", "codex"]
    assert "codex" not in without
    assert packs.SKILLS_CLI_SPEC in without and "skills" not in without

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


def test_remove_pack_nonzero_exit(skills_env):
    """npx remove exits non-zero: nothing verified removed, loud failure."""
    env = skills_env
    p = _pack(skills=("alpha",))
    packs.install_pack(p, env.home, include_codex=False)
    env.set_scenario(mode="exit_nonzero", exit_code=2, stderr="remove boom")
    res = packs.remove_pack(p, env.home)
    assert res.ok is False
    assert "remove boom" in res.message
    # skill untouched on disk
    assert (env.home / ".claude" / "skills" / "alpha" / "SKILL.md").exists()


def test_remove_pack_rc0_but_skill_persists(skills_env):
    """The rc=0 gotcha on the REMOVE side: the CLI exits 0 but a skill is
    still on disk afterward. Verification must fail loud, naming it."""
    env = skills_env
    p = _pack(skills=("alpha", "beta"))
    packs.install_pack(p, env.home, include_codex=False)
    env.set_scenario(mode="normal", skip=["beta"])  # remove silently skips beta
    res = packs.remove_pack(p, env.home)
    assert res.ok is False
    assert res.removed == ["alpha"]
    assert "beta" in res.message
    assert (env.home / ".agents" / "skills" / "beta" / "SKILL.md").exists()


def test_run_skills_launch_oserror(skills_env, monkeypatch, tmp_path):
    """npx path exists but won't exec (broken node): loud, no crash."""
    env = skills_env
    broken = tmp_path / "broken-npx"
    broken.write_text("not executable content")  # no +x bit
    monkeypatch.setattr(packs, "find_npx", lambda: str(broken))
    res = packs.install_pack(_pack(), env.home, include_codex=False)
    assert res.ok is False
    assert "Failed to run npx" in res.message


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


def test_pack_status_claude_real_dir_no_canonical_is_installed(skills_env):
    # The claude-code-only layout the skills CLI produces on a REAL jacked
    # install: a real ~/.claude/skills/<n>/ dir with NO ~/.agents canonical
    # (jacked seeds ~/.claude/skills with the vendored suite first, so the CLI
    # installs the pack skill directly there rather than via an .agents symlink).
    # Claude Code reads ~/.claude/skills/<n>/SKILL.md, so this IS installed --
    # requiring the .agents canonical here was a false-negative that made every
    # real install report the pack missing and re-run its install forever.
    env = skills_env
    p = _pack(skills=("alpha",))
    packs.install_pack(p, env.home, include_codex=False)
    claude = env.home / ".claude" / "skills" / "alpha"
    canon = env.home / ".agents" / "skills" / "alpha"
    if claude.is_symlink():
        claude.unlink()
    shutil.copytree(canon, claude)   # real dir in .claude (not a symlink)
    shutil.rmtree(canon)             # no .agents canonical

    assert (claude / "SKILL.md").exists()
    assert packs._skill_installed(env.home, "alpha") is True
    st = packs.pack_status(p, env.home)
    assert st["skills"][0]["installed"] is True
    assert st["installed_count"] == 1


# --------------------------------------------------------------------------- #
# Pinned CLI spec + argv shape
# --------------------------------------------------------------------------- #

def test_every_operation_uses_pinned_spec(skills_env):
    env = skills_env
    p = _pack(skills=("alpha",), source="acme/skills")
    packs.install_pack(p, env.home, include_codex=False)
    packs.update_packs([p], env.home, include_codex=False)
    packs.remove_pack(p, env.home)

    calls = _calls(env)
    assert calls, "no npx calls recorded"
    for c in calls:
        assert packs.SKILLS_CLI_SPEC in c, f"call missing pinned spec: {c}"
        assert "skills" not in c, f"call has a bare unpinned 'skills' token: {c}"


def test_update_argv_is_scoped_to_named_skills(skills_env):
    env = skills_env
    p = _pack(skills=("alpha", "beta"), source="acme/skills")
    packs.install_pack(p, env.home, include_codex=False)
    packs.update_packs([p], env.home, include_codex=False)

    update_call = next(c for c in _calls(env) if "update" in c)
    assert "alpha" in update_call and "beta" in update_call
    assert packs.SKILLS_CLI_SPEC in update_call
    assert "skills" not in update_call


# --------------------------------------------------------------------------- #
# stdin hardening + HOME env override
# --------------------------------------------------------------------------- #

def test_stdin_is_devnull_reads_eof(skills_env):
    env = skills_env
    env.set_scenario(mode="read_stdin")
    p = _pack(skills=("alpha",))
    res = packs.install_pack(p, env.home, include_codex=False)
    assert res.ok is True                       # falls through to a normal add
    assert (env.home / ".stdin-eof").read_text() == "eof"


def test_child_home_points_at_target_home(skills_env):
    env = skills_env
    packs.install_pack(_pack(skills=("alpha",)), env.home, include_codex=False)
    child_env = json.loads((env.home / ".child-env.json").read_text())
    assert child_env["HOME"] == str(env.home)
    assert child_env["USERPROFILE"] == str(env.home)


# --------------------------------------------------------------------------- #
# install_pack collision guard (confirmed user-data-loss)
# --------------------------------------------------------------------------- #

def test_install_collision_guard_user_dir_skipped(skills_env):
    env = skills_env
    # The user hand-placed their own skill dir; no lockfile entry at all.
    userdir = env.home / ".claude" / "skills" / "alpha"
    userdir.mkdir(parents=True)
    (userdir / "SKILL.md").write_text("MY OWN WORK\n")

    p = _pack(skills=("alpha", "beta"), source="acme/skills")
    res = packs.install_pack(p, env.home, include_codex=False)

    assert res.ok is True                       # skips do not fail the op
    assert res.skipped == ["alpha"]
    assert res.installed == ["beta"]
    assert "Refusing to overwrite" in res.message and "acme/skills" in res.message
    # alpha was NEVER handed to the CLI; beta was.
    add_call = next(c for c in _calls(env) if "add" in c)
    assert "alpha" not in add_call and "beta" in add_call
    # the user's content is untouched
    assert (userdir / "SKILL.md").read_text() == "MY OWN WORK\n"


def test_install_collision_guard_foreign_source_skipped(skills_env):
    env = skills_env
    _seed_installed(env.home, "alpha", source="someone/else")
    p = _pack(skills=("alpha", "beta"), source="acme/skills")
    res = packs.install_pack(p, env.home, include_codex=False)

    assert res.ok is True
    assert res.skipped == ["alpha"]
    assert res.installed == ["beta"]
    add_call = next(c for c in _calls(env) if "add" in c)
    assert "alpha" not in add_call


def test_install_collision_guard_own_source_refreshes(skills_env):
    env = skills_env
    p = _pack(skills=("alpha",), source="acme/skills")
    packs.install_pack(p, env.home, include_codex=False)   # first install, own source in lock
    res = packs.install_pack(p, env.home, include_codex=False)  # reinstall/refresh

    assert res.ok is True
    assert res.installed == ["alpha"]
    assert res.skipped == []
    add_calls = [c for c in _calls(env) if "add" in c]
    assert len(add_calls) == 2                  # own-source skill is NOT skipped
    assert "alpha" in add_calls[1]


def test_install_collision_guard_all_skipped_no_subprocess(skills_env):
    env = skills_env
    _seed_installed(env.home, "alpha", source="someone/else")
    p = _pack(skills=("alpha",), source="acme/skills")
    res = packs.install_pack(p, env.home, include_codex=False)

    assert res.ok is True
    assert res.skipped == ["alpha"]
    assert res.installed == []
    assert _calls(env) == []                    # subprocess skipped entirely


# --------------------------------------------------------------------------- #
# update_packs per-pack attribution
# --------------------------------------------------------------------------- #

def test_update_packs_per_pack_attribution(skills_env):
    env = skills_env
    good = _pack(skills=("alpha",), source="acme/skills", name="good")
    bad = _pack(skills=("beta", "gamma"), source="acme/skills", name="bad")
    packs.install_pack(good, env.home, include_codex=False)
    packs.install_pack(bad, env.home, include_codex=False)

    # gamma becomes un-reinstallable and is dropped from disk (lock lingers).
    env.set_scenario(mode="normal", skip=["gamma"])
    _delete_skill(env.home, "gamma")

    res = packs.update_packs([good, bad], env.home, include_codex=False)
    assert res.ok is False
    assert res.per_pack["good"].ok is True
    assert res.per_pack["bad"].ok is False
    assert "gamma" in res.per_pack["bad"].missing
    assert res.per_pack["good"].missing == []


def test_update_packs_command_failure_marks_all_packs(skills_env):
    env = skills_env
    good = _pack(skills=("alpha",), source="acme/skills", name="good")
    bad = _pack(skills=("beta",), source="acme/skills", name="bad")
    packs.install_pack(good, env.home, include_codex=False)
    packs.install_pack(bad, env.home, include_codex=False)

    env.set_scenario(mode="exit_nonzero", exit_code=3, stderr="update boom")
    res = packs.update_packs([good, bad], env.home, include_codex=False)

    assert res.ok is False
    assert res.per_pack["good"].ok is False
    assert res.per_pack["bad"].ok is False
    assert "update boom" in res.per_pack["good"].message
    assert "update boom" in res.per_pack["bad"].message


# --------------------------------------------------------------------------- #
# Registry structural validation
# --------------------------------------------------------------------------- #

def test_registry_validation_rejects_hostile_packs(tmp_path, caplog):
    bad = {
        "packs": {
            "dashg":     {"source": "acme/skills", "skills": ["-g"]},
            "traversal": {"source": "acme/skills", "skills": ["../evil"]},
            "strskills": {"source": "acme/skills", "skills": "alpha"},
            "badsource": {"source": "--help", "skills": ["alpha"]},
            "badhome":   {"source": "acme/skills", "skills": ["alpha"],
                          "homepage": "javascript:alert(1)"},
            "good":      {"source": "acme/skills", "skills": ["alpha"],
                          "homepage": "https://example.com"},
        }
    }
    (tmp_path / "packs.json").write_text(json.dumps(bad), encoding="utf-8")
    with caplog.at_level("WARNING"):
        reg = packs.load_registry(tmp_path)

    assert set(reg) == {"good"}
    for name in ("dashg", "traversal", "strskills", "badsource", "badhome"):
        assert name in caplog.text


def test_registry_source_segments_must_start_alnum(tmp_path, caplog):
    """Leading '-' (argv flag smuggling) and leading '.' ('..', '.git') in
    EITHER source segment must be rejected — the source lands positionally on
    the skills-CLI argv with no terminator."""
    bad = {
        "packs": {
            "updir":    {"source": "../evil", "skills": ["alpha"]},
            "flagown":  {"source": "-g/x", "skills": ["alpha"]},
            "flagrepo": {"source": "owner/-flag", "skills": ["alpha"]},
            "dotrepo":  {"source": "owner/..", "skills": ["alpha"]},
            "hidden":   {"source": ".git/x", "skills": ["alpha"]},
            "good":     {"source": "acme/skills", "skills": ["alpha"]},
        }
    }
    (tmp_path / "packs.json").write_text(json.dumps(bad), encoding="utf-8")
    with caplog.at_level("WARNING"):
        reg = packs.load_registry(tmp_path)
    assert set(reg) == {"good"}
    for name in ("updir", "flagown", "flagrepo", "dotrepo", "hidden"):
        assert name in caplog.text


def test_run_skills_pins_cwd_to_home(skills_env, monkeypatch):
    """cwd must be the verification home: npm exec prefers a cwd-local (or
    ancestor) node_modules/skills over the pinned registry package, so an
    inherited launch cwd inside an untrusted checkout defeats the version pin
    (proven empirically). Every subprocess call must pin cwd."""
    env = skills_env
    calls = []
    real_run = packs.subprocess.run

    def spy_run(cmd, **kwargs):
        calls.append(kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(packs.subprocess, "run", spy_run)
    p = _pack(skills=("alpha",))
    packs.install_pack(p, env.home, include_codex=False)
    packs.update_packs([p], env.home, include_codex=False)
    packs.remove_pack(p, env.home)
    assert calls, "no subprocess calls recorded"
    for kwargs in calls:
        assert kwargs.get("cwd") == str(env.home)


def test_registry_default_non_bool_coerced_to_false_with_warning(tmp_path, caplog):
    """A stray `"default": "false"` (a non-empty string) must NOT truthy-coerce
    a pack ON: it falls back to opt-in (False) with a warning, rather than
    silently defaulting the pack in for every user."""
    reg = {"packs": {
        "sneaky": {"source": "acme/skills", "skills": ["alpha"], "default": "false"},
        "good": {"source": "acme/skills", "skills": ["beta"], "default": True},
    }}
    (tmp_path / "packs.json").write_text(json.dumps(reg), encoding="utf-8")
    with caplog.at_level("WARNING"):
        packs_reg = packs.load_registry(tmp_path)
    assert packs_reg["sneaky"].default is False    # not coerced True
    assert packs_reg["good"].default is True
    assert any("'default' is not a bool" in r.getMessage() for r in caplog.records)


def test_install_pack_skips_dangling_symlink_no_lock(skills_env):
    """A dangling symlink at ~/.claude/skills/<n> with no own-source lock entry
    must be treated as a pre-existing trace the guard refuses to overwrite (not
    a clobber), and _skill_installed stays False for it. Pins the wild-card
    behavior so a future _skill_present/_skill_installed merge can't break it."""
    env = skills_env
    link = env.home / ".claude" / "skills" / "alpha"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(env.home / "nonexistent-target")  # dangling
    assert link.is_symlink() and not link.exists()
    assert packs._skill_installed(env.home, "alpha") is False

    res = packs.install_pack(_pack(skills=("alpha",)), env.home, include_codex=False)
    assert "alpha" in res.skipped               # guard refused it
    assert not _calls(env)                       # never handed to the CLI
    assert link.is_symlink()                     # untouched (not clobbered)


def test_registry_packs_not_a_dict(tmp_path, caplog):
    (tmp_path / "packs.json").write_text(json.dumps({"packs": ["a", "b"]}), encoding="utf-8")
    with caplog.at_level("WARNING"):
        reg = packs.load_registry(tmp_path)
    assert reg == {}
    assert "packs" in caplog.text


# --------------------------------------------------------------------------- #
# Lockfile version guard
# --------------------------------------------------------------------------- #

def test_read_lockfile_version_mismatch_warns_but_returns(tmp_path, caplog):
    lock_path = tmp_path / ".agents" / ".skill-lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"version": 4, "skills": {"x": {"source": "a/b"}}}))

    with caplog.at_level("WARNING"):
        data = packs.read_lockfile(tmp_path)

    assert data["version"] == 4
    assert data["skills"]["x"]["source"] == "a/b"   # parse still returned intact
    assert "version" in caplog.text and "4" in caplog.text


def test_read_lockfile_version_3_no_warning(tmp_path, caplog):
    lock_path = tmp_path / ".agents" / ".skill-lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"version": 3, "skills": {}}))
    with caplog.at_level("WARNING"):
        data = packs.read_lockfile(tmp_path)
    assert data["version"] == 3
    assert "!= 3" not in caplog.text


# --------------------------------------------------------------------------- #
# jacked_home
# --------------------------------------------------------------------------- #

def test_jacked_home_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    assert packs.jacked_home() == tmp_path


def test_jacked_home_defaults_to_home(monkeypatch):
    monkeypatch.delenv("JACKED_HOME", raising=False)
    assert packs.jacked_home() == Path.home()


def test_run_skills_strips_terminal_escapes_from_tail(skills_env, caplog):
    """A compromised upstream package could emit ANSI/OSC escape sequences on
    stderr that fire when the tail is later cat'd from the log. _tail must
    strip control chars so both the returned message and the log record are
    inert. Newlines/tabs are preserved."""
    env = skills_env
    hostile = "npm ERR!\x1b[2J\x1b]52;c;ZXZpbA==\x07 boom\ttab\nline"
    env.set_scenario(mode="exit_nonzero", exit_code=1, stderr=hostile)
    with caplog.at_level("WARNING"):
        res = packs.install_pack(_pack(), env.home, include_codex=False)
    assert res.ok is False
    assert "\x1b" not in res.message and "\x07" not in res.message
    assert "npm ERR!" in res.message and "boom" in res.message
    assert "\n" in res.message  # newlines survive the strip
    for rec in caplog.records:
        assert "\x1b" not in rec.getMessage()


def test_registry_source_rejects_trailing_newline(tmp_path, caplog):
    """\\Z (not $) anchoring: a trailing newline in a source must be rejected."""
    bad = {"packs": {"nl": {"source": "acme/skills\n", "skills": ["alpha"]},
                     "good": {"source": "acme/skills", "skills": ["alpha"]}}}
    (tmp_path / "packs.json").write_text(json.dumps(bad), encoding="utf-8")
    with caplog.at_level("WARNING"):
        reg = packs.load_registry(tmp_path)
    assert set(reg) == {"good"}
