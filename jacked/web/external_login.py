"""Start a provider CLI sign-in so the IdP session lands in the account's profile.

Claude re-auth opens an authorize URL that jacked built, in the account
profile. A provider CLI (Grok) builds its own sign-in URL. jacked runs that
CLI with ``login --device-auth``, reads the first https URL that the CLI
prints, and opens it in the account's browser profile. The profile keeps
the IdP session, so the next re-auth is one confirmation.

On macOS and Linux, jacked also points ``$BROWSER`` at its profile opener,
for a CLI that opens a browser by itself. Windows has no ``sh``, so there
the URL watcher is the only path.

The device URL can carry the one-time user code. It is never logged.

Only Grok is wired to this today. Codex prints its one-time code apart from
the URL, and the dashboard does not show that code yet, so Codex keeps the
terminal ``codex login`` flow.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from jacked.findbin import find_bin
from jacked.web.browser_launch import profile_dir_for

logger = logging.getLogger(__name__)

_LOGIN_ARGS = {
    "grok": ("login", "--device-auth"),
}
# ANSI color sequences. Codex wraps its URL in them, and an escape kept in the
# match makes the URL invalid.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https://[^\s\"'<>\x1b]+")
_WATCH_SECONDS = 90


class LoginStartError(Exception):
    """The provider login could not be started."""


def _write_opener(profile_dir: Path) -> Optional[Path]:
    """Write a ``$BROWSER`` program that opens its ``$1`` in ``profile_dir``.

    Returns None on Windows, where a ``sh`` script cannot run.
    """
    if os.name == "nt":
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass
    script = profile_dir / ".jacked-open-url"
    body = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -m jacked.web.profile_opener "
        f"{shlex.quote(str(profile_dir))} \"$1\"\n"
    )
    script.write_text(body, encoding="utf-8")
    try:
        script.chmod(0o700)
    except OSError:
        pass
    return script


def _first_url(line: str) -> Optional[str]:
    """The first https URL in ``line``, with ANSI color codes removed.

    >>> _first_url("  \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m")
    'https://auth.openai.com/codex/device'
    >>> _first_url("no link here") is None
    True
    """
    match = _URL_RE.search(_ANSI_RE.sub("", line))
    return match.group(0) if match else None


def _watch_for_url(proc, account: dict, db) -> None:
    """Open the first https URL that the CLI prints, in the account profile."""
    from jacked.web.browser_launch import open_auth_url

    opened = False
    deadline = time.time() + _WATCH_SECONDS
    stream = proc.stdout
    try:
        while stream is not None and time.time() < deadline:
            line = stream.readline()
            if not line:
                # End of output. readline() returns "" at once from here on,
                # so a loop that waits for poll() would spin a core.
                break
            if opened:
                continue
            url = _first_url(line)
            if url is None:
                continue
            open_auth_url(url, account, db)
            opened = True
            logger.info(
                "Opened %s sign-in in the account browser profile",
                account.get("provider") or "?",
            )
    except Exception:
        logger.debug("Provider login URL watch failed", exc_info=True)


def start_provider_login(
    provider: str,
    account: dict,
    db,
    *,
    env: Optional[Mapping[str, str]] = None,
    binary: Optional[str] = None,
) -> dict:
    """Start the ``provider`` CLI sign-in and aim its browser at this account's profile.

    Returns at once. The CLI keeps running until the user finishes the
    sign-in. Raises ``LoginStartError`` when the provider has no CLI sign-in
    that jacked can open, or when its CLI is not installed.
    """
    provider_name = (provider or "").strip().lower()
    login_args = _LOGIN_ARGS.get(provider_name)
    if login_args is None:
        raise LoginStartError(f"jacked cannot open a {provider} sign-in in a browser profile.")

    resolved = binary or find_bin(provider_name)
    if not resolved:
        raise LoginStartError(
            f"The {provider_name} CLI is not installed. Install it, then try again."
        )

    target = {**account, "provider": provider_name}
    profile_dir = profile_dir_for(target)
    if profile_dir is None:
        raise LoginStartError(
            "This account has no email, so jacked has no profile to keep the sign-in in."
        )

    run_env = dict(env if env is not None else os.environ)
    opener = _write_opener(profile_dir)
    if opener is not None:
        run_env["BROWSER"] = str(opener)
    proc = subprocess.Popen(  # noqa: S603 - argv is the resolved CLI plus fixed login args
        [resolved, *login_args],
        env=run_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    threading.Thread(
        target=_watch_for_url,
        args=(proc, target, db),
        daemon=True,
        name=f"jacked-{provider_name}-login",
    ).start()
    logger.info("Started %s sign-in (pid %s)", provider_name, proc.pid)
    return {
        "profile_login": True,
        "provider": provider_name,
        "message": (
            f"The {provider_name.capitalize()} sign-in opened in this account's browser "
            "profile. Finish the sign-in there. The profile keeps that sign-in for the "
            "next re-auth."
        ),
    }
