"""The private random identity of this machine.

The identity is ``MACHINE_ID_BYTES`` random bytes as lowercase hex, in
``<jacked home>/.claude/jacked-tests-machine-id``. It holds no part of the
host name. ``jacked.testdiet.model.MACHINE_ID_RE`` is the one definition of
its format: this module writes and reads that format, and the history
reader checks a stored identity with it.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from jacked.atomic_io import atomic_write_bytes
from jacked.packs import jacked_home
from jacked.regular_file import open_regular_fd
from jacked.testdiet.model import MACHINE_ID_BYTES, MACHINE_ID_RE

logger = logging.getLogger(__name__)

#: The file of the machine identity, in the ``.claude`` folder of the jacked home.
MACHINE_ID_NAME = "jacked-tests-machine-id"


def machine_id(home: Path | None = None) -> str | None:
    """Return the private random identity of this machine, and make it on first use.

    The identity is in ``<home>/.claude/jacked-tests-machine-id``. ``home``
    is the jacked home when it is not given. The file has mode 0600 where
    the system has modes. Two processes that make it at the same time get
    the same identity. The result is None when the file cannot be read or
    made, or holds no valid identity.
    """
    path = Path(home if home is not None else jacked_home()) / ".claude" / MACHINE_ID_NAME
    try:
        if not os.path.lexists(path):
            _make_machine_id(path)
        return _read_machine_id(path)
    except (OSError, ValueError) as exc:
        logger.debug("machine_id: no machine identity (%s)", type(exc).__name__)
        return None


def _make_machine_id(path: Path) -> None:
    """Make the file with a complete content, or keep the file that another process made.

    The value goes into a private temporary file first. A hard link then puts
    it at ``path``; the link fails when ``path`` exists, so the first process
    wins and no reader sees a partial file. Where the file system has no hard
    links, an atomic replace that checks for the file just before it is used.
    """
    value = secrets.token_hex(MACHINE_ID_BYTES)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    atomic_write_bytes(temp, (value + "\n").encode("ascii"), mode=0o600)
    try:
        os.link(temp, path)
    except FileExistsError:
        pass
    except OSError:
        # A file system with no hard links: an atomic replace that stops
        # when another process made the file first.
        _write_machine_id(path, value)
    finally:
        temp.unlink(missing_ok=True)


class _IdExists(Exception):
    """The machine identity file exists. The write stops."""


def _write_machine_id(path: Path, value: str) -> None:
    def refuse_when_made() -> None:
        if os.path.lexists(path):
            raise _IdExists

    try:
        atomic_write_bytes(path, (value + "\n").encode("ascii"), mode=0o600,
                           before_replace=refuse_when_made)
    except _IdExists:
        pass


def _read_machine_id(path: Path) -> str | None:
    fd = open_regular_fd(path)
    try:
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    value = data.decode("ascii").strip()
    return value if MACHINE_ID_RE.fullmatch(value) else None
