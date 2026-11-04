"""The identity watcher waits out a /login, not any config write (#154i).

Claude Code writes the ``oauthAccount`` identity at ``/login`` a moment
before it saves the new tokens. A republish inside that window would put
the old account back into the config, and the stamped-rotation guard would
then read a stale verdict and undo the user's choice. The watcher skips a
pass only while the identity keys changed inside ``CONFIG_FRESH_SECONDS``.
Every running session also rewrites the file, so the mtime alone must not
starve the repair. A deferral spends no attempt floor and hides no sticky
reason.
"""

from __future__ import annotations

import os
import time

from jacked.api import identity_watcher
from jacked.api.authority_guard import ProfileLookup
from jacked.api.claude_config_store import claude_config_path
from tests.unit.test_identity_watcher import Harness, _config

OTHER = _config(emailAddress="other@test.com")


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


def test_continuous_unrelated_writes_do_not_starve_the_repair(tmp_path):
    harness = Harness(tmp_path, config=OTHER)
    _write_config(harness.home, age_seconds=1.0)
    assert harness.run() == ()
    assert harness.writes == []

    # Another session rewrites the file; the identity keys stay the same.
    harness.config = {**OTHER, "lastUsedAt": "display-field-changed"}
    harness.advance(identity_watcher.CONFIG_FRESH_SECONDS + 1)
    _write_config(harness.home, age_seconds=1.0)

    assert harness.run() == ("claude-config:identity:republished",)
    assert len(harness.writes) == 1


def test_a_deferral_does_not_spend_the_attempt_floor(tmp_path):
    harness = Harness(tmp_path, config=OTHER)
    _write_config(harness.home, age_seconds=1.0)

    harness.run()

    assert harness.watcher._states[7].last_attempt is None


def test_a_new_identity_inside_the_window_defers_again(tmp_path):
    harness = Harness(tmp_path, config=OTHER)
    _write_config(harness.home, age_seconds=1.0)
    harness.run()
    harness.advance(identity_watcher.CONFIG_FRESH_SECONDS + 1)

    harness.config = _config(emailAddress="third@test.com")
    _write_config(harness.home, age_seconds=1.0)
    harness.run()
    assert harness.writes == []
    assert harness.watcher.status()["accounts"]["7"]["reason"] == "config-fresh"

    harness.advance(identity_watcher.CONFIG_FRESH_SECONDS + 1)
    _write_config(harness.home, age_seconds=1.0)
    harness.run()
    assert len(harness.writes) == 1


def test_a_login_after_an_in_sync_pass_is_seen_as_a_change(tmp_path):
    """The in-sync passes keep the baseline: a /login back to an identity an
    earlier pass repaired is a change, not the old value seen again."""
    harness = Harness(tmp_path, config=OTHER)
    _write_config(harness.home, age_seconds=identity_watcher.CONFIG_FRESH_SECONDS + 60)
    harness.run()
    assert len(harness.writes) == 1

    harness.config = _config()
    harness.advance(identity_watcher.ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    harness.run()
    harness.config = OTHER
    harness.advance(identity_watcher.ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    _write_config(harness.home, age_seconds=1.0)

    assert harness.run() == ()
    assert len(harness.writes) == 1
    assert harness.watcher.status()["accounts"]["7"]["reason"] == "config-fresh"


def test_a_change_in_a_file_nobody_wrote_inside_the_window_is_not_deferred(tmp_path):
    harness = Harness(tmp_path, config=_config())
    _write_config(harness.home, age_seconds=identity_watcher.CONFIG_FRESH_SECONDS + 60)
    harness.run()
    harness.config = OTHER
    harness.advance(identity_watcher.ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert harness.run() == ("claude-config:identity:republished",)


def test_a_deferral_keeps_the_sticky_evidence_of_an_earlier_check(tmp_path):
    harness = Harness(tmp_path, config=OTHER)
    harness.lookup_result = ProfileLookup(401)
    _write_config(harness.home, age_seconds=identity_watcher.CONFIG_FRESH_SECONDS + 60)
    assert harness.run() == ("claude-config:identity:owner-unproven",)

    harness.config = _config(emailAddress="third@test.com")
    harness.advance(identity_watcher.ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    _write_config(harness.home, age_seconds=1.0)

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    assert harness.watcher.status()["accounts"]["7"]["reason"] == "owner-unproven"
