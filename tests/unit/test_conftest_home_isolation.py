"""No test reaches the developer's real ~/.claude (tests/conftest.py).

Import-time path constants resolve under an isolated HOME, the launch's
per-account tree sits under each test's HOME, and a credential write under
the real ~/.claude trips before it lands.
"""

import os
from pathlib import Path

import pytest

from jacked.api import credential_helpers
from tests.conftest import REAL_HOME


def test_import_time_constants_are_off_the_real_claude_dir():
    """The isolated HOME may itself sit under the real home (on Windows the
    temp directory is under the user profile), so the guard is the real
    ``~/.claude``, which no constant may resolve under."""
    import jacked.launch
    import jacked.service

    real_claude = REAL_HOME / ".claude"
    for value in (jacked.launch.ACCOUNTS_DIR, jacked.service.CLAUDE_DIR):
        assert real_claude not in Path(value).parents, value
        assert Path(value) != real_claude


def test_the_launch_accounts_dir_is_under_this_tests_home():
    import jacked.launch

    assert Path(os.environ["HOME"]) in jacked.launch.ACCOUNTS_DIR.parents


def test_a_credential_write_under_the_real_claude_dir_trips(tmp_path):
    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="real ~/.claude"):
        credential_helpers._safe_replace(
            str(src), str(REAL_HOME / ".claude" / "accounts" / "999" / ".credentials.json")
        )
    assert src.exists()  # nothing moved


def test_a_credential_write_under_a_temp_home_is_allowed(tmp_path):
    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    dst = tmp_path / ".claude" / "accounts" / "1" / ".credentials.json"
    dst.parent.mkdir(parents=True)
    credential_helpers._safe_replace(str(src), str(dst))
    assert dst.exists()


def test_the_launch_modules_own_writer_alias_is_guarded_too(tmp_path, monkeypatch):
    """launch.py binds _safe_replace by name at import; the guard must cover
    that alias, not only the credential_helpers attribute."""
    import jacked.launch
    from jacked.api import claude_config_store

    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    real_dst = str(REAL_HOME / ".claude" / "accounts" / "999" / ".credentials.json")
    for writer in (jacked.launch._safe_replace, claude_config_store._safe_replace):
        with pytest.raises(RuntimeError, match="real ~/.claude"):
            writer(str(src), real_dst)
    assert src.exists()
