"""The project routes open files only in a project that jacked recorded.

Every ``/api/project/*`` and ``/api/claude-settings/project-*`` route takes a
``repo_path`` from the request. The dashboard can serve a remote client (the
remote-access toggle), so that value is attacker input. The routes must:

* refuse a path that jacked has no activity for, before they touch the disk
  at that path (no "is it a directory" oracle for an unknown path);
* refuse when the project list cannot be read (no database), instead of
  opening any git folder on the disk;
* open files at the path that jacked recorded, never at the request text
  (``/known/../victim`` must not reach ``/victim``).

Each test sends the hostile value through a TestClient and checks that no
file appears on the disk.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.project_paths import ProjectRefused, known_project_path
from jacked.api.routes import permissions, system
from tests._known_projects import KnownProjects as _Projects


class _BrokenProjects:
    def get_project_activity_summary(self, limit=20):
        raise RuntimeError("database is locked")


def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(system.router, prefix="/api")
    app.include_router(permissions.router, prefix="/api")
    app.state.db = db
    return TestClient(app)


def _git_repo(path):
    (path / ".git").mkdir(parents=True)
    return path


# Each project route, as (method, url, how the repo_path travels).
_ROUTES = {
    "permissions-get": ("GET", "/api/claude-settings/project-permissions", "query"),
    "permissions-put": ("PUT", "/api/claude-settings/project-permissions", "body"),
    "permissions-rule": ("POST", "/api/claude-settings/permissions/rule", "body"),
    "guardrails-init": ("POST", "/api/project/guardrails-init", "body"),
    "lint-hook-init": ("POST", "/api/project/lint-hook-init", "body"),
    "env-get": ("GET", "/api/project/env", "query"),
    "env-put": ("PUT", "/api/project/env", "body"),
    "env-detect": ("POST", "/api/project/env/detect", "body"),
    "lessons-get": ("GET", "/api/project/lessons", "query"),
    "lessons-put": ("PUT", "/api/project/lessons", "body"),
}

_BODIES = {
    "permissions-put": {"allow": ["Bash(git status:*)"]},
    "permissions-rule": {"pattern": "Bash(git status:*)", "scope": "project"},
    "env-put": {"env_path": "/nonexistent/env"},
    "lessons-put": {"lessons": [{"index": 0, "strike": 1, "text": "Use uv"}]},
}


def _send(client, route, repo_path):
    method, url, where = _ROUTES[route]
    if where == "query":
        return client.request(method, url, params={"repo_path": repo_path})
    body = {"repo_path": repo_path, **_BODIES.get(route, {})}
    return client.request(method, url, json=body)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_an_unknown_git_folder_is_refused_and_left_alone(tmp_path, route):
    known = _git_repo(tmp_path / "known")
    victim = _git_repo(tmp_path / "victim")
    before = _tree(victim)

    resp = _send(_client(_Projects(known)), route, str(victim))

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNKNOWN_PROJECT"
    assert _tree(victim) == before


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_traversal_out_of_a_known_project_is_refused(tmp_path, route):
    known = _git_repo(tmp_path / "known")
    victim = _git_repo(tmp_path / "victim")
    before = _tree(victim)

    resp = _send(_client(_Projects(known)), route, f"{known}/../victim")

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNKNOWN_PROJECT"
    assert _tree(victim) == before


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_an_unknown_path_gets_no_disk_oracle(tmp_path, route):
    """A missing folder and a plain folder get the same answer as a git
    folder: the route does not tell a remote client what is on the disk."""
    known = _git_repo(tmp_path / "known")
    plain = tmp_path / "plain"
    plain.mkdir()
    client = _client(_Projects(known))

    answers = {
        (r.status_code, r.json()["error"]["code"])
        for r in (
            _send(client, route, str(tmp_path / "missing")),
            _send(client, route, str(plain)),
        )
    }

    assert answers == {(422, "UNKNOWN_PROJECT")}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_no_database_refuses_instead_of_opening_any_git_folder(tmp_path, route):
    victim = _git_repo(tmp_path / "victim")
    before = _tree(victim)

    resp = _send(_client(None), route, str(victim))

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "DB_UNAVAILABLE"
    assert _tree(victim) == before


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_database_error_refuses(tmp_path, route):
    victim = _git_repo(tmp_path / "victim")
    before = _tree(victim)

    resp = _send(_client(_BrokenProjects()), route, str(victim))

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "DB_ERROR"
    assert _tree(victim) == before


def test_a_known_project_is_written_at_its_recorded_path(tmp_path):
    known = _git_repo(tmp_path / "known")
    client = _client(_Projects(known))

    lessons = _send(client, "lessons-put", str(known) + "/")
    settings = _send(client, "permissions-put", str(known))

    assert lessons.json() == {"saved": True, "count": 1}
    assert settings.json() == {"ok": True}
    assert (known / "lessons.md").exists()
    assert (known / ".claude" / "settings.local.json").exists()


class TestKnownProjectPath:
    def test_returns_the_recorded_value_not_the_request_text(self, tmp_path):
        recorded = str(tmp_path / "repo").replace("\\", "/")
        db = _Projects(recorded)

        assert known_project_path(recorded + "/", db) == recorded
        assert known_project_path(recorded.replace("/", "\\"), db) == recorded

    @pytest.mark.parametrize("value", [None, "", 7])
    def test_refuses_a_missing_value(self, value):
        with pytest.raises(ProjectRefused) as refused:
            known_project_path(value, _Projects("/repo"))
        assert refused.value.status_code == 422
        assert refused.value.code == "INVALID_REPO_PATH"

    def test_refuses_without_a_database(self):
        with pytest.raises(ProjectRefused) as refused:
            known_project_path("/repo", None)
        assert (refused.value.status_code, refused.value.code) == (503, "DB_UNAVAILABLE")

    def test_refuses_on_a_database_error(self):
        with pytest.raises(ProjectRefused) as refused:
            known_project_path("/repo", _BrokenProjects())
        assert (refused.value.status_code, refused.value.code) == (503, "DB_ERROR")

    def test_refuses_an_unknown_path(self):
        with pytest.raises(ProjectRefused) as refused:
            known_project_path("/repo/../other", _Projects("/repo", "/other"))
        assert (refused.value.status_code, refused.value.code) == (422, "UNKNOWN_PROJECT")

    def test_the_refusal_is_a_json_answer(self):
        answer = ProjectRefused(422, "UNKNOWN_PROJECT", "No.").response()
        assert answer.status_code == 422
        assert answer.body == b'{"error":{"message":"No.","code":"UNKNOWN_PROJECT"}}'
