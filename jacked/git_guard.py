"""Run git in a checkout that can be untrusted, without running its programs.

A repository config can name programs that a git read runs: a
``core.fsmonitor`` command, hooks under ``core.hooksPath``, a GPG program for
``log.showSignature``, and the clean, smudge or process command of a filter
driver, which ``git status`` and ``git diff`` run when they read the content
of a file. A hook runs git with the session folder as its cwd, and that
folder can be a checkout that someone else made.

Two parts close those paths:

- ``GIT_CONFIG_OVERRIDE_ARGS`` goes after the git binary on every call. A
  ``-c`` value beats the repository config.
- ``guarded_git_env`` reads every filter driver name from the config (one
  ``git config`` call, which only reads files) and returns an environment
  that turns each driver off. A list of known driver names cannot do this,
  because the repository picks the names.

``run_git`` runs each of these reads. It gives the output to a consumer in
chunks, so a huge commit message or a huge config never sits in memory
whole, and the whole call ends within its timeout.

Calls into the jacked memory vault do not use this module: the vault is a
repository that jacked owns, and its sync needs remote access, which
``protocol.allow=never`` closes.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Protocol

from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

#: The ``-c`` overrides for each git call in a checkout that can be untrusted.
#: The untracked cache goes off with the fsmonitor that feeds it, and
#: ``protocol.allow=never`` closes remote access, which no read needs.
GIT_CONFIG_OVERRIDE_ARGS = (
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "log.showSignature=false",
    "-c", "protocol.allow=never",
)

#: The ``git config`` arguments that list every config key, filter drivers
#: included. ``--get-regexp '^filter\\.'`` is not used: git matches it with the
#: C library regex under the process locale, and under a UTF-8 locale a key
#: whose subsection holds a byte that is not valid UTF-8 never matches, so
#: that driver stays enabled while the exit code is 0. ``--list`` emits every
#: key byte for byte; ``filter_drivers`` does the prefix test itself.
FILTER_CONFIG_ARGS = ("config", "-z", "--list")

# The keys that make a filter driver run a program, and the value that turns
# each one off. An empty command runs nothing, and a driver that is not
# required lets git use the file content as it is.
_FILTER_OFF = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))


#: The most bytes of git output that ``Capture`` keeps. The reads that use it
#: print a few lines (rev-parse, branch, config, a file list), so a larger
#: output is not a real answer: the reader stops, git is ended and the caller
#: sees ``truncated``. The log and the status of git reality read with their
#: own consumers, which keep less per record or only counts, so they never
#: need the whole output in memory.
OUTPUT_LIMIT = 1024 * 1024
_READ_CHUNK = 64 * 1024
# After git is ended, how long the reader thread can take to see the end of
# the pipe before it is left behind (a daemon thread).
_REAP_S = 1.0


class GitGuardError(ValueError):
    """The environment cannot carry the filter overrides."""


class Consumer(Protocol):
    """Takes the stdout of a git call as it comes, in chunks."""

    def feed(self, chunk: bytes) -> bool:
        """Take the next chunk. Return False to stop the read and end git."""

    def close(self) -> None:
        """The output ended (EOF). It is not called after ``feed`` returned False."""


class Capture:
    """Keep the first ``limit`` bytes (default ``OUTPUT_LIMIT``) of an output.

    >>> c = Capture(4); c.feed(b"abc"), c.feed(b"def"), bytes(c.data), c.truncated
    (True, False, b'abcd', True)
    """

    def __init__(self, limit: int | None = None) -> None:
        self.limit = OUTPUT_LIMIT if limit is None else limit
        self.data = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> bool:
        room = self.limit - len(self.data)
        if len(chunk) > room:
            self.data += chunk[:room]
            self.truncated = True
            return False
        self.data += chunk
        return True

    def close(self) -> None:
        return None


def run_git(
    argv: list[str], *, name: str, env: Mapping[str, str], timeout: float,
    consumer: Consumer, cwd: str | None = None,
) -> int:
    """Run git and give its stdout to ``consumer`` in chunks. Return the exit code.

    The output is never held whole: ``consumer`` keeps what it needs. When
    ``consumer.feed`` returns False, the read stops and git is ended; the
    exit code is then the code of the ended process, and the consumer says
    why it stopped. stderr goes to ``os.devnull``, because no caller reads
    it. The whole call ends within ``timeout`` seconds: a slow git is ended
    and ``subprocess.TimeoutExpired`` is raised. ``name`` is the fixed
    subcommand name for the DEBUG log, which never holds the argv, a path
    or the output. An ``OSError`` from the start of git goes up.
    """
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )
    state: dict = {"stopped": False, "read": 0, "error": None}

    def pump() -> None:
        try:
            while chunk := proc.stdout.read1(_READ_CHUNK):
                state["read"] += len(chunk)
                if not consumer.feed(chunk):
                    state["stopped"] = True
                    return
            consumer.close()
        except Exception as exc:  # noqa: BLE001 -- raised again in the caller thread
            state["error"] = exc

    reader = threading.Thread(target=pump, name=f"git-{name}-output", daemon=True)
    reader.start()
    try:
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            raise subprocess.TimeoutExpired("git", timeout)
        if state["error"] is not None:
            raise state["error"]
        if state["stopped"]:
            logger.debug("git guard: stopped the output of git %s after %d bytes",
                         name, state["read"])
            proc.kill()
        return proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(_REAP_S)
        if not reader.is_alive():
            proc.stdout.close()


def filter_drivers(config: str) -> list[str]:
    """Return the driver names in ``git config -z --list`` output.

    Each entry is "<key>LF<value>" and ends with NUL. The driver name is the
    part between "filter." and the last dot, so it can hold "=" and ".".

    >>> filter_drivers("filter.a=b.c.clean\\n/x\\0filter.a=b.c.required\\ntrue\\0")
    ['a=b.c']
    """
    drivers: list[str] = []
    for entry in config.split("\0"):
        key = entry.split("\n", 1)[0]
        if not key.startswith("filter."):
            continue
        name = key[len("filter."):].rpartition(".")[0]
        if name and name not in drivers:
            drivers.append(name)
    return drivers


def filter_keys(raw: bytes) -> bytes:
    """Return only the filter keys of the ``git config -z --list`` output ``raw``.

    Only a filter key goes into the environment, so only a filter key must
    pass through it. Any other entry, and the value of a filter key, stays
    out: a user.name in Latin-1 must not make a read fail closed.

    >>> filter_keys(b"user.name\\nJos\\xe9\\0filter.a.clean\\n/\\xff\\0filter.a.required\\0")
    b'filter.a.clean\\x00filter.a.required'
    """
    entries = (entry.split(b"\n", 1)[0] for entry in raw.split(b"\0"))
    return b"\0".join(key for key in entries if key.startswith(b"filter."))


def config_text(raw: bytes) -> str | None:
    """Return the filter keys ``raw`` (``filter_keys``) as text that gives back the same bytes.

    ``subprocess`` encodes each environment key and value with
    ``os.fsencode``, so the text comes from ``os.fsdecode`` (on POSIX: UTF-8
    with ``surrogateescape``). A driver name that is not UTF-8 then reaches
    git byte for byte. Return None when the text does not give back ``raw``
    (on Windows a name that is not UTF-8 cannot pass), so the caller fails
    closed. A lossy decode would make an override for a different name, and
    git would run the real driver.

    >>> config_text(b"filter.a.clean\\n/x\\0")
    'filter.a.clean\\n/x\\x00'
    """
    try:
        text = os.fsdecode(raw)
        same = os.fsencode(text) == raw
    except (UnicodeError, ValueError):
        return None
    return text if same else None


def filter_overrides(env: Mapping[str, str], drivers: list[str]) -> dict[str, str]:
    """Return the environment that turns off each filter driver in ``drivers``.

    git reads ``GIT_CONFIG_COUNT`` and the ``GIT_CONFIG_KEY_<n>`` and
    ``GIT_CONFIG_VALUE_<n>`` pairs as command-line config. A key in a pair can
    hold any character, but a ``-c key=value`` argument cannot hold "=" in
    the key. The new pairs go after the pairs that ``env`` already has, so
    those pairs stay. An inherited count that is not a number raises
    ``GitGuardError``.

    >>> filter_overrides({}, ["x"])["GIT_CONFIG_KEY_3"]
    'filter.x.required'
    """
    if not drivers:
        return {}
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        raise GitGuardError("GIT_CONFIG_COUNT is not a number") from None
    out: dict[str, str] = {}
    for driver in drivers:
        for field, value in _FILTER_OFF:
            out[f"GIT_CONFIG_KEY_{n}"] = f"filter.{driver}.{field}"
            out[f"GIT_CONFIG_VALUE_{n}"] = value
            n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out


def guarded_git_env(
    git: str, cwd: str, base_env: Mapping[str, str] | None = None, *, timeout: float = 3
) -> dict[str, str] | None:
    """Return the environment for a git read in ``cwd``, or None.

    The environment is ``base_env`` (default ``os.environ``) with
    ``GIT_OPTIONAL_LOCKS=0`` and the overrides for every filter driver of the
    config. The driver list comes from one ``git config`` call, with the
    ``-c`` overrides and without the filter environment. None means that the
    drivers cannot be read (git failed, timed out, cannot start, or printed
    more than ``OUTPUT_LIMIT`` bytes, so the list can be incomplete). Then the
    caller must not run a read that can use a filter. This function never
    raises; the reason goes to the DEBUG log.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    out = Capture()
    try:
        code = run_git(
            [git, "-C", cwd, *GIT_CONFIG_OVERRIDE_ARGS, *FILTER_CONFIG_ARGS],
            name="config", env=env, timeout=timeout, consumer=out,
        )
    except subprocess.TimeoutExpired as exc:
        logger.debug("git guard: git config timed out (%s)", type(exc).__name__)
        return None
    except OSError as exc:
        logger.debug("git guard: git config did not run (%s)", type(exc).__name__)
        return None
    if out.truncated:
        logger.debug("git guard: git config printed more than %d bytes", OUTPUT_LIMIT)
        return None
    if code not in (0, 1):  # 1: no filter key in the config
        logger.debug("git guard: git config exited %d", code)
        return None
    config = config_text(filter_keys(bytes(out.data)))
    if config is None:
        logger.debug("git guard: a filter key cannot pass through the environment")
        return None
    drivers = filter_drivers(config)
    try:
        env.update(filter_overrides(env, drivers))
    except GitGuardError as exc:
        logger.debug("git guard: %s", exc)
        return None
    return env
