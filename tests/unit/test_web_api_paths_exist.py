"""Each fixed ``/api/...`` path in the dashboard code has a server route.

The dashboard asked ``/api/logs/sessions`` for the project-permissions list
after that route was removed (0.70.0). The request got a 404, the ``catch``
made the list empty, and no test saw it: the list showed no projects. This
test reads each fixed API path in the dashboard JavaScript and HTML and
matches it against the routes of the app. A path that is made at run time
(a template with ``${...}``) is out of scope.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
SETTINGS_JS = WEB / "js" / "components" / "settings.js"

# A quoted path that ends at the quote or at a query string.
_FIXED_PATH = re.compile(r"""([`'"])(/api/[A-Za-z0-9_./-]*)(?:\?[^`'"$]*)?\1""")


def _fixed_api_paths():
    found = []
    for path in sorted(WEB.rglob("*")):
        if path.suffix not in {".js", ".html"} or not path.is_file():
            continue
        for match in _FIXED_PATH.finditer(path.read_text(encoding="utf-8")):
            found.append((path.relative_to(WEB).as_posix(), match.group(2)))
    return found


def _api_route_paths():
    from fastapi.routing import APIRoute

    from jacked.api.main import app

    return [
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
    ]


def _has_route(url, route_paths):
    """True when a route serves ``url``. A path that ends with ``/`` is the
    start of a path that the code completes at run time (``'/api/x/' + id``),
    so a route that starts with it is enough."""
    from starlette.routing import compile_path

    if url.endswith("/"):
        return any(path.startswith(url) for path in route_paths)
    return any(compile_path(path)[0].match(url) for path in route_paths)


def test_the_scan_finds_the_dashboard_api_paths():
    paths = {url for _, url in _fixed_api_paths()}
    assert "/api/installations/overview" in paths
    assert len(paths) > 20


def test_each_fixed_dashboard_api_path_has_a_server_route():
    route_paths = _api_route_paths()
    missing = sorted(
        f"{source}: {url}"
        for source, url in _fixed_api_paths()
        if url != "/api/" and not _has_route(url, route_paths)
    )
    assert missing == []


def test_a_removed_route_is_reported():
    """The check names the path that broke the permission project list."""
    assert not _has_route("/api/logs/sessions", _api_route_paths())
    assert _has_route("/api/auth/accounts/", ["/api/auth/accounts/{account_id}"])
    assert not _has_route("/api/gone/", ["/api/auth/accounts/{account_id}"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_permission_project_list_comes_from_the_recorded_projects(tmp_path):
    overview = {
        "projects": [
            {"repo_path": "/work/b"},
            {"repo_path": "/work/a"},
            {"repo_path": "/work/b"},
            {"repo_path": "/Work/A"},
            {"repo_path": ""},
            {},
            None,
        ]
    }
    # eval() runs only this repo's own settings.js in a throwaway node
    # process, the same pattern as the other test_web_js_* suites.
    program = f"""
const fs = require('fs');
global.window = {{ jackedState: {{}} }};
global.document = {{ getElementById: () => null, querySelectorAll: () => [], querySelector: () => null }};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
eval(fs.readFileSync({json.dumps(str(SETTINGS_JS))}, 'utf8'));
process.stdout.write(JSON.stringify([
    permProjectRepoList({json.dumps(overview)}),
    permProjectRepoList(null),
    permProjectRepoList({{}}),
]));
"""
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [["/Work/A", "/work/a", "/work/b"], [], []]
