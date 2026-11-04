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

A watcher thread reads the CLI output to its end, so a chatty CLI never
blocks on a full pipe. When the CLI exits, the watcher reports the outcome to
the caller, which imports the new login. A login that runs longer than the
device code lives is stopped.

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
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from jacked.findbin import find_bin
from jacked.web.browser_launch import profile_dir_for

from jacked.public_errors import PublicError

logger = logging.getLogger(__name__)

_LOGIN_ARGS = {
    "grok": ("login", "--device-auth"),
}
# ANSI color sequences. Codex wraps its URL in them, and an escape kept in the
# match makes the URL invalid.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https://[^\s\"'<>\x1b]+")
# xAI device codes expire after 15 minutes. The watcher stops a login that
# runs longer, with one minute of margin for a slow final poll.
LOGIN_TIMEOUT_SECONDS = 16 * 60
# After the end of the output, how long the CLI gets to exit.
_EXIT_GRACE_SECONDS = 30

# Outcomes that ``on_exit`` receives.
SIGNED_IN = "signed_in"
FAILED = "failed"
TIMED_OUT = "timed_out"
NO_BROWSER = "no_browser"

# The one live login process of each provider. A second sign-in would write
# the same credential file, so it is refused while the first one runs.
_live_logins: dict[str, subprocess.Popen] = {}
_live_lock = threading.Lock()


class LoginStartError(PublicError):
    """The provider login could not be started.

    Every message is authored text that is safe to show a dashboard client.
    """


class LoginInProgressError(LoginStartError):
    """A sign-in for this provider is still running."""


def login_running(provider: str) -> bool:
    """True while a sign-in that jacked started for ``provider`` runs.

    >>> login_running("nobody")
    False
    """
    with _live_lock:
        proc = _live_logins.get(provider)
        return proc is not None and proc.poll() is None


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


def _watch_for_url(proc, account: dict, db) -> bool:
    """Read the CLI output to its end. Open the first https URL in the profile.

    The output is read until the CLI closes it, so a CLI that prints more
    lines never blocks on a full pipe. Returns False when a URL was printed
    and no browser could open it, else True.
    """
    from jacked.web.browser_launch import open_auth_url

    opened = False
    usable = True
    stream = proc.stdout
    try:
        while stream is not None:
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
            opened = True
            result = open_auth_url(url, account, db)
            if getattr(result, "mode", None) == "manual":
                # Nobody can see the URL, so the sign-in cannot finish.
                usable = False
                _stop(proc)
                continue
            logger.info(
                "Opened %s sign-in in the account browser profile",
                account.get("provider") or "?",
            )
    except Exception:
        logger.debug("Provider login URL watch failed", exc_info=True)
    return usable


def _stop(proc) -> None:
    """Stop the login process that jacked started (by its own handle)."""
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        logger.debug("Could not stop a provider login", exc_info=True)


def _watch_login(proc, account: dict, db, provider: str, on_exit) -> None:
    """Follow one login process to its end, then report how it ended.

    A timer stops the process after ``LOGIN_TIMEOUT_SECONDS``. ``on_exit``
    gets one of SIGNED_IN, FAILED, TIMED_OUT or NO_BROWSER. The process stays
    registered as the live login until ``on_exit`` returns, so a new sign-in
    cannot start while the result is imported.
    """
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        _stop(proc)

    timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, _expire)
    timer.daemon = True
    timer.start()
    try:
        usable = _watch_for_url(proc, account, db)
        try:
            code = proc.wait(timeout=_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _stop(proc)
            code = proc.wait()
    finally:
        timer.cancel()
    if not usable:
        outcome = NO_BROWSER
    elif timed_out.is_set():
        outcome = TIMED_OUT
    else:
        outcome = SIGNED_IN if code == 0 else FAILED
    logger.info("%s sign-in ended: %s", provider, outcome)
    try:
        if on_exit is not None:
            on_exit(outcome)
    except Exception:
        logger.warning("Handling the end of the %s sign-in failed", provider, exc_info=True)
    finally:
        with _live_lock:
            if _live_logins.get(provider) is proc:
                del _live_logins[provider]


def start_provider_login(
    provider: str,
    account: dict,
    db,
    *,
    env: Optional[Mapping[str, str]] = None,
    binary: Optional[str] = None,
    prepare: Optional[Callable[[], tuple[Mapping[str, str], Sequence[str]]]] = None,
    on_exit: Optional[Callable[[str], None]] = None,
) -> dict:
    """Start the ``provider`` CLI sign-in and aim its browser at this account's profile.

    Returns at once. The CLI keeps running until the user finishes the
    sign-in, or until ``LOGIN_TIMEOUT_SECONDS``. Only one sign-in for each
    provider runs at a time.

    ``prepare`` runs after the check for a running sign-in and before the
    start. It returns more environment values and more CLI arguments, for
    example an isolated home for the new login. ``on_exit`` gets the outcome
    (``SIGNED_IN``, ``FAILED``, ``TIMED_OUT`` or ``NO_BROWSER``) on the
    watcher thread.

    Raises ``LoginInProgressError`` when a sign-in for ``provider`` still
    runs. Raises ``LoginStartError`` when the provider has no CLI sign-in that
    jacked can open, or when its CLI is not installed.
    """
    provider_name = (provider or "").strip().lower()
    login_args = _LOGIN_ARGS.get(provider_name)
    if login_args is None:
        raise LoginStartError(f"jacked cannot open a {provider} sign-in in a browser profile.")

    # find_bin applies the trusted-path rule to every off-PATH candidate and
    # skips a match in an unlisted current folder on Windows.
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
    with _live_lock:
        running = _live_logins.get(provider_name)
        if running is not None and running.poll() is None:
            raise LoginInProgressError(
                f"A {provider_name.capitalize()} sign-in is already open. Finish it or "
                "wait for it to end, then try again."
            )
        extra_args: Sequence[str] = ()
        if prepare is not None:
            extra_env, extra_args = prepare()
            run_env.update(extra_env)
        opener = _write_opener(profile_dir)
        if opener is not None:
            run_env["BROWSER"] = str(opener)
        proc = subprocess.Popen(  # noqa: S603 - argv is the resolved CLI plus fixed login args
            [resolved, *login_args, *extra_args],
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        _live_logins[provider_name] = proc
    threading.Thread(
        target=_watch_login,
        args=(proc, target, db, provider_name, on_exit),
        daemon=True,
        name=f"jacked-{provider_name}-login",
    ).start()
    logger.info("Started %s sign-in (pid %s)", provider_name, proc.pid)
    return {
        "profile_login": True,
        "provider": provider_name,
        "message": (
            f"The {provider_name.capitalize()} sign-in opened in this account's browser "
            "profile. Finish the sign-in there. jacked adds the account when the "
            "sign-in is done."
        ),
    }
