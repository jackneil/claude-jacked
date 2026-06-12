"""Shared fixtures for service tests.

Every test in this directory gets UPDATE_STATUS_FILE redirected to a tmp
path automatically. Without this, any test that drives updater.run_update
(directly or transitively) writes the REAL ~/.claude/jacked-update-status.json
— a test run on a user machine then leaves a bogus "failed update" record
that the tray surfaces as a recovery warning. Individual tests may still
monkeypatch their own path; this autouse default only covers the ones that
forget.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_update_status_file(tmp_path, monkeypatch):
    from jacked.service import update_status as us_mod

    monkeypatch.setattr(
        us_mod, "UPDATE_STATUS_FILE", tmp_path / "update-status.json",
    )
