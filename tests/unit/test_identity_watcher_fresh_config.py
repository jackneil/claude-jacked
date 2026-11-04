"""The identity watcher waits out a fresh config write (#154i).

Claude Code writes the ``oauthAccount`` identity at ``/login`` a moment
before it saves the new tokens. A republish inside that window would put
the old account back into the config, and the stamped-rotation guard would
then read a stale verdict and undo the user's choice. The watcher skips a
pass while the config file is younger than ``CONFIG_FRESH_SECONDS`` and
decides on the next pass.
"""

from __future__ import annotations

import os
import time

from jacked.api import identity_watcher
from jacked.api.claude_config_store import claude_config_path
from tests.unit.test_identity_watcher import Harness, _config


def _write_config(home, age_seconds: float):
    path = claude_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_a_fresh_config_defers_the_republish(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="other@test.com"))
    _write_config(harness.home, age_seconds=1.0)

    evidence = harness.run()

    assert harness.writes == []
    assert evidence == ()
    assert harness.watcher.status()["accounts"]["7"]["reason"] == "config-fresh"


def test_an_old_config_is_repaired_as_before(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="other@test.com"))
    _write_config(harness.home, age_seconds=identity_watcher.CONFIG_FRESH_SECONDS + 60)

    evidence = harness.run()

    assert len(harness.writes) == 1
    assert evidence == ("claude-config:identity:republished",)


def test_no_config_file_is_not_fresh(tmp_path):
    assert identity_watcher._config_age_seconds(tmp_path) is None
