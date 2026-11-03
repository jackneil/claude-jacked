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

Calls into the jacked memory vault do not use this module: the vault is a
repository that jacked owns, and its sync needs remote access, which
``protocol.allow=never`` closes.
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping

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

#: The ``git config`` arguments that list the filter driver keys.
FILTER_CONFIG_ARGS = ("config", "-z", "--get-regexp", r"^filter\.")

# The keys that make a filter driver run a program, and the value that turns
# each one off. An empty command runs nothing, and a driver that is not
# required lets git use the file content as it is.
_FILTER_OFF = (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))


class GitGuardError(ValueError):
    """The environment cannot carry the filter overrides."""


def filter_drivers(config: str) -> list[str]:
    """Return the driver names in ``git config -z --get-regexp ^filter\\.`` output.

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
    drivers cannot be read (git failed, timed out or cannot start). Then the
    caller must not run a read that can use a filter. This function never
    raises; the reason goes to the DEBUG log.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            [git, "-C", cwd, *GIT_CONFIG_OVERRIDE_ARGS, *FILTER_CONFIG_ARGS],
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=timeout, creationflags=NO_WINDOW, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.debug("git guard: git config timed out (%s)", type(exc).__name__)
        return None
    except OSError as exc:
        logger.debug("git guard: git config did not run (%s)", type(exc).__name__)
        return None
    if proc.returncode not in (0, 1):  # 1: no filter key in the config
        logger.debug("git guard: git config exited %d", proc.returncode)
        return None
    drivers = filter_drivers(proc.stdout.decode("utf-8", errors="replace"))
    try:
        env.update(filter_overrides(env, drivers))
    except GitGuardError as exc:
        logger.debug("git guard: %s", exc)
        return None
    return env
