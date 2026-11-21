"""No test reaches the developer's real ~/.claude (tests/conftest.py).

Import-time path constants resolve under an isolated HOME, the launch's
per-account tree sits under each test's HOME, and a file write under the
real ~/.claude (or to the real ~/.claude.json) trips before it lands.
"""

import os
from pathlib import Path

import pytest

from jacked import atomic_io
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


@pytest.mark.parametrize(
    "real_target",
    [
        REAL_HOME / ".claude" / "accounts" / "999" / ".credentials.json",
        REAL_HOME / ".claude.json",
    ],
)
def test_a_write_to_the_real_claude_files_trips(tmp_path, real_target):
    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="real ~/.claude"):
        atomic_io.replace_with_retry(str(src), str(real_target))
    assert src.exists()  # nothing moved


def test_a_credential_write_under_a_temp_home_is_allowed(tmp_path):
    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    dst = tmp_path / ".claude" / "accounts" / "1" / ".credentials.json"
    dst.parent.mkdir(parents=True)
    atomic_io.replace_with_retry(str(src), str(dst))
    assert dst.exists()


def test_every_name_bound_to_the_shared_replace_is_guarded(tmp_path):
    """Modules bind ``replace_with_retry`` by name at import. The guard sits
    on the primitive under it, so every bound name trips."""
    from jacked.api import claude_config_store
    from jacked.credentials import file_store

    src = tmp_path / "staged.json"
    src.write_text("{}", encoding="utf-8")
    real_dst = str(REAL_HOME / ".claude" / "accounts" / "999" / ".credentials.json")
    for writer in (claude_config_store.replace_with_retry, file_store.replace_with_retry):
        with pytest.raises(RuntimeError, match="real ~/.claude"):
            writer(str(src), real_dst)
    assert src.exists()


def test_the_real_home_is_not_a_test_home():
    """A pytest-xdist worker starts with the HOME that the controller already
    moved. REAL_HOME must still name the developer's home there, or every
    real-home tripwire above guards a temporary directory."""
    import conftest as root_conftest

    assert not any(p.name.startswith("jacked-tests-home-") for p in (REAL_HOME, *REAL_HOME.parents)), REAL_HOME
    assert REAL_HOME == root_conftest.REAL_HOME


_PROBE_REAL_HOME = '''
import os
from pathlib import Path

def test_real_home_in_this_process():
    from tests.conftest import REAL_HOME
    assert REAL_HOME == Path(os.environ["PROBE_EXPECTED_REAL_HOME"]), REAL_HOME
'''


@pytest.mark.parametrize("mode", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_worker_sees_the_home_the_run_started_with(tmp_path, mode):
    """A fresh run from a given HOME: the workers report that HOME as
    REAL_HOME, not the temporary home that the controller moved to."""
    from tests._nested_pytest import nested_pytest

    developer_home = tmp_path / "developer-home"
    developer_home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "JACKED_TESTS_REAL_HOME"}
    env.update(HOME=str(developer_home), USERPROFILE=str(developer_home),
               PROBE_EXPECTED_REAL_HOME=str(developer_home))
    proc = nested_pytest(tmp_path / "run", _PROBE_REAL_HOME, *mode, env=env)
    assert proc.returncode == pytest.ExitCode.OK, proc.stdout + proc.stderr
