"""Remove a temporary folder of the recorder, and never raise.

``remove_tree_quietly`` is not ``jacked.atomic_install.remove_tree``: that
one raises ``OSError``. This one logs each path that stays and returns,
because a cleanup step of the recorder must never stop or change the gate.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def remove_tree_quietly(path: Path) -> None:
    """Remove the folder ``path`` and all that it holds. This function never raises.

    Git makes its loose objects read-only, and Windows does not delete a
    read-only file. So a path that cannot be removed gets write permission
    (and its folder too, when that folder is inside ``path``), and the
    removal tries again. A symbolic link never gets a new mode, because the
    mode change would go to its target. A path that still cannot be removed
    is logged at WARNING. A missing folder is not an error.
    """
    try:
        _remove_tree(Path(path), os.path.abspath(path), nested=False)
    except Exception as exc:  # noqa: BLE001 -- a cleanup helper must never raise
        logger.warning("remove_tree: cannot remove %s (%s)", path, type(exc).__name__)


def _remove_tree(path: Path, root: str, *, nested: bool) -> None:
    def retry(func, failed, exc) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            failed = os.fspath(failed)
            parent = os.path.dirname(os.path.abspath(failed))
            # Only a folder inside ``path`` gets a new mode, never the folder that holds it.
            if parent == root or parent.startswith(root + os.sep):
                os.chmod(parent, stat.S_IRWXU)
            if not os.path.islink(failed) and os.path.lexists(failed):
                is_dir = os.path.isdir(failed)
                os.chmod(failed, stat.S_IRWXU if is_dir else stat.S_IREAD | stat.S_IWRITE)
            if func in (os.unlink, os.remove, os.rmdir):
                func(failed)
            elif not nested and os.path.isdir(failed) and not os.path.islink(failed):
                # rmtree could not open or list this folder. Remove it one more time.
                _remove_tree(Path(failed), root, nested=True)
            else:
                logger.warning("remove_tree: cannot remove %s (%s)", failed, type(exc).__name__)
        except FileNotFoundError:
            return
        except Exception as again:  # noqa: BLE001 -- a cleanup helper must never raise
            logger.warning("remove_tree: cannot remove %s (%s)", failed, type(again).__name__)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=lambda func, failed, info: retry(func, failed, info[1]))
