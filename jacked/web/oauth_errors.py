"""The errors of the OAuth sign-in flow, and the text that the user sees.

``jacked.web.oauth`` imports and re-exports these names. They live here so
that the flow module stays inside its line budget (#289i).
"""

from __future__ import annotations

import httpx

from jacked.public_errors import PublicError, public_message


class OAuthIdentityError(PublicError, ValueError):
    """The sign-in gave tokens for a different account, org or target.

    The message is written for the user: it names the account emails and
    orgs that the dashboard already shows. It is a ``ValueError`` for older
    callers.
    """


class OAuthVerificationError(PublicError, RuntimeError):
    """jacked cannot verify whose tokens the sign-in gave.

    The message is written for the user.
    """


_SIGN_IN_FAILED = (
    "Sign-in could not be completed. Check the jacked log for the cause, "
    "then start the sign-in again."
)


def flow_error_text(exc: BaseException) -> str:
    """The text of a failed sign-in that the user sees (#289i).

    A refusal written for the user keeps its text. An HTTP error gives its
    status code only, and a network error gives a fixed sentence. Any other
    exception gives fixed text, because its own text can name a path or a
    response body.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            f"The sign-in server returned HTTP {exc.response.status_code}. "
            "Start the sign-in again."
        )
    if isinstance(exc, httpx.TransportError):
        return (
            "jacked could not reach the sign-in server. Check the network "
            "connection, then start the sign-in again."
        )
    return public_message(exc, _SIGN_IN_FAILED)
