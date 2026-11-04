"""Make sure that no jacked upgrade path installs an older version (#295i).

Three paths install a new jacked build:

- ``jacked upgrade`` (inline on POSIX, a detached cmd.exe helper on Windows).
- The tray updater (``python -m jacked.service.updater`` on POSIX, a cmd.exe
  batch on Windows).
- The dashboard Upgrade button (``POST /api/upgrade``).

``uv tool install "claude-jacked[tray]" --force --refresh`` installs the
newest version that the package index serves, also when that version is
OLDER than the installed version. On 2026-11-04 the simple index served only
0.111.1 for a minute after the 0.111.2 release, and ``jacked upgrade`` moved
0.111.2 to 0.111.1. Each path uses this module in two layers:

1. Before the install, :func:`preinstall_check` reads the simple index (the
   index that uv reads). When the newest version that uv installs now is
   older than the installed version, the path does not run the install. It
   shows the :func:`downgrade_refusal` message.
2. :func:`guarded_upgrade_command` adds a version floor to the requirement
   (``claude-jacked[tray]>=<installed>``). Then uv itself cannot resolve to an
   older version, also when the index changes between the check and the
   install, or when uv reads a different index. ``jacked upgrade`` and the
   tray updater install with this command. The dashboard runs
   ``uv tool upgrade claude-jacked``, which cannot take a floor. That command
   keeps the installed build when the index serves only older versions
   (a live check with uv 0.10.2 gave "Nothing to upgrade").

This module is new in the release that adds it. Thus an older interpreter
that imports it during an upgrade reads it from disk, and it imports only
names that every supported release has.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The timeout of each index probe. The two probes run at the same time, so an
# unreachable index costs one timeout, not two.
PROBE_TIMEOUT_SECONDS = 5.0

# A normalized public PEP 440 version with no epoch: the only shape that a
# floor may have. The floor goes into a requirement and, on Windows, into a
# cmd.exe batch line, so it holds digits, letters of a pre-release tag and
# dots only.
_FLOOR_RE = re.compile(r"\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?")


@dataclass(frozen=True)
class IndexVersions:
    """What the package index names right now.

    ``target`` is the newest release that the JSON API or the simple index
    names (version_check.newest_listed_version). ``installable`` is the
    newest release that the simple index serves, which is the version that
    uv installs now. Each one is None when it is not known.
    """

    target: str | None = None
    installable: str | None = None


def _safe(version: str | None) -> str | None:
    """Return ``version`` when it is safe to put into a batch line, else None."""
    if not version:
        return None
    from jacked.install_method import safe_version_label

    return safe_version_label(version, fallback="") or None


def probe_index_versions(timeout: float = PROBE_TIMEOUT_SECONDS) -> IndexVersions:
    """Read the JSON API and the simple index at the same time.

    Best effort: a probe that fails logs its own failure and gives no answer.
    Offline, both fields are None and each caller continues as before. The
    version floor in the install command still prevents a downgrade.
    """
    from concurrent.futures import ThreadPoolExecutor

    from jacked import version_check

    probes = (
        version_check.get_latest_pypi_version,
        version_check.get_latest_from_simple_index,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        pypi_latest, simple_latest = pool.map(lambda probe: probe(timeout=timeout), probes)
    return IndexVersions(
        target=_safe(version_check.newest_listed_version(pypi_latest, simple_latest)),
        installable=_safe(version_check.newest_listed_version(None, simple_latest)),
    )


# The real probe, for tests: tests/conftest.py replaces probe_index_versions
# so that no test reaches PyPI, and this name lets the probe itself be tested.
_probe_index_versions_impl = probe_index_versions


def installed_version() -> str:
    """Return the claude-jacked version that is on disk now, or "" when unknown.

    ``jacked.__version__`` is the version that this interpreter imported.
    The distribution metadata is read from disk on each call, so it names the
    build that the next install replaces.
    """
    try:
        from importlib.metadata import version as dist_version

        return dist_version("claude-jacked")
    except Exception:
        return ""


def is_older(candidate: str | None, reference: str | None) -> bool:
    """True when ``candidate`` is an older version than ``reference``.

    Versions compare in PEP 440 order. An empty or missing version is never
    older and never newer.

    >>> is_older("0.111.1", "0.111.2"), is_older("0.111.2", "0.111.2")
    (True, False)
    >>> is_older("0.112.0", "0.111.2"), is_older(None, "0.111.2"), is_older("0.111.1", "")
    (False, False, False)
    >>> is_older("0.112.0rc1", "0.112.0"), is_older("0.112.0", "0.112.0.dev3")
    (True, False)
    """
    if not candidate or not reference:
        return False
    from packaging.version import InvalidVersion, Version

    try:
        return Version(candidate) < Version(reference)
    except InvalidVersion:
        from jacked.version_check import _parse_version_tuple

        return _parse_version_tuple(candidate) < _parse_version_tuple(reference)


def downgrade_refusal(installed: str | None, installable: str | None) -> str | None:
    """Return the refusal message when an install would move to an older version.

    None when the install may run: the index serves the installed version or
    a newer one, or either version is not known.

    >>> downgrade_refusal("0.111.2", "0.111.1")
    'Nothing to upgrade. The package index serves v0.111.1. The installed version is v0.111.2.'
    >>> downgrade_refusal("0.111.2", "0.111.2") is None  # same version: reinstall
    True
    >>> downgrade_refusal("0.111.1", "0.111.2") is None  # a real upgrade
    True
    >>> downgrade_refusal("0.111.2", None) is None  # the index did not answer
    True
    """
    if not is_older(installable, installed):
        return None
    return (
        f"Nothing to upgrade. The package index serves v{installable}. "
        f"The installed version is v{installed}."
    )


def preinstall_check(installed: str) -> tuple[IndexVersions, str | None]:
    """Probe the index, then decide whether the install may run.

    Returns the index answers and the refusal message (None when the install
    may run). Every install path calls this function before it runs the
    install command.
    """
    versions = probe_index_versions()
    return versions, downgrade_refusal(installed, versions.installable)


def _disk_version() -> str:
    """The version on disk, or the imported version when the metadata is missing."""
    import jacked

    return installed_version() or jacked.__version__


def append_update_log(line: str) -> None:
    """Add one line with a time stamp to the update log. Never raises.

    The update log (``~/.claude/jacked-update.log``) is the file that the tray
    and the updater both write.
    """
    try:
        import time

        from jacked.service.updater import UPDATE_LOG

        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(UPDATE_LOG, "a", encoding="utf-8") as log_fh:
            log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except Exception:
        logger.exception("Could not write the update log")


def current_refusal(log_source: str | None = None) -> str | None:
    """Run :func:`preinstall_check` for the build on disk. Never raises.

    Returns the refusal message, or None when the install may run. A check
    that cannot run returns None: the version floor in the install command
    still prevents a downgrade. With ``log_source``, a refusal also goes to
    the update log, after that name.
    """
    try:
        refusal = preinstall_check(_disk_version())[1]
    except Exception:
        logger.exception("The no-downgrade check failed")
        return None
    if refusal and log_source:
        append_update_log(f"{log_source}: REFUSED: {refusal}")
    return refusal


def version_floor(installed: str | None) -> str | None:
    """Return the version floor for the install requirement, or None.

    The floor is the public part of the installed version. A local label
    (``+abc``) is not permitted in a ``>=`` specifier (PEP 440). A version
    that is not valid, or that has an epoch, gets no floor.

    >>> version_floor("0.111.2"), version_floor("0.112.0rc1"), version_floor("0.112.0.dev3")
    ('0.111.2', '0.112.0rc1', '0.112.0.dev3')
    >>> version_floor("0.111.2+local"), version_floor("1!0.1"), version_floor("garbage")
    ('0.111.2', None, None)
    >>> version_floor("") is None
    True
    """
    if not installed:
        return None
    from packaging.version import InvalidVersion, Version

    try:
        parsed = Version(installed)
    except InvalidVersion:
        return None
    if parsed.epoch:
        return None
    floor = parsed.public
    return floor if _FLOOR_RE.fullmatch(floor) else None


def _requirement(extras: str) -> str:
    return f"claude-jacked[{extras}]"


def guarded_upgrade_command(extras: str, installed: str | None) -> list[str]:
    """Return install_method.upgrade_command with a version floor in the requirement.

    With the floor, the installer cannot resolve to a version older than
    ``installed``. With no usable floor (the installed version is not known),
    the command is the plain upgrade command. Raises ValueError for an
    install method that cannot upgrade itself, the same as upgrade_command,
    and when the upgrade command has no requirement to put the floor on.
    """
    from jacked.install_method import upgrade_command

    cmd = upgrade_command(extras)
    floor = version_floor(installed)
    if not floor:
        return cmd
    plain = _requirement(extras)
    if cmd.count(plain) != 1:
        raise ValueError(f"the upgrade command has no {plain} requirement for the version floor")
    return [f"{arg}>={floor}" if arg == plain else arg for arg in cmd]


def guarded_upgrade_command_label(extras: str, installed: str | None) -> str:
    """Return the human-readable form of :func:`guarded_upgrade_command`.

    Raises ValueError under the same conditions as
    install_method.upgrade_command_label.
    """
    from jacked.install_method import upgrade_command_label

    label = upgrade_command_label(extras)
    floor = version_floor(installed)
    if not floor:
        return label
    plain = f'"{_requirement(extras)}"'
    if label.count(plain) != 1:
        raise ValueError(f"the upgrade label has no {plain} requirement for the version floor")
    return label.replace(plain, f'"{_requirement(extras)}>={floor}"')


def guarded_upgrade_commands(extras: str) -> tuple[list[str], str]:
    """Return the argv and the label of the install, with the build on disk as the floor."""
    installed = _disk_version()
    return (
        guarded_upgrade_command(extras, installed),
        guarded_upgrade_command_label(extras, installed),
    )
