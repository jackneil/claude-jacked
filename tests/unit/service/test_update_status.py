"""Tests for the update-status JSON reader/writer."""

import os
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
    from jacked.service.update_status import init_or_adopt_status, read_status
    p = tmp_path / "status.json"
    # Tray pre-init writes the real from/to metadata.
    init_or_adopt_status(p, from_version="0.41.19", to_version="0.41.20", method="uv")
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
    from jacked.service.update_status import (
        init_status, read_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 10
    os.utime(p, (old, old))
    assert read_status(p) is None


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
    assert r.json() == {"status": None, "mtime_iso": None}


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
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")  # tray pre-init
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
        init_or_adopt_status(path, "1.0.0", "1.1.0", "uv", updater_pid=999999)
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
