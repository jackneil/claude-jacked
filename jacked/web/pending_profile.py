"""The browser profile that receives a first provider sign-in (Add Account).

Add Account does not know the email until the provider CLI writes its
credential file, but the sign-in must still land in a profile that jacked
controls. The sign-in uses a pending profile. After the import,
``adopt_pending_profile`` moves that directory onto the real email.
"""

from __future__ import annotations

import logging

from jacked.web import browser_launch

logger = logging.getLogger(__name__)


def pending_login_account(provider: str) -> dict:
    """The identity of the profile that receives a first sign-in.

    >>> pending_login_account("grok")["email"]
    'pending@grok.login'
    """
    return {"email": f"pending@{provider}.login", "provider": provider, "id": "pending"}


def adopt_pending_profile(provider: str, email: str) -> None:
    """Move a first-sign-in profile onto ``email`` when that profile is not there yet.

    An existing profile for ``email`` is never replaced. The pending profile
    then stays, and the next first sign-in reuses it.
    """
    pending = browser_launch.profile_dir_for(pending_login_account(provider))
    real = browser_launch.profile_dir_for({"email": email, "provider": provider})
    if pending is None or real is None or pending == real:
        return
    if not pending.exists() or real.exists():
        return
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
        pending.rename(real)
    except OSError as exc:
        # The type only: the error text names the profile path (the email).
        logger.debug("Could not adopt the pending %s profile: %s", provider, type(exc).__name__)
