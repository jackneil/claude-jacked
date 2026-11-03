"""The integrity baseline file: its path, its lock, its shape, its reads and writes (#99i).

``jacked.integrity`` compares and reports, and re-exports the public names of
this module. The document is ``<settings folder>/jacked/integrity.json``:
``{"version", "baseline_time", "baseline", "log"}``. A read accepts only the
exact shape that ``write`` gives; every other shape raises
``BaselineUnreadableError``, so no later read can fail on a wrong type.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from jacked.regular_file import FileTooLargeError, read_regular

logger = logging.getLogger(__name__)

BASELINE_VERSION = 1
#: The largest baseline file that a read accepts. Each log entry is about 100
#: bytes, so this is many years of writes. A larger file reads as unreadable,
#: the finding says so, and ``jacked integrity accept`` starts a new one.
BASELINE_MAX_BYTES = 64 * 1024 * 1024


class BaselineUnreadableError(Exception):
    """The baseline file exists but is not valid."""


def baseline_path(settings_file: Path | str) -> Path:
    """``<settings folder>/jacked/integrity.json``.

    >>> baseline_path(Path("/h/.claude/settings.json")).as_posix()
    '/h/.claude/jacked/integrity.json'
    """
    return Path(settings_file).parent / "jacked" / "integrity.json"


_HELD = threading.local()


@contextlib.contextmanager
def baseline_lock(settings_file: Path | str):
    """Hold the lock of the baseline beside ``settings_file`` for the block.

    jacked writes settings.json from the CLI, the service and hook processes.
    Without the lock, two writes interleave their snapshots and their
    baseline updates: a log entry is lost, and the change of one write shows
    as a change from outside jacked. The lock is the shared advisory file lock
    of the memory vault (``vault._file_lock``). A thread that holds it can
    take it again, so ``write_settings`` can hold it around ``rebaseline``.
    """
    from jacked.memory import vault

    lock = baseline_path(settings_file).with_name("integrity.json.lock")
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = set()
    if str(lock) in held:
        yield
        return
    with vault._file_lock(lock):
        held.add(str(lock))
        try:
            yield
        finally:
            held.discard(str(lock))


def load_baseline(settings_file: Path | str) -> dict | None:
    """The baseline document, or None when there is none. Raises when it is not valid."""
    path = baseline_path(settings_file)
    try:
        raw = read_regular(path, BASELINE_MAX_BYTES)
    except FileNotFoundError:
        return None
    except FileTooLargeError as exc:
        logger.warning("integrity: %s is %d bytes, over the limit of %d", path.name, exc.size, BASELINE_MAX_BYTES)
        raise BaselineUnreadableError(f"{path.name} is too large") from exc
    except OSError as exc:
        raise BaselineUnreadableError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BaselineUnreadableError(f"{path.name} is not valid JSON") from exc
    if not _valid_document(doc):
        raise BaselineUnreadableError(f"{path.name} has an unknown shape")
    return doc


def _str_map(value) -> bool:
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())


def _is_time(value) -> bool:
    """True for an ISO 8601 time string, the form that ``log_entry`` writes."""
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _valid_document(doc) -> bool:
    """True when ``doc`` has the exact shape that ``_write`` gives, nested types included.

    Every other shape is unreadable, so no read of a baseline can fail later
    on a wrong type, and every surface shows the same finding.
    """
    if not isinstance(doc, dict) or doc.get("version") != BASELINE_VERSION or not _is_time(doc.get("baseline_time")):
        return False
    base, log = doc.get("baseline"), doc.get("log")
    if not isinstance(base, dict) or not isinstance(base.get("file"), str) or not isinstance(log, list):
        return False
    if not (_str_map(base.get("scripts")) and _str_map(base.get("settings"))):
        return False
    hooks = base.get("hooks")
    if not isinstance(hooks, dict) or not all(isinstance(m, dict) for m in hooks.values()):
        return False
    for matchers in hooks.values():
        for items in matchers.values():
            if not isinstance(items, list) or not all(
                isinstance(i, dict) and isinstance(i.get("command"), str) and isinstance(i.get("digest"), str)
                for i in items
            ):
                return False
    return all(isinstance(e, dict) and _is_time(e.get("time")) and isinstance(e.get("reason"), str) for e in log)


def log_entry(reason: str, **extra) -> dict:
    from jacked import __version__

    when = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"time": when, "reason": reason, "jacked_version": __version__, **extra}


def write(settings_file: Path, baseline: dict, log: list, baseline_time: str) -> None:
    """Write the baseline document. ``baseline_time`` is the time of the last rebaseline.

    It is a field of its own, so an event in the log (a quarantine) never
    moves the time that ``status`` shows.
    """
    from jacked.atomic_io import atomic_write_text

    doc = {"version": BASELINE_VERSION, "baseline_time": baseline_time, "baseline": baseline, "log": log}
    atomic_write_text(baseline_path(settings_file), json.dumps(doc, indent=2))
