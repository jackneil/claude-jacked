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

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.project_paths import ProjectRefused, known_project_path
from jacked.api.routes import permissions, system
from tests._known_projects import KnownProjects as _Projects
from tests._platform import requires_symlinks


class _BrokenProjects:
    def find_recorded_project(self, candidates):
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


# --- every repo_path route goes through the one guard -----------------------


def _routes_that_take_a_repo_path():
    from fastapi.routing import APIRoute

    from jacked.api.main import app

    found = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        names = {p.name for p in route.dependant.query_params}
        for body in route.dependant.body_params:
            names.add(body.name)
            model = getattr(body.field_info, "annotation", None)
            names |= set(getattr(model, "model_fields", {}) or {})
        if "repo_path" in names:
            for method in route.methods:
                found.add((method, route.path))
    return found


def test_the_guarded_route_list_names_every_route_that_takes_a_repo_path():
    """A new route that takes a repo_path must join _ROUTES, so that each
    test above (no database, unknown path, traversal) runs against it."""
    guarded = {(method, url) for method, url, _ in _ROUTES.values()}
    assert _routes_that_take_a_repo_path() == guarded


# --- the real database: an exact lookup, at any age --------------------------


def _real_db(tmp_path, recorded_old, newer_count=120):
    """A real Database where ``recorded_old`` is older than ``newer_count``
    projects with more recent activity."""
    from jacked.web.database import Database

    db = Database(str(tmp_path / "jacked.db"))
    db.record_hook_execution(
        "Stop", hook_name="h", session_id="s0",
        repo_path=str(recorded_old).replace("\\", "/"),
        timestamp="2020-01-01T00:00:00+00:00",
    )
    for n in range(newer_count):
        db.record_hook_execution(
            "Stop", hook_name="h", session_id=f"s{n + 1}",
            repo_path=f"/elsewhere/project-{n:03d}",
            timestamp=f"2026-11-01T00:{n // 60:02d}:{n % 60:02d}+00:00",
        )
    return db


_REFUSALS = {"UNKNOWN_PROJECT", "INVALID_REPO_PATH", "DB_UNAVAILABLE", "DB_ERROR"}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_a_recorded_project_older_than_the_100_most_recent_is_accepted(
    tmp_path, route, monkeypatch
):
    import jacked.cli

    # env-detect looks at the interpreter of this process; a fixed answer
    # keeps the route on its write path.
    monkeypatch.setattr(jacked.cli, "_detect_project_env", lambda: None)
    old = _git_repo(tmp_path / "old")
    db = _real_db(tmp_path, old)
    recent = {row["repo_path"] for row in db.get_project_activity_summary(limit=100)}
    assert str(old).replace("\\", "/") not in recent

    resp = _send(_client(db), route, str(old))

    code = (resp.json().get("error") or {}).get("code") if resp.status_code >= 400 else None
    assert resp.status_code != 503, resp.text
    assert code not in _REFUSALS, resp.text


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_the_real_database_refuses_an_unrecorded_git_folder(tmp_path, route):
    old = _git_repo(tmp_path / "old")
    victim = _git_repo(tmp_path / "victim")
    before = _tree(victim)

    resp = _send(_client(_real_db(tmp_path, old, newer_count=3)), route, str(victim))

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "UNKNOWN_PROJECT"
    assert _tree(victim) == before


class TestFindRecordedProject:
    @pytest.fixture
    def db(self, tmp_path):
        from jacked.web.database import Database

        db = Database(str(tmp_path / "jacked.db"))
        db.record_hook_execution("Stop", hook_name="h", repo_path="/work/repo")
        db.record_command_usage("dc", repo_path="C:\\Work\\win-repo")
        return db

    def test_an_exact_forward_slash_match(self, db):
        assert db.find_recorded_project(["/work/repo"]) == "/work/repo"

    def test_a_back_slash_record_matches_its_forward_slash_text(self, db):
        assert db.find_recorded_project(["C:/Work/win-repo"]) == "C:/Work/win-repo"
        assert db.find_recorded_project(["C:\\Work\\win-repo"]) == "C:/Work/win-repo"

    @pytest.mark.parametrize(
        "value",
        [
            "/work",
            "/work/repo/sub",
            "/WORK/REPO",
            "c:/work/win-repo",
            "/work/repo/../repo",
            "/work/repo' OR '1'='1",
            "%",
        ],
    )
    def test_anything_else_has_no_match(self, db, value):
        assert db.find_recorded_project([value]) is None

    def test_no_candidates_has_no_match(self, db):
        assert db.find_recorded_project([]) is None
        assert db.find_recorded_project(["", None]) is None


# --- .git/jacked/env: the first line, never through a link -------------------


def _env_answer(repo):
    resp = _send(_client(_Projects(repo)), "env-get", str(repo))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _env_file(repo):
    folder = repo / ".git" / "jacked"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "env"


def test_the_env_route_returns_only_the_first_line(tmp_path):
    from jacked.guardrails import check_project_setup

    repo = _git_repo(tmp_path / "repo")
    _env_file(repo).write_text("/opt/env  \nSECRET=do-not-show\n", encoding="utf-8")

    assert _env_answer(repo) == {"env_path": "/opt/env", "source": "file"}
    assert check_project_setup(repo)["env_path"] == "/opt/env"


def test_an_empty_env_file_is_not_set(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _env_file(repo).write_text("\nsecond\n", encoding="utf-8")

    assert _env_answer(repo) == {"env_path": None, "source": None}


@requires_symlinks
def test_an_env_file_that_is_a_link_is_not_read(tmp_path, caplog):
    from jacked.guardrails import check_project_setup

    repo = _git_repo(tmp_path / "repo")
    private = tmp_path / "private.txt"
    private.write_text("private-content\n", encoding="utf-8")
    _env_file(repo).symlink_to(private)

    with caplog.at_level("WARNING", logger="jacked.guardrails"):
        answer = _env_answer(repo)
        setup = check_project_setup(repo)

    assert answer == {"env_path": None, "source": None}
    assert setup["env_path"] is None
    assert "private-content" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "is a link" in caplog.text


@requires_symlinks
def test_an_env_file_inside_the_project_that_is_a_link_is_not_read(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "notes.txt").write_text("/opt/from-notes\n", encoding="utf-8")
    _env_file(repo).symlink_to(repo / "notes.txt")

    assert _env_answer(repo)["env_path"] is None


@requires_symlinks
def test_an_env_folder_link_out_of_the_git_folder_is_not_read(tmp_path, caplog):
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "env").write_text("private-content\n", encoding="utf-8")
    (repo / ".git" / "jacked").symlink_to(outside, target_is_directory=True)

    with caplog.at_level("WARNING", logger="jacked.guardrails"):
        answer = _env_answer(repo)

    assert answer["env_path"] is None
    assert "private-content" not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this platform")
def test_an_env_pipe_is_not_read(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    os.mkfifo(_env_file(repo))

    assert _env_answer(repo)["env_path"] is None
