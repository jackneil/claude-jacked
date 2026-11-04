"""A database stand-in for the routes that open files in a project.

The project routes open a project only when jacked recorded it
(``jacked.api.project_paths.known_project_path``). A route test gives the
route this stand-in, with the test's project folders, as ``app.state.db``.
"""

from __future__ import annotations

from types import SimpleNamespace


class KnownProjects:
    """Record ``repos`` as projects with jacked activity.

    The real database keeps each path with forward slashes, so the stand-in
    does the same.
    """

    def __init__(self, *repos):
        self.rows = [{"repo_path": str(r).replace("\\", "/")} for r in repos]

    def find_recorded_project(self, candidates):
        """The exact lookup of ``Database.find_recorded_project``, at any age."""
        recorded = {row["repo_path"] for row in self.rows}
        for candidate in candidates:
            forward = candidate.replace("\\", "/")
            if forward in recorded:
                return forward
        return None

    def get_installation_by_repo(self, repo_path):
        return None

    def update_installation_env(self, repo_path, env_path):
        return None


def request_for(*repos):
    """A request stand-in whose ``app.state.db`` knows ``repos``."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=KnownProjects(*repos))))
