"""Unit tests for the agent-reach runner.

Fully offline: `subprocess.run` and `shutil.which` are mocked; no `uv tool
install`, `agent-reach`, `npm`, or `git` process is ever spawned. Pin data is a
local fixture (the real vendored pin is produced by a sibling milestone).
"""

import contextlib
import hashlib
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from jacked.integrations.agent_reach import AgentReachRunner

UPSTREAM = "https://github.com/Panniantong/Agent-Reach"
PIN_SHA = "b" * 40
REDDIT_GIT_SHA = "d" * 40

SKILL_CONTENT = {
    "SKILL.md": b"# agent-reach skill\nstanding rules\n",
    "references/twitter.md": b"twitter reference\n",
}
SKILL_HASHES = {name: hashlib.sha256(c).hexdigest() for name, c in SKILL_CONTENT.items()}

_DEFAULT_BINS = {
    "uv": "/fake/uv",
    "agent-reach": "/fake/agent-reach",
    "npm": "/fake/npm",
    "git": "/fake/git",
}


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

class FakeSettings:
    """Dict-backed stand-in for the DB settings accessors (injected persistence)."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value


def _pin_dict(commit_sha=PIN_SHA):
    return {
        "schema_version": 1,
        "upstream": UPSTREAM,
        "commit_sha": commit_sha,
        "version_label": "v1.5.0",
        "vetted_at": "2026-07-11",
        "constraints_file": "agent-reach-constraints.txt",
        "skill_hashes": dict(SKILL_HASHES),
        "channels": {
            "twitter": {"backends": [{"kind": "pipx", "spec": "twitter-cli==1.2.3"}]},
            "opencli": {
                "backends": [
                    {"kind": "npm", "spec": "@jackwener/opencli@0.5.1"},
                    {"note": "Chrome extension is manual"},
                ]
            },
            "reddit": {
                "backends": [
                    {"kind": "pipx-git", "spec": f"git+https://github.com/public-clis/rdt-cli.git@{REDDIT_GIT_SHA}"}
                ]
            },
        },
    }


def make_runner(tmp_path, settings=None, commit_sha=PIN_SHA):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    pin_path = data_dir / "agent-reach.json"
    pin_path.write_text(json.dumps(_pin_dict(commit_sha)), encoding="utf-8")
    (data_dir / "agent-reach-constraints.txt").write_text("# fully pinned\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    fs = FakeSettings(settings)
    runner = AgentReachRunner(fs.get, fs.set, pin_path=pin_path, home=home)
    return runner, fs, home


def CP(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


@contextlib.contextmanager
def patched(side_effect, which_map=None):
    which_map = _DEFAULT_BINS if which_map is None else which_map
    with mock.patch("subprocess.run", side_effect=side_effect), \
         mock.patch("shutil.which", side_effect=lambda n: which_map.get(n)):
        yield


class Recorder:
    """subprocess.run stand-in that records calls and simulates the installer.

    On the `agent-reach install` step it writes the skill files into the home
    skill dirs (tampered, if requested) so hash-verify runs against real bytes.
    """

    def __init__(self, home, uv_version="uv 0.10.2\n", write_dirs=None, tamper=False):
        self.calls = []
        self.home = home
        self.uv_version = uv_version
        self.write_dirs = write_dirs if write_dirs is not None else [Path(".claude") / "skills" / "agent-reach"]
        self.tamper = tamper

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if len(cmd) >= 2 and cmd[0].endswith("uv") and cmd[1] == "--version":
            return CP(cmd, 0, self.uv_version, "")
        if cmd[0].endswith("agent-reach") and "install" in cmd:
            self._place_skills()
        return CP(cmd, 0, "", "")

    def _place_skills(self):
        for rel in self.write_dirs:
            for name, content in SKILL_CONTENT.items():
                f = self.home / rel / name
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(content + (b"tampered" if self.tamper else b""))


def _place_skills(home, rels, tamper=False):
    for rel in rels:
        for name, content in SKILL_CONTENT.items():
            f = home / rel / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content + (b"X" if tamper else b""))


def _mark_installed(home, channels=None):
    state = {
        "installed_sha": PIN_SHA,
        "installed_at": "2026-07-11T00:00:00+00:00",
        "skill_hashes_verified": True,
        "channels_enabled": channels or [],
        "override_active": False,
    }
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "jacked-reach-state.json").write_text(json.dumps(state), encoding="utf-8")


def _find(calls, pred):
    for c in calls:
        if pred(c):
            return c
    raise AssertionError(f"no call matched; calls were: {calls}")


def _has(calls, pred):
    return any(pred(c) for c in calls)


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

class TestInstall:
    def test_runs_pinned_safe_commands(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        rec = Recorder(home)
        with patched(rec):
            result = runner.install()

        assert result["skill_hashes_verified"] is True
        assert result["installed_sha"] == PIN_SHA

        # uv tool install: -c <constraints> + the pinned git+SHA source, and
        # --force so update() deterministically overwrites when the SHA moves.
        uv_install = _find(rec.calls, lambda c: c[:3] == ["/fake/uv", "tool", "install"])
        assert "-c" in uv_install
        ci = uv_install.index("-c")
        assert uv_install[ci + 1].endswith("agent-reach-constraints.txt")
        assert f"agent-reach @ git+{UPSTREAM}@{PIN_SHA}" in uv_install
        assert "--force" in uv_install

        # agent-reach step: --safe is non-negotiable, --env=auto present
        ar = _find(rec.calls, lambda c: c[0] == "/fake/agent-reach" and "install" in c)
        assert "--safe" in ar
        assert "--env=auto" in ar

    def test_writes_state_file(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with patched(Recorder(home)):
            runner.install()
        state = json.loads((home / ".claude" / "jacked-reach-state.json").read_text())
        assert state["installed_sha"] == PIN_SHA
        assert state["skill_hashes_verified"] is True
        assert state["override_active"] is False
        assert state["channels_enabled"] == []

    def test_rejects_old_uv(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with patched(Recorder(home, uv_version="uv 0.4.9\n")):
            with pytest.raises(RuntimeError, match=r"0\.4\.9"):
                runner.install()
        # never reached the tool-install step
        assert not (home / ".claude" / "jacked-reach-state.json").exists()

    def test_rejects_unparseable_uv(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with patched(Recorder(home, uv_version="uv wat\n")):
            with pytest.raises(RuntimeError, match="could not parse"):
                runner.install()

    def test_missing_constraints_file_raises(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        (tmp_path / "data" / "agent-reach-constraints.txt").unlink()
        with patched(Recorder(home)):
            with pytest.raises(RuntimeError, match="constraints file not found"):
                runner.install()

    def test_hash_mismatch_rolls_back(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        rec = Recorder(home, tamper=True)
        with patched(rec):
            with pytest.raises(RuntimeError, match="hash mismatch"):
                runner.install()
        # rollback: uv tool uninstall called + skill dir deleted + no state file
        assert _has(rec.calls, lambda c: c[:3] == ["/fake/uv", "tool", "uninstall"])
        assert not (home / ".claude" / "skills" / "agent-reach").exists()
        assert not (home / ".claude" / "jacked-reach-state.json").exists()

    def test_rollback_covers_openclaw_skill_dir(self, tmp_path):
        """Upstream also writes ~/.openclaw/skills when present; a tampered
        install must not survive there after rollback."""
        openclaw_rel = Path(".openclaw") / "skills" / "agent-reach"
        runner, fs, home = make_runner(tmp_path)
        rec = Recorder(
            home,
            tamper=True,
            write_dirs=[Path(".claude") / "skills" / "agent-reach", openclaw_rel],
        )
        with patched(rec):
            with pytest.raises(RuntimeError, match="hash mismatch"):
                runner.install()
        assert not (home / openclaw_rel).exists()
        assert not (home / ".claude" / "skills" / "agent-reach").exists()

    def test_no_skill_dir_placed_rolls_back(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        # installer writes nothing -> verify has zero dirs -> failure + rollback
        rec = Recorder(home, write_dirs=[])
        with patched(rec):
            with pytest.raises(RuntimeError, match="hash mismatch"):
                runner.install()
        assert not (home / ".claude" / "jacked-reach-state.json").exists()

    def test_update_reinstalls(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with patched(Recorder(home)):
            result = runner.update()
        assert result["installed_sha"] == PIN_SHA


# --------------------------------------------------------------------------- #
# drift / hash verification
# --------------------------------------------------------------------------- #

class TestDriftVerify:
    def test_match(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _place_skills(home, [Path(".claude") / "skills" / "agent-reach"])
        ok, drift = runner._verify_skill_hashes(runner._load_pin())
        assert ok
        assert {d["status"] for d in drift} == {"ok"}

    def test_mismatch(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _place_skills(home, [Path(".claude") / "skills" / "agent-reach"], tamper=True)
        ok, drift = runner._verify_skill_hashes(runner._load_pin())
        assert not ok
        assert any(d["status"] == "mismatch" for d in drift)

    def test_missing_file(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        # place only SKILL.md, omit references/twitter.md
        f = home / ".claude" / "skills" / "agent-reach" / "SKILL.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(SKILL_CONTENT["SKILL.md"])
        ok, drift = runner._verify_skill_hashes(runner._load_pin())
        assert not ok
        assert any(d["status"] == "missing" and d["file"] == "references/twitter.md" for d in drift)

    def test_no_dirs_at_all_is_failure(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        ok, drift = runner._verify_skill_hashes(runner._load_pin())
        assert not ok

    def test_both_dirs_verified(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _place_skills(
            home,
            [Path(".claude") / "skills" / "agent-reach", Path(".agents") / "skills" / "agent-reach"],
        )
        ok, drift = runner._verify_skill_hashes(runner._load_pin())
        assert ok
        # two dirs x two files = four ok entries
        assert len([d for d in drift if d["status"] == "ok"]) == 4


# --------------------------------------------------------------------------- #
# override state machine
# --------------------------------------------------------------------------- #

class TestOverride:
    def test_set_requires_ack(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with pytest.raises(RuntimeError, match="ack"):
            runner.set_override("main", ack=False)
        assert fs.get("reach_override_sha") is None

    def test_set_resolves_ref_and_persists(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        resolved = "e" * 40

        def side(cmd, **kw):
            if cmd[0] == "/fake/git" and cmd[1] == "ls-remote":
                return CP(cmd, 0, f"{resolved}\trefs/heads/main\n", "")
            return CP(cmd, 0, "", "")

        with patched(side):
            out = runner.set_override("main", ack=True)
        assert out["override_sha"] == resolved
        assert fs.get("reach_override_sha") == resolved
        assert fs.get("reach_override_ack") == "true"
        assert fs.get("reach_override_at")

    def test_set_accepts_raw_sha_without_git(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        raw = "f" * 40
        calls = []

        def side(cmd, **kw):
            calls.append(list(cmd))
            return CP(cmd, 0, "", "")

        with patched(side):
            out = runner.set_override(raw, ack=True)
        assert out["override_sha"] == raw
        assert not _has(calls, lambda c: c[:2] == ["/fake/git", "ls-remote"])
        # fresh constraints still compiled via uv (not pip/pipx)
        assert _has(calls, lambda c: c[:3] == ["/fake/uv", "pip", "compile"])

    def test_clear_resets_when_not_installed(self, tmp_path):
        runner, fs, home = make_runner(
            tmp_path,
            settings={
                "reach_override_sha": "c" * 40,
                "reach_override_ack": "true",
                "reach_override_at": "2026-07-11T00:00:00+00:00",
            },
        )
        runner.clear_override()
        assert fs.get("reach_override_sha") is None
        assert fs.get("reach_override_ack") is None
        assert fs.get("reach_override_at") is None

    def test_status_reflects_active_override(self, tmp_path):
        override_sha = "c" * 40
        runner, fs, home = make_runner(
            tmp_path, settings={"reach_override_sha": override_sha, "reach_override_ack": "true"}
        )
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={"uv": "/fake/uv"}):
            st = runner.status()
        assert st["override"]["active"] is True
        assert st["authoritative_sha"] == override_sha
        assert st["pin_sha"] == PIN_SHA
        # agent-reach absent -> doctor is a clean None + note, not a crash
        assert st["doctor"] is None
        assert st["doctor_error"]

    def test_status_auto_clears_stale_override(self, tmp_path):
        # override SHA == shipped pin SHA -> auto-clear (spec Trust Model)
        runner, fs, home = make_runner(
            tmp_path, settings={"reach_override_sha": PIN_SHA, "reach_override_ack": "true"}
        )
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={}):
            st = runner.status()
        assert st["override"]["active"] is False
        assert fs.get("reach_override_sha") is None

    def test_override_ignored_without_ack_flag(self, tmp_path):
        # sha present but ack not truthy -> not active
        runner, fs, home = make_runner(
            tmp_path, settings={"reach_override_sha": "c" * 40, "reach_override_ack": "false"}
        )
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={}):
            st = runner.status()
        assert st["override"]["active"] is False
        assert st["authoritative_sha"] == PIN_SHA


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #

class TestEnableChannel:
    def test_unknown_channel_lists_valid_names(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with pytest.raises(RuntimeError) as exc:
            runner.enable_channel("nope")
        msg = str(exc.value)
        assert "opencli" in msg and "reddit" in msg and "twitter" in msg

    def test_requires_install_first(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)  # not installed
        with pytest.raises(RuntimeError, match="not installed"):
            runner.enable_channel("twitter")

    def test_pipx_channel_uses_uv(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        calls = []
        with patched(lambda cmd, **kw: (calls.append(list(cmd)), CP(cmd, 0, "", ""))[1]):
            hint = runner.enable_channel("twitter")
        assert ["/fake/uv", "tool", "install", "twitter-cli==1.2.3"] in calls
        assert hint
        state = json.loads((home / ".claude" / "jacked-reach-state.json").read_text())
        assert "twitter" in state["channels_enabled"]

    def test_npm_channel_uses_npm_and_skips_notes(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        calls = []
        with patched(lambda cmd, **kw: (calls.append(list(cmd)), CP(cmd, 0, "", ""))[1]):
            runner.enable_channel("opencli")
        assert ["/fake/npm", "install", "-g", "@jackwener/opencli@0.5.1"] in calls
        # note-only backend produced no command
        assert not _has(calls, lambda c: "chrome" in " ".join(c).lower())

    def test_pipx_git_channel_uses_uv_with_git_sha(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        calls = []
        with patched(lambda cmd, **kw: (calls.append(list(cmd)), CP(cmd, 0, "", ""))[1]):
            runner.enable_channel("reddit")
        assert [
            "/fake/uv",
            "tool",
            "install",
            f"git+https://github.com/public-clis/rdt-cli.git@{REDDIT_GIT_SHA}",
        ] in calls


# --------------------------------------------------------------------------- #
# status / remove
# --------------------------------------------------------------------------- #

class TestStatusRemove:
    def test_status_not_installed(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={}):
            st = runner.status()
        assert st["installed"] is False
        assert st["installed_sha"] is None
        assert st["drift"] == []
        assert st["sha_matches"] is False

    def test_status_doctor_json_passthrough(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        _place_skills(home, [Path(".claude") / "skills" / "agent-reach"])
        doctor_payload = {"channels": [{"name": "twitter", "active_backend": "twitter-cli", "status": "ok"}]}

        def side(cmd, **kw):
            if cmd[0] == "/fake/agent-reach" and "doctor" in cmd:
                return CP(cmd, 0, json.dumps(doctor_payload), "")
            return CP(cmd, 0, "", "")

        with patched(side):
            st = runner.status()
        assert st["installed"] is True
        assert st["sha_matches"] is True
        assert st["doctor"] == doctor_payload
        assert st["doctor_error"] is None
        assert all(d["status"] == "ok" for d in st["drift"])

    def test_status_doctor_absent_is_soft(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={"uv": "/fake/uv"}):
            st = runner.status()
        assert st["doctor"] is None
        assert "not found" in st["doctor_error"]

    def test_remove_is_tolerant_and_cleans_up(self, tmp_path):
        runner, fs, home = make_runner(
            tmp_path, settings={"reach_override_sha": "c" * 40, "reach_override_ack": "true"}
        )
        _mark_installed(home)
        calls = []
        with patched(lambda cmd, **kw: (calls.append(list(cmd)), CP(cmd, 0, "", ""))[1]):
            runner.remove()
        assert not (home / ".claude" / "jacked-reach-state.json").exists()
        assert _has(calls, lambda c: c == ["/fake/agent-reach", "uninstall"])
        assert _has(calls, lambda c: c[:3] == ["/fake/uv", "tool", "uninstall"])
        assert fs.get("reach_override_sha") is None

    def test_remove_tolerates_missing_binaries(self, tmp_path):
        runner, fs, home = make_runner(tmp_path)
        _mark_installed(home)
        with patched(lambda cmd, **kw: CP(cmd, 0, "", ""), which_map={}):
            runner.remove()  # no uv / agent-reach on PATH -> no crash
        assert not (home / ".claude" / "jacked-reach-state.json").exists()
