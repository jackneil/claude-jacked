"""Tests for the update-status JSON reader/writer."""

import os
import time


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
