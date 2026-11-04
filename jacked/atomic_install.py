"""Install jacked's own artifacts in one step: a dir, a file copy or a link.

A copy that fails part way (a full disk, a file that became unwritable) must
leave the target as it was. So every install of one of jacked's own
artifacts makes the new copy beside the target and then swaps it into place
(#260i). A reader sees the complete old artifact or the complete new one,
never a mix of the two and never a partial copy.

* ``install_dir`` installs a whole directory: a skill dir for Claude Code or
  for Codex.
* ``atomic_copy_file``, ``atomic_symlink`` and ``atomic_hardlink`` install
  one file: an agent, a command, a prompt or a template. They use the shape
  of ``jacked.atomic_io``: a temp file or link ``.<name>.<random>.tmp``
  beside the target, then one replace.

``install_dir`` builds the new tree in a private staging dir beside the
target, then swaps that dir into place with renames.

A directory cannot replace a non-empty directory in one rename on POSIX.
So the swap has three steps:

1. Rename the old dir aside, to the previous dir.
2. Rename the staging dir to the target path.
3. Remove the previous dir.

When step 2 fails, the rollback renames the previous dir back. When any
step fails, the rollback removes the staging dir. Each rename uses
``jacked.atomic_io.replace_with_retry``, which retries a Windows sharing
refusal for a short time. A rename in the same directory is atomic on every
platform that jacked supports.

The private dirs are named ``.<name>.<tag>-<pid>`` beside the target:
``.<name>.jacked-staging-<pid>`` holds the new tree, and
``.<name>.jacked-previous-<pid>`` holds the old tree between steps 1 and 3.
The leading dot keeps the skill loaders of Claude Code and Codex from
loading a leftover as a skill. The pid of the process that made the dir lets
a later run tell the leftover of a dead process from an install that is
still running. The next install of the same name removes the leftovers of
this process and of each dead process.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

# The module, not its names: each rename then goes through the one
# ``atomic_io.replace_with_retry``, and a test can watch every rename there.
from jacked import atomic_io

logger = logging.getLogger(__name__)

STAGING_TAG = "jacked-staging"
PREVIOUS_TAG = "jacked-previous"
_LEFTOVER_TAGS = (STAGING_TAG, PREVIOUS_TAG)

T = TypeVar("T")

# ``on_rollback_error(error, step, path)``: ``step`` is "staging" (the staging
# dir at ``path`` could not be removed) or "previous" (the old tree at
# ``path`` could not be put back at the target path).
RollbackReport = Callable[[OSError, str, Path], None]


def present(path: Path) -> bool:
    """True when something is at ``path``: a file, a dir or a link. A broken
    link counts too.

    >>> present(Path("/nonexistent/jacked/atomic_install"))
    False
    """
    return path.exists() or path.is_symlink()


def side_dir(target: Path, tag: str, pid: int | None = None, *, name: str | None = None) -> Path:
    """The private dir ``.<name>.<tag>-<pid>`` beside ``target``.

    ``name`` is the name of ``target`` when none is given. ``pid`` is the
    pid of this process when none is given.

    >>> side_dir(Path("skills") / "qa", STAGING_TAG, 7).as_posix()
    'skills/.qa.jacked-staging-7'
    >>> side_dir(Path("skills") / "qa", PREVIOUS_TAG, 7).as_posix()
    'skills/.qa.jacked-previous-7'
    """
    name = target.name if name is None else name
    return target.parent / f".{name}.{tag}-{os.getpid() if pid is None else pid}"


def remove_tree(path: Path) -> None:
    """Remove the dir or link at ``path``.

    A link is unlinked and never followed, so the files that it points at
    stay. A real dir is removed with ``shutil.rmtree``, which removes the
    links inside it without following them. Raises OSError.
    """
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def _leftover_pid(entry_name: str, name: str) -> int | None:
    """The pid in the name of a leftover private dir of ``name``, or None
    when ``entry_name`` is not one.

    >>> _leftover_pid(".qa.jacked-staging-42", "qa")
    42
    >>> _leftover_pid(".qa.jacked-previous-notes", "qa") is None
    True
    >>> _leftover_pid(".qb.jacked-staging-42", "qa") is None
    True
    """
    for tag in _LEFTOVER_TAGS:
        prefix = f".{name}.{tag}-"
        if entry_name.startswith(prefix):
            pid_text = entry_name[len(prefix):]
            if pid_text.isascii() and pid_text.isdigit():
                return int(pid_text)
    return None


def clear_leftovers(
    target: Path, *, name: str | None = None, remove: Callable[[Path], None] | None = None,
) -> None:
    """Remove the private dirs that an earlier install of ``target`` left.

    A crash during the build leaves a staging dir. A crash between the two
    renames of the swap leaves a previous dir, which holds the old tree. A
    leftover is removed when its pid is this process or a process that is
    proven dead (`process_liveness` is False). The leftover of a live
    process belongs to an install that is still running, and an unknown
    answer proves nothing, so both stay. A removal that fails is logged at
    WARNING. ``install_dir`` then refuses to use a staging dir that is still
    there. Raises OSError when the parent dir cannot be listed.
    """
    from jacked.service.process import process_liveness

    name = target.name if name is None else name
    remove = remove_tree if remove is None else remove
    try:
        entries = list(target.parent.iterdir())
    except FileNotFoundError:
        return
    me = os.getpid()
    for entry in entries:
        pid = _leftover_pid(entry.name, name)
        if pid is None:
            continue
        if pid != me and process_liveness(pid) is not False:
            continue
        try:
            remove(entry)
        except OSError as e:
            logger.warning("Could not remove the leftover %s of %s: %s", entry, name, e)


def _log_rollback_error(error: OSError, step: str, path: Path) -> None:
    if step == "staging":
        logger.warning("Could not remove the staging dir %s: %s", path, error)
    else:
        logger.warning("Could not put the old tree back from %s: %s", path, error)


def _refuse_if_present(target: Path) -> None:
    # rename(2) replaces an EMPTY dir on POSIX. A dir that another writer put
    # at the target path is never replaced, removed or written into.
    if present(target):
        raise FileExistsError(errno.EEXIST, "The install path is in use", str(target))


def _roll_back(
    target: Path, staging: Path, previous: Path | None,
    remove: Callable[[Path], None], report: RollbackReport,
) -> None:
    """Remove the staging dir, then put the previous dir back when the target
    path is free. Each failure goes to ``report``; none is raised."""
    try:
        if present(staging):
            remove(staging)
    except OSError as e:
        report(e, "staging", staging)
    if previous is None:
        return
    try:
        _refuse_if_present(target)
        atomic_io.replace_with_retry(previous, target)
    except OSError as e:
        report(e, "previous", previous)


def install_dir(
    target: Path,
    build: Callable[[Path], T],
    *,
    replace_existing: bool = False,
    check: Callable[[Path], None] | None = None,
    name: str | None = None,
    remove: Callable[[Path], None] | None = None,
    on_rollback_error: RollbackReport | None = None,
) -> T:
    """Install the dir that ``build`` makes at ``target`` in one step.

    ``build(staging)`` writes the new tree into ``staging``, a NEW dir path
    beside ``target`` that does not exist yet. Its return value is the
    return value of this function. ``build`` never writes into ``target``.

    Set ``replace_existing`` when the caller decided, before the call, that
    the dir or link at ``target`` is jacked's own old copy. The swap then
    renames that copy aside, renames the staging dir into place, and
    removes the old copy. ``check(path)`` runs on the old copy immediately
    before it is renamed aside and again at its aside path. It raises to
    stop the swap. When ``replace_existing`` is False, or the old copy went
    away during the build, the staging dir is renamed to a free target path
    only. Something at the target path that the caller did not mark as
    replaceable raises FileExistsError and stays as it is.

    On any failure, the build's and the swap's included, the rollback
    removes the staging dir and puts the old copy back. Then the exception
    propagates unchanged. A rollback step that fails goes to
    ``on_rollback_error(error, step, path)`` (a WARNING log line when none
    is given). The old tree is never removed by a rollback: when it cannot
    go back, it stays at the previous dir. ``remove`` removes a private dir
    (``remove_tree`` when none is given). ``name`` names the private dirs
    (the name of ``target`` when none is given).
    """
    target = Path(target)
    name = target.name if name is None else name
    remove = remove_tree if remove is None else remove
    report = _log_rollback_error if on_rollback_error is None else on_rollback_error
    staging = side_dir(target, STAGING_TAG, name=name)
    clear_leftovers(target, name=name, remove=remove)
    if present(staging):
        raise FileExistsError(
            errno.EEXIST, "A leftover staging dir could not be removed", str(staging),
        )
    previous: Path | None = None
    try:
        result = build(staging)
        if replace_existing and present(target):
            if check is not None:
                check(target)
            aside = side_dir(target, PREVIOUS_TAG, name=name)
            atomic_io.replace_with_retry(target, aside)
            previous = aside
            if check is not None:
                # A change that landed between the check and the rename is in
                # the previous dir now. The rollback renames the dir back.
                check(aside)
        _refuse_if_present(target)
        atomic_io.replace_with_retry(staging, target)
    except BaseException:
        _roll_back(target, staging, previous, remove, report)
        raise
    if previous is not None:
        try:
            remove(previous)
        except OSError as e:
            # The new tree is live, so the install worked. The next install
            # of this name removes the leftover.
            logger.warning("Could not remove the previous copy of %s at %s: %s", name, previous, e)
    return result


def atomic_copy_file(src, dst, *, retry_budget: float = atomic_io.RETRY_BUDGET_SECONDS) -> None:
    """Replace ``dst`` with a copy of ``src`` in one step.

    The bytes go into a temp file ``.<name>.<random>.tmp`` beside ``dst``.
    The temp file gets the permission bits of ``src``, as ``shutil.copy``
    gives them. Then the temp file replaces ``dst`` with
    ``replace_with_retry``. A copy that fails part way (a full disk, an
    unreadable source) removes the temp file and leaves ``dst`` as it was.
    A link at ``dst`` is replaced by the copy and never written through.
    The parent dir of ``dst`` must exist, as for ``shutil.copy``.
    """
    target = Path(dst)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            shutil.copyfileobj(source, out)
        shutil.copymode(src, tmp_name)
        atomic_io.replace_with_retry(tmp_name, target, retry_budget=retry_budget)
    except BaseException:
        atomic_io._remove_temp(tmp_name, retry_budget)
        raise


def _link_atomically(make: Callable[[str], None], dst, retry_budget: float) -> None:
    """Make a link with ``make(temp_path)`` beside ``dst``, then replace
    ``dst`` with it in one step. A failure removes the temp link."""
    target = Path(dst)
    # os.symlink and os.link cannot make a link at a name that mkstemp
    # reserved, so the temp name is random. A name clash is tried again.
    for attempt in range(3):
        tmp_name = os.fspath(target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp")
        try:
            make(tmp_name)
            break
        except FileExistsError:
            if attempt == 2:
                raise
    try:
        atomic_io.replace_with_retry(tmp_name, target, retry_budget=retry_budget)
    except BaseException:
        atomic_io._remove_temp(tmp_name, retry_budget)
        raise


def atomic_symlink(
    src, dst, *, target_is_directory: bool = False,
    retry_budget: float = atomic_io.RETRY_BUDGET_SECONDS,
) -> None:
    """Replace ``dst`` with a symlink to ``src`` in one step.

    The link is made under a temp name ``.<name>.<random>.tmp`` beside
    ``dst``. Then it replaces ``dst`` with ``replace_with_retry``. When a
    step fails, the temp link is removed and ``dst`` stays as it was. The
    link holds ``src`` as it is given: give an absolute path for a link
    that must resolve from any dir. Raises OSError, for example when this
    process cannot make symlinks (Windows without Developer Mode).
    """
    _link_atomically(
        lambda tmp: os.symlink(src, tmp, target_is_directory=target_is_directory),
        dst, retry_budget,
    )


def atomic_hardlink(src, dst, *, retry_budget: float = atomic_io.RETRY_BUDGET_SECONDS) -> None:
    """Replace ``dst`` with a hard link to ``src`` in one step.

    The shape is the same as for ``atomic_symlink``. A hard link needs
    ``src`` and ``dst`` on the same volume. When they are not, OSError is
    raised and ``dst`` stays as it was.
    """
    _link_atomically(lambda tmp: os.link(src, tmp), dst, retry_budget)
