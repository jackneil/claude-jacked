"""Tests for the update-status JSON reader/writer."""

import os

import pytest
import time
from pathlib import Path


def test_init_creates_file_with_metadata(tmp_path):
    from jacked.service.update_status import init_status, read_status
    p = tmp_path / "status.json"
    init_status(p, from_version="0.41.18", to_version="0.41.19", method="uv")
    data = read_status(p)
    assert data["from_version"] == "0.41.18"
    assert data["to_version"] == "0.41.19"
    assert data["method"] == "uv"
    assert data["overall"] == "in_progress"
    assert data["phases"] == []
    assert "started_at" in data


def test_begin_phase_appends_entry(tmp_path):
    from jacked.service.update_status import init_status, begin_phase, read_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    data = read_status(p)
    assert len(data["phases"]) == 1
    assert data["phases"][0]["name"] == "installing_package"
    assert data["phases"][0]["status"] == "in_progress"
    assert data["current_phase"] == "installing_package"


def test_end_phase_ok(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(p, "installing_package", status="ok")
    data = read_status(p)
    assert data["phases"][0]["status"] == "ok"


def test_end_phase_failure_sets_overall(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(
        p, "installing_package", status="failed",
        error="uv tool install failed", recovery="Re-run: uv tool install ...",
    )
    data = read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "uv tool install failed"


def test_end_phase_raises_on_unknown_phase(tmp_path):
    from jacked.service.update_status import init_status, end_phase
    import pytest
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    with pytest.raises(ValueError):
        end_phase(p, "nonexistent_phase", status="ok")


def test_mark_succeeded_finalizes_overall(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_succeeded, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    data = read_status(p)
    assert data["overall"] == "succeeded"


def test_clear_status_removes_file(tmp_path):
    from jacked.service.update_status import (
        init_status, clear_status, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    assert p.exists()
    clear_status(p)
    assert not p.exists()
    assert read_status(p) is None


def test_clear_status_missing_is_noop(tmp_path):
    from jacked.service.update_status import clear_status
    clear_status(tmp_path / "nope.json")


def test_read_missing_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    assert read_status(tmp_path / "does-not-exist.json") is None


def test_read_corrupt_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    p = tmp_path / "status.json"
    p.write_text("{not json at all")
    assert read_status(p) is None


def test_read_stale_succeeded_returns_none(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_succeeded, read_status,
        STALE_SUCCEEDED_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    old = time.time() - STALE_SUCCEEDED_SECONDS - 10
    os.utime(p, (old, old))
    assert read_status(p) is None


def test_read_with_mtime_returns_iso_timestamp(tmp_path):
    from jacked.service.update_status import read_status_with_mtime, init_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    data, mtime_iso = read_status_with_mtime(p)
    assert data is not None
    assert mtime_iso is not None
    assert "T" in mtime_iso


def test_write_is_atomic_no_tmp_leftover(tmp_path):
    from jacked.service.update_status import init_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    siblings = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert siblings == []


def test_lock_rejects_second_init_if_another_active(tmp_path):
    from jacked.service.update_status import init_status, LockBusy
    import pytest
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    with pytest.raises(LockBusy):
        init_status(p, from_version="a", to_version="b", method="uv")


def test_lock_allows_init_after_previous_succeeded(tmp_path):
    from jacked.service.update_status import init_status, mark_succeeded
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    init_status(p, from_version="b", to_version="c", method="uv")


def test_lock_allows_init_after_stale_in_progress(tmp_path):
    from jacked.service.update_status import (
        init_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 10
    os.utime(p, (old, old))
    init_status(p, from_version="b", to_version="c", method="uv")


def test_init_or_adopt_fresh_file_initializes(tmp_path):
    from jacked.service.update_status import init_or_adopt_status, read_status
    p = tmp_path / "status.json"
    outcome = init_or_adopt_status(p, from_version="a", to_version="b", method="uv")
    assert outcome == "initialized"
    assert read_status(p)["overall"] == "in_progress"


def test_init_or_adopt_over_tray_pre_init_adopts_and_preserves_metadata(tmp_path):
    from jacked.service.update_status import (
        init_or_adopt_status,
        init_status,
        read_status,
    )
    p = tmp_path / "status.json"
    # Tray pre-init writes the real from/to metadata, marked as pre-init.
    init_status(p, from_version="0.41.19", to_version="0.41.20", method="uv", preinit=True)
    # Detached updater races in moments later with a placeholder target.
    outcome = init_or_adopt_status(p, from_version="0.41.19", to_version="next", method="uv")
    assert outcome == "adopted"
    data = read_status(p)
    # The tray's metadata must survive — no rewrite on adopt.
    assert data["from_version"] == "0.41.19"
    assert data["to_version"] == "0.41.20"


def test_init_or_adopt_over_open_phase_raises_lockbusy(tmp_path):
    from jacked.service.update_status import (
        init_or_adopt_status, init_status, begin_phase, LockBusy,
    )
    import pytest
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    with pytest.raises(LockBusy):
        init_or_adopt_status(p, from_version="a", to_version="b", method="uv")


def test_init_or_adopt_over_stale_in_progress_initializes(tmp_path):
    from jacked.service.update_status import (
        init_or_adopt_status, init_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 10
    os.utime(p, (old, old))
    outcome = init_or_adopt_status(p, from_version="b", to_version="c", method="uv")
    assert outcome == "initialized"


def test_read_stale_in_progress_returns_none(tmp_path):
    """An old record whose updater is gone (or unknown) is a zombie."""
    from jacked.service.update_status import (
        NO_UPDATER_PID, init_status, read_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv", updater_pid=NO_UPDATER_PID)
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 10
    os.utime(p, (old, old))
    assert read_status(p) is None


def test_an_old_record_of_a_live_updater_stays_visible(tmp_path):
    """The skill-pack refresh may run 15 minutes without writing its record.
    While the updater runs, age alone must not hide the update (the page
    would report it finished and stop polling)."""
    from jacked.service.update_status import (
        abandoned_status, init_status, read_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")  # this live process
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 600
    os.utime(p, (old, old))
    assert read_status(p) is not None
    assert abandoned_status(p) is None


def test_the_api_reports_whether_the_updater_runs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jacked.api.main import app as _app
    from jacked.service import update_status as us_mod

    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    assert TestClient(_app).get("/api/update/status").json()["updater_alive"] is True


def test_read_fresh_in_progress_returns_data(tmp_path):
    from jacked.service.update_status import init_status, read_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    data = read_status(p)
    assert data is not None
    assert data["overall"] == "in_progress"


def test_api_endpoint_returns_null_when_no_status_file(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jacked.api.main import app as _app
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "nope.json")
    client = TestClient(_app)
    r = client.get("/api/update/status")
    assert r.status_code == 200
    assert r.json() == {"status": None, "mtime_iso": None, "updater_alive": None}


def test_api_endpoint_returns_status_content_with_mtime(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jacked.api.main import app as _app
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    client = TestClient(_app)
    r = client.get("/api/update/status")
    body = r.json()
    assert body["status"]["current_phase"] == "installing_package"
    assert body["mtime_iso"] is not None


def test_cli_update_status_init(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    result = CliRunner().invoke(
        main, ["_update_status_init", "0.41.18", "0.41.19", "uv"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(tmp_path / "status.json")
    assert data["from_version"] == "0.41.18"
    assert data["to_version"] == "0.41.19"
    assert data["method"] == "uv"


def test_cli_update_status_init_accepts_log_path(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    CliRunner().invoke(
        main,
        ["_update_status_init", "a", "b", "uv", "--log-path", "/tmp/foo.log"],
    )
    data = us_mod.read_status(tmp_path / "status.json")
    assert data["log_path"] == "/tmp/foo.log"


def test_cli_update_status_init_exits_2_when_phase_open(tmp_path, monkeypatch):
    """A REAL updater in flight (phase open) must abort the batch (exit 2)."""
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")  # a genuinely-active updater
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(main, ["_update_status_init", "a", "b", "uv"])
    assert result.exit_code == 2


def test_cli_update_status_init_exits_0_on_tray_pre_init(tmp_path, monkeypatch):
    """The tray pre-inits the file (no phases); the batch's own init must
    adopt it and exit 0 rather than deadlocking on its own breadcrumb."""
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv", preinit=True)  # tray pre-init
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(main, ["_update_status_init", "a", "b", "uv"])
    assert result.exit_code == 0
    assert "adopted" in result.output


def test_cli_update_status_begin(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main, ["_update_status", "installing_package", "in_progress"],
    )
    assert result.exit_code == 0
    assert us_mod.read_status(p)["current_phase"] == "installing_package"


def test_cli_update_status_end_ok(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main, ["_update_status", "installing_package", "ok"],
    )
    assert result.exit_code == 0
    assert us_mod.read_status(p)["phases"][0]["status"] == "ok"


def test_cli_update_status_failed_with_error(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main,
        ["_update_status", "installing_package", "failed",
         "--error", "upgrade failed",
         "--recovery", "retry command"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "upgrade failed"


def test_cli_update_status_succeed(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(main, ["_update_status_succeed"])
    assert result.exit_code == 0
    assert us_mod.read_status(p)["overall"] == "succeeded"


def test_update_html_is_served_as_itself_not_spa_rewritten():
    """The SPA fallback serves index.html for unmatched paths. The .html
    suffix makes /update.html hit the file branch. Regression-guard."""
    from fastapi.testclient import TestClient
    from jacked.api.main import app as _app
    client = TestClient(_app)
    r = client.get("/update.html")
    assert r.status_code == 200
    assert "Jacked is updating" in r.text
    assert "waiting_for_parent" in r.text


def test_mark_failed_sets_overall_with_error_and_recovery(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_failed, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_failed(p, error="uv not on PATH",
                recovery="Install uv from https://docs.astral.sh/uv/")
    data = read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "uv not on PATH"
    assert data["recovery"] == "Install uv from https://docs.astral.sh/uv/"


def test_mark_failed_preserves_existing_phases(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, mark_failed, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(p, "installing_package", status="ok")
    mark_failed(p, error="downstream step errored", recovery="")
    data = read_status(p)
    assert data["overall"] == "failed"
    assert data["phases"][0]["status"] == "ok"


def test_mark_failed_on_missing_file_is_noop(tmp_path):
    from jacked.service.update_status import mark_failed, read_status
    mark_failed(tmp_path / "nope.json", error="x", recovery="y")
    assert read_status(tmp_path / "nope.json") is None


def test_cli_update_status_exits_1_on_unknown_phase(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(main, ["_update_status", "nonexistent_phase", "ok"])
    assert result.exit_code == 1


class TestAbandonedStatus:
    """A crash mid-update must be reported, not just hidden."""

    @staticmethod
    def _write(path, overall, phase, age_seconds):
        import json
        import os
        import time

        path.write_text(
            json.dumps(
                {
                    "overall": overall,
                    "current_phase": phase,
                    "from_version": "0.95.0",
                    "to_version": "0.100.0",
                    "phases": [],
                }
            ),
            encoding="utf-8",
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_a_stale_in_progress_file_is_reported_as_abandoned(self, tmp_path):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, "in_progress", "migrating_settings",
                    us.STALE_IN_PROGRESS_SECONDS + 60)

        # read_status hides it so the dashboard shows no zombie banner...
        assert us.read_status(path) is None
        # ...but the raw record is still available to warn the user.
        record = us.abandoned_status(path)
        assert record is not None
        assert record["current_phase"] == "migrating_settings"

    def test_a_fresh_in_progress_file_is_not_abandoned(self, tmp_path):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, "in_progress", "installing_package", 5)
        assert us.abandoned_status(path) is None

    def test_a_finished_update_is_never_abandoned(self, tmp_path):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, "failed", None, us.STALE_IN_PROGRESS_SECONDS + 60)
        assert us.abandoned_status(path) is None

    def test_a_missing_file_is_never_abandoned(self, tmp_path):
        from jacked.service import update_status as us

        assert us.abandoned_status(tmp_path / "nope.json") is None


class TestUpdaterPidLiveness:
    """A live updater is never abandoned, however long its phase runs.

    STALE_IN_PROGRESS_SECONDS is a heartbeat rule, not a duration limit. One
    phase (a package install on a slow link) can legitimately run longer, and
    a supervisor that relaunches the tray mid-update then read the LIVE update
    as abandoned, marked it failed and wrote a false recovery file.
    """

    @staticmethod
    def _write(path, pid, age_seconds):
        import json
        import os
        import time

        record = {
            "overall": "in_progress",
            "current_phase": "installing_package",
            "phases": [],
        }
        if pid is not None:
            record["updater_pid"] = pid
        path.write_text(json.dumps(record), encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_init_status_records_the_updater_pid(self, tmp_path):
        import json

        from jacked.service.update_status import init_status

        path = tmp_path / "status.json"
        init_status(path, "1.0.0", "1.1.0", "uv")
        assert json.loads(path.read_text())["updater_pid"] == os.getpid()

    def test_init_status_accepts_an_explicit_updater_pid(self, tmp_path):
        import json

        from jacked.service.update_status import init_status

        path = tmp_path / "status.json"
        init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=4242)
        assert json.loads(path.read_text())["updater_pid"] == 4242

    def test_a_stale_record_whose_updater_still_runs_is_not_abandoned(
        self, tmp_path
    ):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, os.getpid(), us.STALE_IN_PROGRESS_SECONDS + 600)
        assert us.abandoned_status(path) is None

    def test_a_stale_record_whose_updater_is_gone_is_abandoned(self, tmp_path):
        import subprocess
        import sys

        from jacked.service import update_status as us

        # A real, definitely-dead pid: run a child to completion and reuse it.
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        path = tmp_path / "status.json"
        self._write(path, dead.pid, us.STALE_IN_PROGRESS_SECONDS + 60)
        record = us.abandoned_status(path)
        assert record is not None
        assert record["current_phase"] == "installing_package"

    def test_a_record_without_a_pid_keeps_the_mtime_rule(self, tmp_path):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, None, us.STALE_IN_PROGRESS_SECONDS + 60)
        assert us.abandoned_status(path) is not None

    def test_a_garbage_pid_keeps_the_mtime_rule(self, tmp_path):
        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        self._write(path, "not-a-pid", us.STALE_IN_PROGRESS_SECONDS + 60)
        assert us.abandoned_status(path) is not None

    def test_adopting_the_tray_pre_init_claims_the_pid(self, tmp_path):
        import json

        from jacked.service.update_status import init_or_adopt_status

        path = tmp_path / "status.json"
        # The tray pre-creates the file, then exits; the detached updater
        # adopts it. A record still naming the dead tray would read as
        # abandoned the moment the update outran the mtime rule.
        from jacked.service.update_status import init_status

        init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=999999, preinit=True)
        assert init_or_adopt_status(path, "1.0.0", "1.1.0", "uv") == "adopted"
        data = json.loads(path.read_text())
        assert data["updater_pid"] == os.getpid()
        assert data["from_version"] == "1.0.0"


class TestExclusiveUpdateLock:
    """Two upgrades that start in the same second must not both proceed.

    `init_or_adopt_status` is a read-check-write on mtime, so it cannot
    serialize them by itself. `acquire_update_lock` is a real OS lock.
    """

    def test_the_lock_is_granted_when_nothing_holds_it(self, tmp_path):
        from jacked.service.update_status import acquire_update_lock

        handle = acquire_update_lock(tmp_path / "status.json")
        assert handle is not None
        handle.close()

    def test_the_lock_is_reusable_after_release(self, tmp_path):
        from jacked.service.update_status import acquire_update_lock

        path = tmp_path / "status.json"
        first = acquire_update_lock(path)
        assert first is not None
        first.close()
        second = acquire_update_lock(path)
        assert second is not None
        second.close()

    def test_the_lock_does_not_require_an_existing_status_file(self, tmp_path):
        from jacked.service.update_status import acquire_update_lock

        path = tmp_path / "nested" / "status.json"
        handle = acquire_update_lock(path)
        assert handle is not None
        handle.close()
        assert not path.exists()

    def test_another_process_holding_the_lock_refuses_this_one(self, tmp_path):
        """The real thing: a separate process, a real cross-process lock."""
        import subprocess
        import sys

        from jacked.service.update_status import acquire_update_lock

        path = tmp_path / "status.json"
        holder_script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from jacked.service.update_status import acquire_update_lock\n"
            "handle = acquire_update_lock(Path(%r))\n"
            "print('HELD' if handle is not None else 'REFUSED', flush=True)\n"
            "sys.stdin.readline()\n"
        ) % (str(Path(__file__).resolve().parents[3]), str(path))

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "HELD"
            assert acquire_update_lock(path) is None, (
                "a second updater took a lock another process holds"
            )
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)
        # Released with the holder: the next updater may proceed.
        handle = acquire_update_lock(path)
        assert handle is not None
        handle.close()


@pytest.mark.parametrize("pid", [2**31, 2**40, 2**70, True])
def test_an_oversized_or_bogus_updater_pid_never_raises(tmp_path, pid):
    """os.kill raises OverflowError (not OSError) above pid_t; a probe must not."""
    import json
    import os
    import time

    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    path.write_text(json.dumps({"overall": "in_progress", "current_phase": "preflight", "updater_pid": pid}))
    old = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
    os.utime(path, (old, old))
    record = us.abandoned_status(path)
    assert record is not None and record["current_phase"] == "preflight"


def test_process_liveness_is_false_for_out_of_range_pids():
    from jacked.service.process import is_process_alive, process_liveness

    for pid in (2**31, 2**40, -5, 0):
        assert process_liveness(pid) is False
        assert is_process_alive(pid) is False


def test_no_updater_pid_sentinel_records_no_pid_and_uses_the_mtime_rule(tmp_path):
    import json
    import os
    import time

    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    us.init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=us.NO_UPDATER_PID)
    assert "updater_pid" not in json.loads(path.read_text()) or json.loads(path.read_text())["updater_pid"] is None
    us.begin_phase(path, "installing_package")
    old = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
    os.utime(path, (old, old))
    assert us.abandoned_status(path) is not None


def test_adoption_replaces_the_dead_tray_pid_with_no_pid_for_a_batch(tmp_path):
    import json

    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    us.init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=99999999, preinit=True)  # tray pre-init
    assert us.init_or_adopt_status(path, "1.0.0", "1.1.0", "uv", updater_pid=us.NO_UPDATER_PID) == "adopted"
    assert json.loads(path.read_text()).get("updater_pid") is None


def test_adoption_that_cannot_write_its_claim_is_refused(tmp_path, monkeypatch):
    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    us.init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=99999999, preinit=True)

    def _boom(p, data):
        raise OSError("disk full")

    monkeypatch.setattr(us, "_atomic_write", _boom)
    with pytest.raises(us.LockBusy, match="could not claim"):
        us.init_or_adopt_status(path, "1.0.0", "1.1.0", "uv")


def test_indeterminate_liveness_falls_back_to_the_mtime_rule(tmp_path, monkeypatch):
    import json
    import os
    import time

    from jacked.service import update_status as us

    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: None)
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"overall": "in_progress", "current_phase": "preflight", "updater_pid": 4242}))
    old = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
    os.utime(path, (old, old))
    assert us.abandoned_status(path) is not None
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: True)
    assert us.abandoned_status(path) is None


def test_only_a_tray_preinit_record_can_be_adopted(tmp_path):
    """Two fresh updaters must not share one record: adoption needs the marker."""
    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    # A record written by ANOTHER updater (no phases yet, no preinit marker).
    us.init_status(path, "1.0.0", "1.1.0", "uv")
    with pytest.raises(us.LockBusy):
        us.init_or_adopt_status(path, "1.0.0", "1.1.0", "uv")


def test_the_preinit_marker_is_consumed_on_adoption(tmp_path):
    import json

    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    us.init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=99999999, preinit=True)
    assert us.init_or_adopt_status(path, "1.0.0", "1.1.0", "uv") == "adopted"
    assert "preinit" not in json.loads(path.read_text())
    # A third arrival sees a claimed record and is refused.
    with pytest.raises(us.LockBusy):
        us.init_or_adopt_status(path, "1.0.0", "1.1.0", "uv")


def test_update_status_doctests_pass():
    """The module's doctests document adoption; keep them executable."""
    import doctest

    from jacked.service import update_status

    results = doctest.testmod(update_status)
    assert results.failed == 0, results


def test_init_status_clobbers_a_record_whose_updater_is_proven_dead(tmp_path, monkeypatch):
    """The write side agrees with the read side on liveness."""
    from jacked.service import update_status as us

    path = tmp_path / "status.json"
    us.init_status(path, "1.0.0", "1.1.0", "uv", updater_pid=4242)
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: False)
    us.init_status(path, "1.0.0", "1.2.0", "uv")  # no LockBusy
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: True)
    with pytest.raises(us.LockBusy):
        us.init_status(path, "1.0.0", "1.3.0", "uv")
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: None)
    with pytest.raises(us.LockBusy):  # indeterminate keeps the mtime rule
        us.init_status(path, "1.0.0", "1.4.0", "uv")



def test_a_reused_pid_does_not_keep_an_update_alive(tmp_path, monkeypatch):
    """The updater exits and an unrelated process takes its pid: same pid,
    different creation identity, so the record is not proven alive."""
    from jacked.service import update_status as us

    p = tmp_path / "status.json"
    us.init_status(p, from_version="a", to_version="b", method="uv")
    data = us.read_status(p)
    assert isinstance(data.get("updater_creation_id"), str)
    assert us.updater_liveness(data) is True
    monkeypatch.setattr(us, "_creation_id", lambda pid: "another-process")
    assert us.updater_liveness(data) is False
    monkeypatch.setattr(us, "_creation_id", lambda pid: None)
    assert us.updater_liveness(data) is None  # cannot tell: the mtime rule applies
    legacy = {k: v for k, v in data.items() if k != "updater_creation_id"}
    assert us.updater_liveness(legacy) is True  # a record from before the field


# ---------------------------------------------------------------------------
# #151i: a completed install is proof the update finished
# ---------------------------------------------------------------------------


def _iso(when):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(when, tz=timezone.utc).isoformat()


_STARTED = "2026-11-02T10:00:00.000000+00:00"
_LATER = "2026-11-02T10:00:25.000000+00:00"


def _record(**overrides):
    record = {
        "overall": "in_progress",
        "started_at": _STARTED,
        "from_version": "0.110.1",
        "to_version": "0.110.2",
    }
    record.update(overrides)
    return record


def _last(at=_LATER, to_version="0.110.2"):
    return {"at": at, "from_version": "0.110.1", "to_version": to_version}


class TestInstallCompletedAfter:
    """The last-install record and the running version are independent proof
    that an update's install finished, whatever its status record says."""

    def test_an_install_after_the_start_at_the_running_version_completed(self):
        from jacked.service import update_status as us

        assert us.install_completed_after(_record(), _last(), "0.110.2") is True

    def test_an_install_newer_than_the_running_build_also_counts(self):
        from jacked.service import update_status as us

        assert us.install_completed_after(
            _record(), _last(to_version="0.110.3"), "0.110.2"
        ) is True

    @pytest.mark.parametrize(
        "at", ["2026-11-02T09:59:59.000000+00:00", _STARTED],
        ids=["before-the-start", "at-the-start"],
    )
    def test_an_install_that_is_not_later_than_the_start_proves_nothing(self, at):
        from jacked.service import update_status as us

        assert us.install_completed_after(_record(), _last(at=at), "0.110.2") is False

    def test_no_last_install_proves_nothing(self):
        from jacked.service import update_status as us

        assert us.install_completed_after(_record(), None, "0.110.2") is False

    def test_an_install_older_than_the_running_build_proves_nothing(self):
        from jacked.service import update_status as us

        assert us.install_completed_after(
            _record(from_version="0.110.0"), _last(to_version="0.110.1"), "0.110.2"
        ) is False

    def test_an_install_that_is_not_newer_than_the_from_version_proves_nothing(self):
        from jacked.service import update_status as us

        assert us.install_completed_after(
            _record(from_version="0.110.2"), _last(to_version="0.110.2"), "0.110.2"
        ) is False

    def test_the_build_the_update_started_from_still_running_proves_nothing(self):
        """Reviewer D: an update from 0.110.2 to 0.110.3 and a later install
        of 0.110.3, while 0.110.2 still runs. The install did not reach this
        process, so this process cannot call the update complete."""
        from jacked.service import update_status as us

        record = _record(from_version="0.110.2", to_version="0.110.3")
        assert us.install_completed_after(
            record, _last(to_version="0.110.3"), "0.110.2"
        ) is False
        # An older build than the update started from is not newer either.
        assert us.install_completed_after(
            record, _last(to_version="0.110.3"), "0.110.1"
        ) is False

    def test_versions_compare_by_pep_440_not_as_text(self):
        from jacked.service import update_status as us

        # 0.99.0 sorts after 0.110.2 as text, before it by PEP 440.
        assert us.install_completed_after(
            _record(from_version="0.99.0"), _last(), "0.110.2"
        ) is True
        # A dev build sorts before its release.
        assert us.install_completed_after(
            _record(), _last(to_version="0.110.2.dev3"), "0.110.2"
        ) is False
        # A release candidate sorts before its release.
        assert us.install_completed_after(
            _record(from_version="0.110.2rc1"), _last(), "0.110.2"
        ) is True

    def test_timestamps_compare_as_instants_across_offsets(self):
        from jacked.service import update_status as us

        # 06:00:25-04:00 is 10:00:25 UTC: 25 s after the start.
        assert us.install_completed_after(
            _record(), _last(at="2026-11-02T06:00:25-04:00"), "0.110.2"
        ) is True

    @pytest.mark.parametrize(
        "record,last,running",
        [
            (_record(from_version="not a version"), _last(), "0.110.2"),
            (_record(from_version=None), _last(), "0.110.2"),
            (_record(), _last(to_version=None), "0.110.2"),
            (_record(), _last(to_version="0.110"), "garbage!"),
            (_record(started_at="yesterday"), _last(), "0.110.2"),
            (_record(started_at=None), _last(), "0.110.2"),
            (_record(), _last(at=12345), "0.110.2"),
            (_record(), _last(at="2026-11-02T10:00:25"), "0.110.2"),
            (_record(), {"to_version": "0.110.2"}, "0.110.2"),
            (_record(), ["not", "a", "record"], "0.110.2"),
            (["not", "a", "record"], _last(), "0.110.2"),
            (_record(), _last(), None),
        ],
        ids=[
            "bad-from-version",
            "no-from-version",
            "no-to-version",
            "bad-running-version",
            "bad-started-at",
            "no-started-at",
            "numeric-at",
            "naive-at",
            "no-at",
            "last-install-not-an-object",
            "record-not-an-object",
            "no-running-version",
        ],
    )
    def test_malformed_input_proves_nothing_with_one_debug_line(
        self, caplog, record, last, running
    ):
        import logging

        from jacked.service import update_status as us

        with caplog.at_level(logging.DEBUG, logger="jacked.service.update_status"):
            assert us.install_completed_after(record, last, running) is False
        lines = [
            r for r in caplog.records
            if r.name == "jacked.service.update_status" and r.levelno == logging.DEBUG
        ]
        assert len(lines) == 1, [r.getMessage() for r in lines]


class TestAbandonedStatusSeesACompletedInstall:
    """A stale in_progress record whose install completed is closed as
    succeeded: the batch or the updater died after the install, not during it.
    """

    @staticmethod
    def _write(path, started, age_seconds):
        import json

        path.write_text(
            json.dumps(_record(started_at=_iso(started), current_phase="restarting_service",
                               phases=[])),
            encoding="utf-8",
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_a_completed_install_closes_the_record_as_succeeded(self, tmp_path):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        self._write(path, started, us.STALE_IN_PROGRESS_SECONDS + 60)
        last = _last(at=_iso(started + 25))

        assert us.abandoned_status(
            path, last_install=last, running_version="0.110.2"
        ) is None
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "succeeded"

    @pytest.mark.parametrize("offset", [None, -60], ids=["no-install", "older-install"])
    def test_without_a_later_install_the_record_is_still_abandoned(self, tmp_path, offset):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        self._write(path, started, us.STALE_IN_PROGRESS_SECONDS + 60)
        last = None if offset is None else _last(at=_iso(started + offset))

        record = us.abandoned_status(path, last_install=last, running_version="0.110.2")

        assert record is not None
        assert record["current_phase"] == "restarting_service"
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    def test_a_fresh_record_stays_open_even_after_a_completed_install(self, tmp_path):
        """The updater may still run its last phases; it closes its own record."""
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - 30
        self._write(path, started, 5)

        assert us.abandoned_status(
            path, last_install=_last(at=_iso(started + 25)), running_version="0.110.2"
        ) is None
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    def test_a_record_that_cannot_be_closed_is_still_not_abandoned(
        self, tmp_path, monkeypatch, caplog
    ):
        """The verdict holds without the write: no recovery file is due, and
        the next start reads the same evidence and tries the close again."""
        import json
        import logging

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        self._write(path, started, us.STALE_IN_PROGRESS_SECONDS + 60)

        def refuse(*_args, **_kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(us, "_atomic_write", refuse)
        with caplog.at_level(logging.WARNING, logger="jacked.service.update_status"):
            assert us.abandoned_status(
                path, last_install=_last(at=_iso(started + 25)), running_version="0.110.2"
            ) is None
        assert "PermissionError" in caplog.text
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    def test_without_a_running_version_the_age_rule_alone_decides(self, tmp_path):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        self._write(path, started, us.STALE_IN_PROGRESS_SECONDS + 60)

        assert us.abandoned_status(
            path, last_install=_last(at=_iso(started + 25))
        ) is not None
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    def test_an_install_that_this_build_did_not_get_is_still_abandoned(self, tmp_path):
        """Reviewer D: the stale record says 0.110.2 to 0.110.3, an install of
        0.110.3 completed later, and 0.110.2 still runs. Marking it succeeded
        hid an update that never reached this process."""
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        path.write_text(
            json.dumps(_record(
                started_at=_iso(started), from_version="0.110.2", to_version="0.110.3",
                current_phase="restarting_service", phases=[],
            )),
            encoding="utf-8",
        )
        stamp = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
        os.utime(path, (stamp, stamp))

        record = us.abandoned_status(
            path,
            last_install=_last(at=_iso(started + 25), to_version="0.110.3"),
            running_version="0.110.2",
        )

        assert record is not None
        assert record["current_phase"] == "restarting_service"
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"


class _FakeLock:
    """Stands in for the update lock handle: records that it was released."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class TestAbandonedStatusHoldsTheUpdateLock:
    """Reviewer B / pre-mortem 4: ``abandoned_status`` marked the record
    succeeded with no update lock and no attempt identity. Updater B could
    start a new attempt between the read and the write, and the write then
    marked B's live attempt succeeded."""

    @staticmethod
    def _stale_completed(path):
        """A stale in_progress record whose install completed after it began."""
        import json

        from jacked.service import update_status as us

        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        path.write_text(
            json.dumps(_record(
                started_at=_iso(started), current_phase="restarting_service", phases=[],
            )),
            encoding="utf-8",
        )
        stamp = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
        os.utime(path, (stamp, stamp))
        return _last(at=_iso(started + 25))

    def test_a_held_lock_leaves_the_record_open(self, tmp_path, monkeypatch):
        """A held lock means a live updater, and a live updater is never
        abandoned: no write, and no recovery file is due."""
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        last = self._stale_completed(path)
        asked = []

        def held(lock_target):
            asked.append(lock_target)
            return None

        monkeypatch.setattr(us, "acquire_update_lock", held)

        assert us.abandoned_status(
            path, last_install=last, running_version="0.110.2"
        ) is None
        assert asked == [path]
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    def test_the_lock_is_taken_and_released_around_the_close(self, tmp_path, monkeypatch):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        last = self._stale_completed(path)
        lock = _FakeLock()
        monkeypatch.setattr(us, "acquire_update_lock", lambda _path: lock)

        assert us.abandoned_status(
            path, last_install=last, running_version="0.110.2"
        ) is None
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "succeeded"
        assert lock.closed == 1

    @pytest.mark.parametrize(
        "change",
        [
            {"started_at": "2026-11-03T08:00:00+00:00"},
            {"from_version": "0.110.2"},
            {"overall": "failed"},
            # The same attempt wrote again (a fresh mtime): it still runs. A
            # Windows batch holds no OS lock, so this second mtime check is
            # what protects it.
            {},
        ],
        ids=[
            "a-new-attempt-started", "a-new-from-version", "closed-meanwhile",
            "the-attempt-wrote-again",
        ],
    )
    def test_a_record_that_changed_before_the_lock_is_left_alone(
        self, tmp_path, monkeypatch, change
    ):
        """Between the first read and the lock, another updater replaced or
        closed the record. Only the attempt that was read may be closed."""
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        last = self._stale_completed(path)
        lock = _FakeLock()

        def replaced_then_granted(_path):
            current = json.loads(path.read_text(encoding="utf-8"))
            current.update(change)
            path.write_text(json.dumps(current), encoding="utf-8")
            return lock

        monkeypatch.setattr(us, "acquire_update_lock", replaced_then_granted)

        assert us.abandoned_status(
            path, last_install=last, running_version="0.110.2"
        ) is None
        after = json.loads(path.read_text(encoding="utf-8"))
        for key, value in change.items():
            assert after[key] == value
        assert after["overall"] == change.get("overall", "in_progress")
        assert lock.closed == 1

    def test_a_lock_that_cannot_be_taken_leaves_the_record_open(
        self, tmp_path, monkeypatch, caplog
    ):
        """The verdict holds without the write: no recovery file is due, and
        the next start reads the same evidence and tries again."""
        import json
        import logging

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        last = self._stale_completed(path)

        def broken(_path):
            raise PermissionError("read-only")

        monkeypatch.setattr(us, "acquire_update_lock", broken)

        with caplog.at_level(logging.WARNING, logger="jacked.service.update_status"):
            assert us.abandoned_status(
                path, last_install=last, running_version="0.110.2"
            ) is None
        assert "PermissionError" in caplog.text
        assert "read-only" not in caplog.text
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"


class TestCloseAbandonedUpdate:
    """The failed close of an abandoned update takes the same update lock and
    makes the same second read as the completed close. Before this the tray
    called mark_failed with neither, so an updater that started between the
    read and the write had its live attempt marked failed, and the tray wrote
    a recovery file for a failure that did not happen."""

    @staticmethod
    def _stale(path):
        """Write a stale in_progress record; return it as the first read saw it."""
        import json

        from jacked.service import update_status as us

        record = _record(
            started_at=_iso(time.time() - us.STALE_IN_PROGRESS_SECONDS - 120),
            current_phase="migrating_settings",
            phases=[],
        )
        path.write_text(json.dumps(record), encoding="utf-8")
        stamp = time.time() - us.STALE_IN_PROGRESS_SECONDS - 60
        os.utime(path, (stamp, stamp))
        return record

    def test_a_stale_attempt_closes_as_failed_and_returns_the_record(self, tmp_path):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        seen = self._stale(path)

        closed = us.close_abandoned_update(
            path, seen, error="interrupted", recovery="jacked upgrade"
        )

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert closed == stored
        assert stored["overall"] == "failed"
        assert stored["error"] == "interrupted"
        assert stored["recovery"] == "jacked upgrade"
        assert stored["started_at"] == seen["started_at"]
        assert "failed_at" in stored

    def test_a_held_lock_writes_nothing(self, tmp_path, monkeypatch):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        seen = self._stale(path)
        monkeypatch.setattr(us, "acquire_update_lock", lambda _path: None)

        assert us.close_abandoned_update(path, seen, error="interrupted") is None
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"

    @pytest.mark.parametrize(
        "change",
        [{"started_at": "2026-11-03T08:00:00+00:00"}, {"overall": "succeeded"}, {}],
        ids=["a-new-attempt-started", "closed-meanwhile", "the-attempt-wrote-again"],
    )
    def test_a_record_that_changed_before_the_lock_is_left_alone(
        self, tmp_path, monkeypatch, change
    ):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        seen = self._stale(path)
        lock = _FakeLock()

        def replaced_then_granted(_path):
            current = json.loads(path.read_text(encoding="utf-8"))
            current.update(change)
            path.write_text(json.dumps(current), encoding="utf-8")
            return lock

        monkeypatch.setattr(us, "acquire_update_lock", replaced_then_granted)

        assert us.close_abandoned_update(path, seen, error="interrupted") is None
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["overall"] == change.get("overall", "in_progress")
        assert "failed_at" not in after
        assert lock.closed == 1

    def test_a_lock_that_cannot_be_taken_writes_nothing(
        self, tmp_path, monkeypatch, caplog
    ):
        import json
        import logging

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        seen = self._stale(path)

        def broken(_path):
            raise PermissionError("read-only")

        monkeypatch.setattr(us, "acquire_update_lock", broken)

        with caplog.at_level(logging.WARNING, logger="jacked.service.update_status"):
            assert us.close_abandoned_update(path, seen, error="interrupted") is None
        assert "Could not close the abandoned update record: PermissionError" in (
            caplog.text
        )
        assert json.loads(path.read_text(encoding="utf-8"))["overall"] == "in_progress"


class TestFailedUpdateSuperseded:
    """A failed record no longer applies once a later install replaced the
    build it started from."""

    def test_a_later_install_past_the_failed_build_supersedes_it(self):
        from jacked.service import update_status as us

        failed = _record(overall="failed", from_version="0.103.0")
        assert us.failed_update_superseded(failed, _last(), "0.110.2") is True

    def test_a_record_that_did_not_fail_is_never_superseded(self):
        from jacked.service import update_status as us

        for overall in ("in_progress", "succeeded", None):
            record = _record(overall=overall, from_version="0.103.0")
            assert us.failed_update_superseded(record, _last(), "0.110.2") is False

    def test_the_failed_build_still_running_is_not_superseded(self):
        from jacked.service import update_status as us

        failed = _record(overall="failed", from_version="0.110.2")
        assert us.failed_update_superseded(
            failed, _last(to_version="0.110.3"), "0.110.2"
        ) is False

    def test_no_install_after_the_failure_is_not_superseded(self):
        from jacked.service import update_status as us

        failed = _record(overall="failed", from_version="0.103.0")
        early = _last(at="2026-11-01T10:00:00.000000+00:00")
        assert us.failed_update_superseded(failed, early, "0.110.2") is False
        assert us.failed_update_superseded(failed, None, "0.110.2") is False

    @pytest.mark.parametrize(
        "record,running",
        [
            (_record(overall="failed", from_version="garbage!"), "0.110.2"),
            (_record(overall="failed", from_version="0.103.0"), "garbage!"),
            (["not", "a", "record"], "0.110.2"),
        ],
        ids=["bad-from-version", "bad-running-version", "record-not-an-object"],
    )
    def test_malformed_input_is_not_superseded_and_never_raises(self, record, running):
        from jacked.service import update_status as us

        assert us.failed_update_superseded(record, _last(), running) is False

    # Reviewer B / pre-mortem 3: the failed attempt's OWN install record
    # counted as the later repair.

    _FAILED_AT = "2026-11-02T10:02:00.000000+00:00"

    def _own_install_failure(self, **overrides):
        """An update that installed 0.110.2 (its migrate step wrote the
        last-install record at _LATER), then failed verification and could
        not roll the package back, so 0.110.2 runs."""
        phases = [
            {"name": "migrating_settings", "status": "ok",
             "started_at": "2026-11-02T10:00:20.000000+00:00",
             "finished_at": "2026-11-02T10:00:30.000000+00:00"},
            {"name": "verifying_service", "status": "failed",
             "started_at": "2026-11-02T10:00:31.000000+00:00",
             "finished_at": "2026-11-02T10:01:31.000000+00:00"},
        ]
        return _record(
            overall="failed", phases=phases, failed_at=self._FAILED_AT, **overrides
        )

    def test_the_failed_attempts_own_install_does_not_supersede_it(self):
        from jacked.service import update_status as us

        assert us.failed_update_superseded(
            self._own_install_failure(), _last(), "0.110.2"
        ) is False

    def test_a_repair_after_the_failure_supersedes_it(self):
        from jacked.service import update_status as us

        repair = _last(at="2026-11-02T10:05:00.000000+00:00")
        assert us.failed_update_superseded(
            self._own_install_failure(), repair, "0.110.2"
        ) is True
        # An install between the last phase and the close is still the
        # failed attempt's own work (a rollback step), not a repair.
        during = _last(at="2026-11-02T10:01:45.000000+00:00")
        assert us.failed_update_superseded(
            self._own_install_failure(), during, "0.110.2"
        ) is False

    def test_a_record_without_failed_at_uses_its_latest_phase_finish(self):
        """A record from a build before ``failed_at``: the latest phase that
        closed is the last instant the record proves the attempt ran."""
        from jacked.service import update_status as us

        old_shape = self._own_install_failure()
        del old_shape["failed_at"]
        old_shape["phases"].append(
            {"name": "rolling_back", "status": "in_progress",
             "started_at": "2026-11-02T10:01:32.000000+00:00", "finished_at": None}
        )

        assert us.failed_update_superseded(old_shape, _last(), "0.110.2") is False
        after_the_last_phase = _last(at="2026-11-02T10:01:40.000000+00:00")
        assert us.failed_update_superseded(
            old_shape, after_the_last_phase, "0.110.2"
        ) is True

    def test_a_record_with_no_finish_instant_falls_back_to_its_start(self):
        """The #151i acceptance: a 0.103.0 failure with no phases and no
        failed_at, then a later install of 0.110.2, is superseded."""
        from jacked.service import update_status as us

        failed = _record(overall="failed", from_version="0.103.0", phases=[])
        assert us.failed_update_superseded(failed, _last(), "0.110.2") is True

    @pytest.mark.parametrize("value", ["yesterday", "2026-11-02T10:02:00", 7])
    def test_an_unusable_failed_at_proves_nothing(self, caplog, value):
        import logging

        from jacked.service import update_status as us

        record = self._own_install_failure()
        record["failed_at"] = value
        repair = _last(at="2026-11-02T10:05:00.000000+00:00")

        with caplog.at_level(logging.DEBUG, logger="jacked.service.update_status"):
            assert us.failed_update_superseded(record, repair, "0.110.2") is False
        assert "failed_at" in caplog.text

    def test_the_real_writers_close_the_record_after_its_own_install(
        self, tmp_path, monkeypatch
    ):
        """The updater's own sequence, through the real writers: the migrate
        step's install, a failed verification, then mark_failed."""
        import json
        from datetime import datetime, timedelta, timezone

        from jacked.service import update_status as us

        clock = [datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)]

        def tick():
            clock[0] += timedelta(seconds=1)
            return clock[0].isoformat()

        monkeypatch.setattr(us, "_now_iso", tick)
        path = tmp_path / "status.json"
        us.init_status(path, "0.110.1", "0.110.2", "uv")
        us.begin_phase(path, "migrating_settings")
        own_install = {"at": tick(), "from_version": "0.110.1", "to_version": "0.110.2"}
        us.end_phase(path, "migrating_settings", status="ok")
        us.begin_phase(path, "verifying_service")
        us.end_phase(path, "verifying_service", status="failed", error="no bind")
        us.mark_failed(path, error="never became ready", recovery="jacked upgrade")
        record = json.loads(path.read_text(encoding="utf-8"))

        assert us.failed_update_superseded(record, own_install, "0.110.2") is False
        repair = {"at": tick(), "from_version": "0.110.2", "to_version": "0.110.2"}
        assert us.failed_update_superseded(record, repair, "0.110.2") is True


class TestFailedAtStamp:
    """The record carries the instant it closed as failed (``failed_at``),
    so a later install can be compared with the failure, not the start."""

    @staticmethod
    def _instant(value):
        from datetime import datetime

        parsed = datetime.fromisoformat(value)
        assert parsed.utcoffset() is not None
        return parsed

    def test_mark_failed_stamps_failed_at(self, tmp_path):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        us.init_status(path, "0.110.1", "0.110.2", "uv")
        before = self._instant(us._now_iso())
        us.mark_failed(path, error="boom")
        record = json.loads(path.read_text(encoding="utf-8"))

        assert self._instant(record["failed_at"]) >= before

    def test_a_failed_phase_stamps_failed_at_and_an_ok_phase_does_not(self, tmp_path):
        import json

        from jacked.service import update_status as us

        path = tmp_path / "status.json"
        us.init_status(path, "0.110.1", "0.110.2", "uv")
        us.begin_phase(path, "preflight")
        us.end_phase(path, "preflight", status="ok")
        assert "failed_at" not in json.loads(path.read_text(encoding="utf-8"))

        us.begin_phase(path, "migrating_settings")
        us.end_phase(path, "migrating_settings", status="failed", error="boom")
        record = json.loads(path.read_text(encoding="utf-8"))

        assert record["failed_at"] == record["phases"][-1]["finished_at"]

    def test_a_later_close_moves_failed_at_forward(self, tmp_path, monkeypatch):
        """mark_failed after a failed phase (the rollback, then the close):
        everything before the last close is the failed attempt's own work."""
        import json
        from datetime import datetime, timedelta, timezone

        from jacked.service import update_status as us

        clock = [datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)]

        def tick():
            clock[0] += timedelta(seconds=1)
            return clock[0].isoformat()

        monkeypatch.setattr(us, "_now_iso", tick)
        path = tmp_path / "status.json"
        us.init_status(path, "0.110.1", "0.110.2", "uv")
        us.begin_phase(path, "verifying_service")
        us.end_phase(path, "verifying_service", status="failed", error="no bind")
        first = json.loads(path.read_text(encoding="utf-8"))["failed_at"]
        us.mark_failed(path, error="never became ready")
        last = json.loads(path.read_text(encoding="utf-8"))["failed_at"]

        assert self._instant(last) > self._instant(first)


class TestRecoveryPredatesCompletedUpdate:
    """Reviewer D: a recovery file left by an EARLIER failure survived a
    later update that closed as succeeded, and toasted on every start."""

    _DONE = {
        "overall": "succeeded",
        "started_at": _STARTED,
        "from_version": "0.110.1",
        "to_version": "0.110.2",
    }

    @staticmethod
    def _epoch(value):
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()

    def test_a_file_older_than_the_completed_update_is_stale(self):
        from jacked.service import update_status as us

        earlier = self._epoch("2026-11-01T10:00:00+00:00")
        assert us.recovery_predates_completed_update(
            self._DONE, "0.110.2", earlier
        ) is True

    @pytest.mark.parametrize(
        "written", [_STARTED, "2026-11-02T10:00:05+00:00"], ids=["at-the-start", "later"]
    )
    def test_a_file_written_during_or_after_the_update_stays(self, written):
        from jacked.service import update_status as us

        assert us.recovery_predates_completed_update(
            self._DONE, "0.110.2", self._epoch(written)
        ) is False

    def test_the_build_the_update_started_from_still_running_keeps_the_file(self):
        from jacked.service import update_status as us

        earlier = self._epoch("2026-11-01T10:00:00+00:00")
        assert us.recovery_predates_completed_update(
            self._DONE, "0.110.1", earlier
        ) is False

    @pytest.mark.parametrize("overall", ["failed", "in_progress", None])
    def test_a_record_that_did_not_succeed_keeps_the_file(self, overall):
        from jacked.service import update_status as us

        earlier = self._epoch("2026-11-01T10:00:00+00:00")
        assert us.recovery_predates_completed_update(
            {**self._DONE, "overall": overall}, "0.110.2", earlier
        ) is False

    @pytest.mark.parametrize(
        "record,running",
        [
            ({**_DONE, "from_version": "garbage!"}, "0.110.2"),
            (_DONE, "garbage!"),
            ({**_DONE, "started_at": "yesterday"}, "0.110.2"),
            ({**_DONE, "started_at": "2026-11-02T10:00:00"}, "0.110.2"),
            (["not", "a", "record"], "0.110.2"),
        ],
        ids=["bad-from-version", "bad-running", "bad-start", "naive-start", "not-an-object"],
    )
    def test_malformed_input_keeps_the_file_and_never_raises(self, record, running):
        from jacked.service import update_status as us

        earlier = self._epoch("2026-11-01T10:00:00+00:00")
        assert us.recovery_predates_completed_update(record, running, earlier) is False
