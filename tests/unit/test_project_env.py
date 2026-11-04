"""Tests for the project env helpers in jacked.cli (.git/jacked/env).

The dashboard's /api/project/env routes pass a request value to both
helpers (CodeQL py/path-injection, #289i).
"""

import os

import pytest

from jacked.cli import _validate_env_path, _write_project_env
from tests._platform import requires_symlinks


def _env_dir(tmp_path):
    env = tmp_path / "envs" / "proj"
    env.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    return env


def test_a_real_env_is_valid(tmp_path):
    assert _validate_env_path(str(_env_dir(tmp_path))) is None


@pytest.mark.parametrize("bad", ["\n", "\r", "\t", "\x1b", "\x7f", "\x00"])
def test_a_control_character_is_refused(tmp_path, bad):
    """The hook reads only the first line of .git/jacked/env, so a line break
    in the value would let the hook use a path that was never validated."""
    env = _env_dir(tmp_path)
    assert _validate_env_path(str(env) + bad + "/x") == "Path contains invalid characters"


def test_write_records_the_env(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert _write_project_env(str(repo), "/opt/env") is True
    assert (repo / ".git" / "jacked" / "env").read_text(encoding="utf-8").strip() == "/opt/env"


def test_write_without_git_is_refused(tmp_path):
    assert _write_project_env(str(tmp_path), "/opt/env") is False
    assert not (tmp_path / ".git").exists()


@requires_symlinks
def test_write_replaces_a_linked_env_file_and_keeps_the_link_target(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "jacked").mkdir(parents=True)
    outside = tmp_path / "bashrc"
    outside.write_text("export SAFE=1\n", encoding="utf-8")
    (repo / ".git" / "jacked" / "env").symlink_to(outside)

    assert _write_project_env(str(repo), "/opt/env") is True

    assert outside.read_text(encoding="utf-8") == "export SAFE=1\n"
    env_file = repo / ".git" / "jacked" / "env"
    assert not env_file.is_symlink()
    assert env_file.read_text(encoding="utf-8").strip() == "/opt/env"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_new_env_file_is_readable_like_other_git_files(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write_project_env(str(repo), "/opt/env")
    assert (repo / ".git" / "jacked" / "env").stat().st_mode & 0o777 == 0o644
