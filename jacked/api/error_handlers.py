"""App-wide handlers for exceptions that no route handled (#289i).

The text of these exceptions is not written for the user. A
``FileNotFoundError`` names a full path, and a ``ValueError`` can contain any
value. The dashboard can be reachable from a different machine, so the client
gets fixed text, or the message of a ``PublicError``. The log gets the
exception type and the route template, and no value.

``jacked.api.main`` registers both handlers on the app.
"""

from __future__ import annotations

import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse

from jacked.public_errors import describe, public_message

logger = logging.getLogger("jacked.api.main")

INVALID_VALUE_TEXT = "The request has a value that jacked cannot use."
NOT_FOUND_TEXT = "A file that the request needs was not found."


def route_template(request: Request) -> str:
    """Return the path template of the matched route, never the raw URL.

    >>> from starlette.requests import Request
    >>> route_template(Request({"type": "http", "method": "GET", "path": "/x", "headers": []}))
    '(no route)'
    """
    route = request.scope.get("route")
    return getattr(route, "path", None) or "(no route)"


def _log(request: Request, exc: BaseException) -> None:
    logger.warning(
        "Unhandled %s on %s %s", describe(exc), request.method, route_template(request)
    )


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Return a 400 with fixed text for an unhandled ``ValueError``."""
    _log(request, exc)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": {
            "message": public_message(exc, INVALID_VALUE_TEXT),
            "code": "VALIDATION_ERROR",
        }},
    )


async def not_found_handler(request: Request, exc: FileNotFoundError) -> JSONResponse:
    """Return a 404 with fixed text for an unhandled ``FileNotFoundError``."""
    _log(request, exc)
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": {
            "message": public_message(exc, NOT_FOUND_TEXT),
            "code": "NOT_FOUND",
        }},
    )
