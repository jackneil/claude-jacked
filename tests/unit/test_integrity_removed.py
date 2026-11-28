"""The hook and settings integrity tripwire (#99i) and the MEMORY.md quarantine are gone.

jacked 0.111.0 to 0.111.5 kept a baseline of settings.json in
``jacked/integrity.json`` beside the settings file, with the lock file
``integrity.json.lock``, and the MEMORY.md quarantine kept lock files in
``~/.claude/jacked/locks``. ``jacked install`` and ``jacked uninstall`` now
remove these files (``jacked.retired_integrity``). These tests also hold every
surface of the tripwire to its removal: the CLI command, the doctor line, the
dashboard field and panel, the rules bullet and the modules. Every test runs in
a scratch home.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from io import StringIO
from types import SimpleNamespace
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from jacked import retired_integrity
from tests._platform import symlinks_supported

REPO_ROOT = Path(__file__).resolve().parents[2]
NAMES = ("integrity.json", "integrity.json.lock")
OLD_BASELINE = {
    "version": 1,
    "baseline_time": "2026-11-21T12:00:00+00:00",
    "baseline": {"file": "sha256:0", "hooks": {}, "scripts": {}, "settings": {}},
    "log": [{"time": "2026-11-21T12:00:00+00:00", "reason": "first baseline", "version": "0.111.5"}],
}

requires_symlinks = pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="directory junctions exist only on Windows")


def _skip_or_fail_in_ci(reason: str):
    """Skip with ``reason``; on CI (CI true, 1 or yes), fail with one line instead.

    The junction tests are the only proof of the Windows rule, and CI runs
    pytest with no ``-rs``, so a skip on the windows-latest job would drop the
    proof with no sign. (The same switch as ``skip_or_fail_in_ci`` of the
    testdiet branch.)
    """
    if os.environ.get("CI", "").strip().lower() in ("true", "1", "yes"):
        pytest.fail(f"{reason}; on CI this test must run, so it fails instead of a skip", pytrace=False)
    pytest.skip(reason)


def _junction(link: Path, target: Path) -> None:
    """Make ``link`` a directory junction to ``target``, as ``jacked/launch.py`` does."""
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    except (ImportError, AttributeError, OSError):
        pass
    made = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if made.returncode != 0 or not os.path.lexists(link):
        _skip_or_fail_in_ci(f"could not make a directory junction: {made.stderr or made.stdout}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def _seed_leftovers(folder: Path) -> list[Path]:
    """Write the two files that an old jacked left in ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "integrity.json").write_text(json.dumps(OLD_BASELINE), encoding="utf-8")
    (folder / "integrity.json.lock").write_text("", encoding="utf-8")
    return [folder / name for name in NAMES]


def _report(home: Path) -> list[str]:
    """The lines that ``report_removal`` prints, as plain text."""
    out = StringIO()
    retired_integrity.report_removal(home, Console(file=out, width=60, highlight=False))
    return out.getvalue().splitlines()


def _install(monkeypatch, *extra: str):
    from jacked import cli

    monkeypatch.setattr(cli, "_setup_tray_autostart", lambda: None)
    return CliRunner().invoke(
        cli.main, ["install", "--no-rules", "--no-packs", "--no-tray", "--no-codex", *extra]
    )


def _removed_line(path: Path) -> str:
    return f"[OK] Removed {path} (a file of the retired settings integrity check)"


# --------------------------------------------------------------------------- #
# jacked install and jacked uninstall remove the old baseline files
# --------------------------------------------------------------------------- #

class TestInstallRemovesTheOldFiles:
    def test_install_removes_both_files_and_the_empty_folder(self, home, monkeypatch):
        folder = home / ".claude" / "jacked"
        files = _seed_leftovers(folder)

        r = _install(monkeypatch)

        assert r.exit_code == 0, r.output
        lines = r.output.splitlines()
        for path in files:
            assert not os.path.lexists(path)
            assert lines.count(_removed_line(path)) == 1, r.output
        assert not folder.exists()

    def test_install_json_removes_the_files_and_stdout_stays_one_json_record(self, home, monkeypatch):
        folder = home / ".claude" / "jacked"
        files = _seed_leftovers(folder)

        r = _install(monkeypatch, "--json")

        assert r.exit_code == 0, r.output
        assert isinstance(json.loads(r.output), dict), r.output
        assert not any(os.path.lexists(path) for path in files)
        assert not folder.exists()

    def test_install_on_a_clean_home_prints_no_integrity_line(self, home, monkeypatch):
        r = _install(monkeypatch)

        assert r.exit_code == 0, r.output
        assert "integrity" not in r.output.lower()
        assert not (home / ".claude" / "jacked").exists()

    def test_install_refuses_a_corrupt_settings_file_and_leaves_it_as_it_is(self, home, monkeypatch):
        from jacked import cli

        # A narrow console: the FAIL line carries a path and must never wrap
        # (soft_wrap), whatever width the suite's console happens to have.
        narrow = StringIO()
        monkeypatch.setattr(cli, "console", Console(file=narrow, width=60, highlight=False))
        path = home / ".claude" / "settings.json"
        path.write_text('{"hooks": {"Stop": [', encoding="utf-8")

        r = _install(monkeypatch)

        out = narrow.getvalue()
        assert r.exit_code == 1
        backup = next(path.parent.glob("settings.json.bak-*"))
        # The whole backup path sits on one line: a wrapped path cannot be copied.
        assert "FAIL" in out and str(backup) in out
        assert path.read_text(encoding="utf-8") == '{"hooks": {"Stop": ['

    def test_an_error_gives_one_warning_and_the_install_goes_on(self, home, monkeypatch):
        folder = home / ".claude" / "jacked"
        baseline, lock = _seed_leftovers(folder)
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if Path(path) == baseline:
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os, "unlink", unlink)

        r = _install(monkeypatch)

        assert r.exit_code == 0, r.output
        lines = r.output.splitlines()
        assert lines.count(
            f"[WARN] Could not remove {baseline} (PermissionError). Delete it yourself."
        ) == 1, r.output
        assert lines.count(_removed_line(lock)) == 1, r.output
        assert baseline.exists() and not lock.exists()
        assert folder.is_dir()  # the folder is not empty, so it stays
        assert (home / ".claude" / "settings.json").exists()  # the install finished

    def test_uninstall_removes_both_files(self, home, monkeypatch):
        from jacked import cli

        folder = home / ".claude" / "jacked"
        files = _seed_leftovers(folder)

        r = CliRunner().invoke(cli.main, ["uninstall", "--yes"])

        assert r.exit_code == 0, r.output
        lines = r.output.splitlines()
        for path in files:
            assert not os.path.lexists(path)
            assert lines.count(_removed_line(path)) == 1, r.output
        assert not folder.exists()

    def test_a_partial_uninstall_keeps_the_files(self, home, monkeypatch):
        """``--rules`` removes only the rules block: it does no other cleanup."""
        from jacked import cli

        files = _seed_leftovers(home / ".claude" / "jacked")

        r = CliRunner().invoke(cli.main, ["uninstall", "--rules"])

        assert r.exit_code == 0, r.output
        assert all(path.exists() for path in files)


class TestTheRemovalRules:
    def test_a_user_file_in_the_folder_stays_and_so_does_the_folder(self, home):
        folder = home / ".claude" / "jacked"
        files = _seed_leftovers(folder)
        (folder / "issue-queue").mkdir()
        mine = folder / "notes.txt"
        mine.write_text("mine", encoding="utf-8")

        lines = _report(home)

        assert lines == [_removed_line(path) for path in files]
        assert mine.read_text(encoding="utf-8") == "mine"
        assert (folder / "issue-queue").is_dir()

    def test_a_missing_file_gives_no_line(self, home):
        folder = home / ".claude" / "jacked"
        folder.mkdir()
        (folder / "integrity.json.lock").write_text("", encoding="utf-8")

        assert _report(home) == [_removed_line(folder / "integrity.json.lock")]
        assert _report(home) == []

    def test_an_empty_folder_that_held_no_old_file_stays(self, home):
        """The folder goes only when this cleanup emptied it."""
        folder = home / ".claude" / "jacked"
        folder.mkdir()

        assert _report(home) == []
        assert folder.is_dir()

    @requires_symlinks
    def test_a_link_named_integrity_json_is_not_followed(self, home, tmp_path):
        folder = home / ".claude" / "jacked"
        folder.mkdir()
        target = tmp_path / "elsewhere" / "important.json"
        target.parent.mkdir()
        target.write_text("keep me", encoding="utf-8")
        link = folder / "integrity.json"
        link.symlink_to(target)

        assert _report(home) == []
        assert target.read_text(encoding="utf-8") == "keep me"
        assert link.is_symlink()
        assert folder.is_dir()

    @requires_symlinks
    def test_a_linked_jacked_folder_is_left_alone(self, home, tmp_path):
        """Nothing inside a symlinked jacked folder is removed (DCR wave 1)."""
        real = tmp_path / "real-jacked"
        files = _seed_leftovers(real)
        (home / ".claude" / "jacked").symlink_to(real, target_is_directory=True)

        assert _report(home) == []
        assert (home / ".claude" / "jacked").is_symlink()
        assert all(path.exists() for path in files)

    def test_a_folder_with_the_name_of_a_file_stays(self, home):
        folder = home / ".claude" / "jacked"
        (folder / "integrity.json").mkdir(parents=True)
        (folder / "integrity.json" / "inside.txt").write_text("x", encoding="utf-8")

        assert _report(home) == []
        assert (folder / "integrity.json" / "inside.txt").read_text(encoding="utf-8") == "x"

    def test_claude_config_dir_is_cleaned_too(self, home, tmp_path, monkeypatch):
        config = tmp_path / "config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        in_home = _seed_leftovers(home / ".claude" / "jacked")
        in_config = _seed_leftovers(config / "jacked")

        lines = _report(home)

        assert lines == [_removed_line(path) for path in (*in_home, *in_config)]
        assert not (home / ".claude" / "jacked").exists()
        assert not (config / "jacked").exists()
        assert config.is_dir()

    def test_the_folders_have_no_duplicate(self):
        assert retired_integrity.leftover_folders("/h", {}) == [Path("/h/.claude/jacked")]
        assert retired_integrity.leftover_folders("/h", {"CLAUDE_CONFIG_DIR": "/h/.claude"}) == [
            Path("/h/.claude/jacked")
        ]

    @requires_symlinks
    def test_a_config_dir_that_resolves_to_the_home_folder_counts_once(self, home, tmp_path, monkeypatch):
        alias = tmp_path / "alias"
        alias.symlink_to(home / ".claude", target_is_directory=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alias))
        files = _seed_leftovers(home / ".claude" / "jacked")

        assert retired_integrity.leftover_folders(home) == [home / ".claude" / "jacked"]
        assert _report(home) == [_removed_line(path) for path in files]


# --------------------------------------------------------------------------- #
# The lock files of the retired MEMORY.md quarantine
# --------------------------------------------------------------------------- #

def _seed_locks(home: Path, count: int) -> Path:
    locks = home / ".claude" / "jacked" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        (locks / f"quarantine--Users-me-repo{n}.lock").write_text("", encoding="utf-8")
    return locks


def _locks_line(count: int, locks: Path) -> str:
    noun = "old quarantine lock file" if count == 1 else "old quarantine lock files"
    return f"[OK] Removed {count} {noun} from {locks}"


class TestQuarantineLocks:
    def test_the_lock_files_go_with_one_count_line_and_the_empty_folders_go(self, home):
        locks = _seed_locks(home, 3)

        assert _report(home) == [_locks_line(3, locks)]
        assert not locks.exists()
        assert not (home / ".claude" / "jacked").exists()

    def test_one_lock_file_gives_the_singular_line(self, home):
        locks = _seed_locks(home, 1)

        assert _report(home) == [_locks_line(1, locks)]

    def test_an_empty_locks_folder_goes(self, home):
        locks = home / ".claude" / "jacked" / "locks"
        locks.mkdir(parents=True)
        (home / ".claude" / "jacked" / "issue-queue").mkdir()

        assert _report(home) == []
        assert not locks.exists()
        assert (home / ".claude" / "jacked" / "issue-queue").is_dir()

    def test_a_file_with_another_name_stays_and_keeps_the_folder(self, home):
        locks = _seed_locks(home, 2)
        other = locks / "other.lock"
        other.write_text("mine", encoding="utf-8")
        near = locks / "quarantine-notes.txt"
        near.write_text("mine", encoding="utf-8")

        assert _report(home) == [_locks_line(2, locks)]
        assert other.read_text(encoding="utf-8") == "mine"
        assert near.read_text(encoding="utf-8") == "mine"
        assert sorted(p.name for p in locks.iterdir()) == ["other.lock", "quarantine-notes.txt"]

    @requires_symlinks
    def test_a_link_with_a_matching_name_stays(self, home, tmp_path):
        locks = _seed_locks(home, 0)
        target = tmp_path / "elsewhere.lock"
        target.write_text("keep me", encoding="utf-8")
        link = locks / "quarantine--Users-me-linked.lock"
        link.symlink_to(target)

        assert _report(home) == []
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "keep me"
        assert locks.is_dir()

    @requires_symlinks
    def test_a_linked_locks_folder_is_not_read(self, home, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "quarantine--x.lock"
        victim.write_text("keep me", encoding="utf-8")
        (home / ".claude" / "jacked").mkdir()
        (home / ".claude" / "jacked" / "locks").symlink_to(outside, target_is_directory=True)

        assert _report(home) == []
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_lock_files_in_both_folders_give_one_count_line(self, home, tmp_path, monkeypatch):
        config = tmp_path / "config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        in_home = _seed_locks(home, 2)
        in_config = config / "jacked" / "locks"
        in_config.mkdir(parents=True)
        (in_config / "quarantine--x.lock").write_text("", encoding="utf-8")

        assert _report(home) == [f"[OK] Removed 3 old quarantine lock files from {in_home} and {in_config}"]
        assert not (home / ".claude" / "jacked").exists()
        assert not (config / "jacked").exists()
        assert config.is_dir()

    def test_a_lock_error_gives_one_warning_line(self, home, monkeypatch):
        locks = _seed_locks(home, 3)
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if Path(path).parent == locks and not Path(path).name.endswith("0.lock"):
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os, "unlink", unlink)

        assert _report(home) == [
            _locks_line(1, locks),
            f"[WARN] Could not remove 2 old quarantine lock files in {locks} (PermissionError). "
            "Delete them yourself.",
        ]
        assert locks.is_dir()

    def test_install_removes_the_baseline_and_the_locks(self, home, monkeypatch):
        files = _seed_leftovers(home / ".claude" / "jacked")
        locks = _seed_locks(home, 4)

        r = _install(monkeypatch)

        assert r.exit_code == 0, r.output
        lines = r.output.splitlines()
        assert lines.count(_locks_line(4, locks)) == 1, r.output
        for path in files:
            assert lines.count(_removed_line(path)) == 1, r.output
        assert not (home / ".claude" / "jacked").exists()


# --------------------------------------------------------------------------- #
# No surface of the tripwire is left
# --------------------------------------------------------------------------- #

class TestNoSurfaceIsLeft:
    @pytest.mark.parametrize("module", [
        "jacked.integrity", "jacked.integrity_baseline", "jacked.integrity_snapshot",
        "jacked.integrity_command", "jacked.integrity_cli",
        "jacked.memory.quarantine", "jacked.memory.injection_shapes",
    ])
    def test_the_module_is_gone(self, module):
        assert importlib.util.find_spec(module) is None

    def test_the_integrity_command_is_gone(self, home):
        from jacked.cli import main

        r = CliRunner().invoke(main, ["integrity", "accept"])

        assert r.exit_code == 2
        assert "No such command 'integrity'" in r.output
        assert "integrity" not in CliRunner().invoke(main, ["--help"]).output

    def test_the_features_list_has_no_integrity_key(self, home, monkeypatch):
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from jacked.api.routes import features

        path = home / ".claude" / "settings.json"
        path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "echo x"}]},
        ]}}), encoding="utf-8")
        _seed_leftovers(home / ".claude" / "jacked")
        monkeypatch.setattr(features, "SETTINGS_JSON", path)
        app = FastAPI()
        app.include_router(features.router, prefix="/api")

        resp = TestClient(app).get("/api/features")

        assert resp.status_code == 200, resp.text
        assert "integrity" not in resp.json()

    def test_settings_js_has_no_integrity_panel(self):
        source = (REPO_ROOT / "jacked" / "data" / "web" / "js" / "components" / "settings.js").read_text(
            encoding="utf-8"
        )
        for text in (
            "_integrityPanel", "_INTEGRITY_SCOPE", "data-integrity-panel", "features.integrity",
            "jacked integrity accept", "Hook and settings integrity",
        ):
            assert text not in source, text

    def test_the_rules_have_no_quarantine_bullet(self):
        rules = (REPO_ROOT / "jacked" / "data" / "rules" / "jacked_behaviors.md").read_text(encoding="utf-8")

        assert "quarantine" not in rules
        assert "jacked integrity" not in rules
        assert "looks injected" not in rules



# --------------------------------------------------------------------------- #
# DCR wave 1: junctions, unreadable folders, a locks folder that goes mid-run
# --------------------------------------------------------------------------- #

def _fake_junction_lstat(monkeypatch, target: Path) -> None:
    """``os.lstat`` reports ``target`` the way Windows reports a directory junction."""
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(retired_integrity.os, "lstat", lstat)


def _spy_removals(monkeypatch) -> list[str]:
    """Record every ``os.unlink`` and ``os.rmdir`` call, then make it."""
    calls: list[str] = []
    real_unlink, real_rmdir = os.unlink, os.rmdir

    def unlink(path, *args, **kwargs):
        calls.append(f"unlink {path}")
        return real_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        calls.append(f"rmdir {path}")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(retired_integrity.os, "unlink", unlink)
    monkeypatch.setattr(retired_integrity.os, "rmdir", rmdir)
    return calls


class TestJunctionsAndReadErrors:
    def test_a_junction_like_jacked_folder_is_not_touched(self, home, monkeypatch):
        folder = home / ".claude" / "jacked"
        files = _seed_leftovers(folder)
        lock = _seed_locks(home, 1) / "quarantine--Users-me-repo0.lock"
        _fake_junction_lstat(monkeypatch, folder)
        calls = _spy_removals(monkeypatch)

        assert _report(home) == []
        assert calls == []
        assert all(path.exists() for path in files) and lock.exists()

    def test_a_junction_like_locks_folder_is_not_touched(self, home, monkeypatch):
        locks = _seed_locks(home, 1)
        lock = locks / "quarantine--Users-me-repo0.lock"
        _fake_junction_lstat(monkeypatch, locks)
        calls = _spy_removals(monkeypatch)

        assert _report(home) == []
        assert calls == []
        assert lock.exists()

    @windows_only
    def test_a_real_locks_junction_is_not_followed_or_removed(self, home, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "quarantine-x.lock"
        victim.write_text("keep me", encoding="utf-8")
        other = outside / "notes.txt"
        other.write_text("mine", encoding="utf-8")
        (home / ".claude" / "jacked").mkdir()
        locks = home / ".claude" / "jacked" / "locks"
        _junction(locks, outside)

        lines = _report(home)

        assert os.path.lexists(locks)
        assert victim.read_text(encoding="utf-8") == "keep me"
        assert other.read_text(encoding="utf-8") == "mine"
        assert not [line for line in lines if line.startswith("[OK]")], lines

    @windows_only
    def test_a_real_jacked_junction_is_not_followed_or_removed(self, home, tmp_path):
        outside = tmp_path / "outside-jacked"
        files = _seed_leftovers(outside)
        (outside / "issue-queue").mkdir()
        folder = home / ".claude" / "jacked"
        _junction(folder, outside)

        lines = _report(home)

        assert os.path.lexists(folder)
        assert all(path.exists() for path in files)
        assert (outside / "issue-queue").is_dir()
        assert not [line for line in lines if line.startswith("[OK]")], lines

    @pytest.mark.parametrize("which", ["jacked", "locks"])
    def test_a_folder_that_cannot_be_read_gives_one_warning(self, home, monkeypatch, which):
        folder = home / ".claude" / "jacked"
        locks = _seed_locks(home, 1)
        files = _seed_leftovers(folder) if which == "jacked" else []
        target = folder if which == "jacked" else locks
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os, "lstat", lstat)

        assert _report(home) == [
            f"[WARN] Could not read {target} (PermissionError). jacked did not remove the old files in it."
        ]
        assert (locks / "quarantine--Users-me-repo0.lock").exists()
        assert all(path.exists() for path in files)

    def test_a_locks_folder_that_cannot_be_listed_gives_one_warning(self, home, monkeypatch):
        locks = _seed_locks(home, 1)
        real_scandir = os.scandir

        def scandir(path=".", *args, **kwargs):
            if Path(path) == locks:
                raise OSError(5, "Input/output error")
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os, "scandir", scandir)

        assert _report(home) == [
            f"[WARN] Could not read {locks} (OSError). jacked did not remove the old files in it."
        ]
        assert (locks / "quarantine--Users-me-repo0.lock").exists()

    def test_a_locks_folder_that_another_install_removed_is_not_an_error(self, home, monkeypatch, caplog):
        """Install A sees locks/, install B removes it, then A lists it.

        The fake patches the global ``os.scandir``, and on Python 3.13
        ``Path.iterdir`` calls ``os.scandir``. So the fake empties the folder
        with the real ``os.listdir`` and acts only on its first call.
        """
        locks = _seed_locks(home, 2)
        real_scandir, real_listdir = os.scandir, os.listdir
        raced: list[Path] = []

        def scandir(path=".", *args, **kwargs):
            if not raced and Path(path) == locks:
                raced.append(locks)
                for name in real_listdir(locks):
                    (locks / name).unlink()
                locks.rmdir()  # the other install got there first
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os, "scandir", scandir)

        with caplog.at_level("WARNING", logger="jacked.retired_integrity"):
            assert _report(home) == []
        assert raced == [locks]
        assert caplog.records == []
        assert not locks.exists()

    @requires_symlinks
    def test_a_home_link_to_the_config_folder_still_lets_the_config_folder_be_cleaned(
        self, home, tmp_path, monkeypatch
    ):
        """DCR wave 2: the link must not take the real folder's place in the dedup."""
        config = tmp_path / "config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        files = _seed_leftovers(config / "jacked")
        link = home / ".claude" / "jacked"
        link.symlink_to(config / "jacked", target_is_directory=True)

        assert retired_integrity.leftover_folders(home) == [link, config / "jacked"]
        assert _report(home) == [_removed_line(path) for path in files]
        assert not any(os.path.lexists(path) for path in files)
        assert link.is_symlink()

    def test_a_home_junction_to_the_config_folder_still_lets_the_config_folder_be_cleaned(
        self, home, tmp_path, monkeypatch
    ):
        """The same with a faked junction: ``lstat`` gives a reparse-point folder,
        and ``realpath`` resolves it to the config folder, as on Windows."""
        config = tmp_path / "config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        files = _seed_leftovers(config / "jacked")
        junction = home / ".claude" / "jacked"
        junction.mkdir()
        _fake_junction_lstat(monkeypatch, junction)
        real_realpath = os.path.realpath

        def realpath(path, *args, **kwargs):
            if Path(path) == junction:
                return str(config / "jacked")
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(retired_integrity.os.path, "realpath", realpath)

        assert retired_integrity.leftover_folders(home) == [junction, config / "jacked"]
        assert _report(home) == [_removed_line(path) for path in files]
        assert junction.is_dir()
