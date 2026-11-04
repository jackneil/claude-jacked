"""Keep the dashboard's reads and writes of project files inside the project.

A repository can ship a link (a symlink, or a junction on Windows) at a path
that the dashboard reads or writes, such as ``.claude``,
``.claude/settings.local.json`` or ``lessons.md``. A read follows the link,
and a write follows a linked parent folder. So a link that points outside the
project moves the read or the write out of the project: for example, a save
could copy the user's global settings, or a private file, into a file that
the project commits. A route checks each path with ``first_outside_project``
before it reads or writes, and refuses with ``outside_project_response``.

The project folder itself comes from the request (``repo_path``), and the
dashboard can serve a remote client. So a route first gets the folder with
``known_project_path``: the folder must be a project that jacked recorded
from its own session activity, and the route then uses the recorded text,
not the request text. A path that jacked did not record gets no disk access.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import status
from fastapi.responses import JSONResponse

LINK_OUTSIDE_PROJECT = "LINK_OUTSIDE_PROJECT"

class ProjectRefused(Exception):
    """A route does not open the project that a request names.

    ``status_code``, ``code`` and ``message`` make the JSON error answer.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def response(self) -> JSONResponse:
        """The JSON error answer for this refusal."""
        return JSONResponse(
            status_code=self.status_code,
            content={"error": {"message": self.message, "code": self.code}},
        )


def _spellings(repo_path: str) -> set[str]:
    """The ways that a client can write one recorded project path.

    The database keeps each path with forward slashes. ``Path`` removes a
    trailing slash and a doubled slash without a disk access.

    >>> {"/a/b", "/a/b/"} <= _spellings("/a/b/")
    True
    """
    spellings = {repo_path, str(Path(repo_path))}
    return spellings | {s.replace("\\", "/") for s in spellings}


def known_project_path(repo_path, db) -> str:
    """Return the path that jacked recorded for the project ``repo_path``.

    The request text is attacker input when the dashboard serves a remote
    client. This function looks for the text in the projects that jacked
    recorded from its own session activity (``find_recorded_project``), at
    any age, before any disk access at the path. It returns the recorded
    text, so the route opens files only at a recorded path. ``..`` in the
    request does not match a recorded path.

    Raise ``ProjectRefused`` when the value is empty, when there is no
    database or the lookup fails (fail closed), or when jacked did not record
    the project.

    >>> class Db:
    ...     def find_recorded_project(self, candidates):
    ...         return "/work/repo" if "/work/repo" in candidates else None
    >>> known_project_path("/work/repo/", Db())
    '/work/repo'
    >>> try:
    ...     known_project_path("/work/repo/../secret", Db())
    ... except ProjectRefused as refused:
    ...     refused.code
    'UNKNOWN_PROJECT'
    """
    if not isinstance(repo_path, str) or not repo_path:
        raise ProjectRefused(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_REPO_PATH",
            "The request has no project path. Send the repo_path of a project.",
        )
    if db is None:
        raise ProjectRefused(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DB_UNAVAILABLE",
            "The project list is not available, because the database did not "
            "start. The dashboard does not open project files without it.",
        )
    try:
        recorded = db.find_recorded_project(sorted(_spellings(repo_path)))
    except Exception:
        raise ProjectRefused(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DB_ERROR",
            "The dashboard cannot read the project list from the database.",
        ) from None
    if isinstance(recorded, str) and recorded:
        return recorded
    raise ProjectRefused(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "UNKNOWN_PROJECT",
        f"Jacked has no activity for the project {repo_path}. "
        "Use Claude Code in the project first.",
    )


class LinkOutsideProject(Exception):
    """A project path is a link to a location outside the project."""

    def __init__(self, relative: str) -> None:
        super().__init__(relative)
        self.relative = relative


def first_outside_project(project, *relative_paths: str) -> str | None:
    """Return the first of ``relative_paths`` that resolves outside ``project``.

    Each path is resolved with every link in it followed. A path that cannot
    be resolved (a link loop) counts as outside. ``None`` means that every
    path stays in the project. A path that does not exist yet stays in the
    project when its existing parts do.

    >>> import tempfile
    >>> root = Path(tempfile.mkdtemp())
    >>> first_outside_project(root, ".claude", ".claude/settings.local.json") is None
    True
    >>> first_outside_project(root, "lessons.md", "../elsewhere.md")
    '../elsewhere.md'
    """
    root = Path(project).resolve()
    for relative in relative_paths:
        try:
            (root / relative).resolve().relative_to(root)
        except (ValueError, OSError, RuntimeError):
            return relative
    return None


def refuse_outside_project(project, *relative_paths: str) -> None:
    """Raise ``LinkOutsideProject`` for the first path that leaves ``project``.

    A writer calls this right before its write, in the worker that holds the
    lock. The route checked the paths before it took the lock, but a link
    made after that check would move the write outside the project. The
    window that stays is the write itself.

    >>> import tempfile
    >>> root = Path(tempfile.mkdtemp())
    >>> refuse_outside_project(root, "lessons.md") is None
    True
    >>> try:
    ...     refuse_outside_project(root, "../elsewhere.md")
    ... except LinkOutsideProject as escaped:
    ...     escaped.relative
    '../elsewhere.md'
    """
    escaped = first_outside_project(project, *relative_paths)
    if escaped:
        raise LinkOutsideProject(escaped)


def outside_project_response(relative: str) -> JSONResponse:
    """The 422 answer for a project path that is a link to outside the project."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "message": (
                    f"The {relative} path in this project is a link to a location "
                    "outside the project. The dashboard does not read or write "
                    "through this link. Replace the link with a file or a folder "
                    "in the project."
                ),
                "code": LINK_OUTSIDE_PROJECT,
            }
        },
    )
