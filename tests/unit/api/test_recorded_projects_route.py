"""GET /api/claude-settings/projects lists every project that jacked recorded.

The project-permissions list in the dashboard reads this route. The
installations overview stops at the 20 most recent projects, so an older
project could not be selected (G3-UI-1). This route has no limit, gives each
path with forward slashes, and reads no project folder on the disk.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes import permissions
from jacked.web.database import Database


def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(permissions.router, prefix="/api")
    app.state.db = db
    return TestClient(app)


def test_every_recorded_project_is_listed_past_the_overview_limit(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "jacked.db"))
    paths = [f"/work/project-{n:02d}" for n in range(25)]
    for n, path in enumerate(paths):
        # Half come from each activity table; one path is recorded twice.
        if n % 2:
            db.record_hook_execution("Stop", hook_name="h", repo_path=path, session_id=f"s{n}")
        else:
            db.record_command_usage("dc", repo_path=path, session_id=f"s{n}")
    db.record_hook_execution("Stop", hook_name="h", repo_path=paths[0], session_id="again")
    # A Windows writer can keep back slashes: the list gives forward slashes.
    db.record_hook_execution("Stop", hook_name="h", repo_path="C:\\work\\win", session_id="w")
    db.record_hook_execution("Stop", hook_name="h", repo_path="C:/work/win", session_id="w2")
    db.record_hook_execution("Stop", hook_name="h", repo_path="", session_id="empty")
    db.record_hook_execution("Stop", hook_name="h", repo_path=None, session_id="none")

    # No project folder is read on the disk for the list.
    def _no_disk(*_a, **_k):
        raise AssertionError("the project list must not read project folders")

    monkeypatch.setattr("jacked.guardrails.check_project_setup", _no_disk)

    response = _client(db).get("/api/claude-settings/projects")

    assert response.status_code == 200
    assert response.json() == {"projects": sorted(paths + ["C:/work/win"])}
    assert len(response.json()["projects"]) == 26


def test_no_database_gives_an_empty_list():
    response = _client(None).get("/api/claude-settings/projects")
    assert response.status_code == 200
    assert response.json() == {"projects": []}


def test_a_database_that_fails_gives_an_empty_list_and_logs(caplog):
    class _Broken:
        def _reader(self):
            raise RuntimeError("database is locked")

    response = _client(_Broken()).get("/api/claude-settings/projects")
    assert response.status_code == 200
    assert response.json() == {"projects": []}
    assert "recorded projects" in caplog.text
