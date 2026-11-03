"""Keep the dashboard's reads and writes of project files inside the project.

A repository can ship a link (a symlink, or a junction on Windows) at a path
that the dashboard reads or writes, such as ``.claude``,
``.claude/settings.local.json`` or ``lessons.md``. A read follows the link,
and a write follows a linked parent folder. So a link that points outside the
project moves the read or the write out of the project: for example, a save
could copy the user's global settings, or a private file, into a file that
the project commits. A route checks each path with ``first_outside_project``
before it reads or writes, and refuses with ``outside_project_response``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import status
from fastapi.responses import JSONResponse

LINK_OUTSIDE_PROJECT = "LINK_OUTSIDE_PROJECT"


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
