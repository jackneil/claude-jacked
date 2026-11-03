"""Read the lines of a JSONL file (a Claude Code transcript) with a bound.

Standard library only, so ``jacked.recover`` stays usable on a bare install.
A line over ``LINE_LIMIT`` bytes is read in small pieces and skipped, so a
reader never holds it whole. A line that is not UTF-8 is skipped too.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The longest transcript line that is parsed. A record holds what one API
# request or response held, and the Messages API refuses a request over 32 MB,
# so a real record is shorter. A longer line is read in small pieces and
# skipped (with a DEBUG log line), so no reader ever holds it whole.
LINE_LIMIT = 32 * 1024 * 1024
_SKIP_CHUNK = 1 << 20


def iter_lines(f):
    """Yield each line of the binary file ``f`` as stripped text.

    The line is read in pieces of ``_SKIP_CHUNK`` bytes. A line over
    ``LINE_LIMIT`` bytes (the newline not counted), or a line that is not
    UTF-8, yields None: the caller skips it, and the read goes on at the next
    line. The pieces of a long line are dropped as they are read.
    """
    while True:
        buf: bytearray | None = bytearray()
        size = 0
        while part := f.readline(_SKIP_CHUNK):
            size += len(part)
            ends = part.endswith(b"\n")
            if buf is not None:
                if size - ends > LINE_LIMIT:
                    buf = None
                else:
                    buf += part
            if ends:
                break
        if not size:
            return
        if buf is None:
            logger.debug(
                "jsonl: skipped a line of %d bytes (the limit is %d)", size, LINE_LIMIT
            )
            yield None
            continue
        try:
            yield buf.decode("utf-8").strip()
        except UnicodeDecodeError:
            yield None


def iter_records(path: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/garbled
    lines. Tolerates a crash-truncated final line (it is simply skipped).
    A line over ``LINE_LIMIT`` bytes or not in UTF-8 is skipped too."""
    try:
        with open(path, "rb") as f:
            for line in iter_lines(f):
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        return


def iter_objects(f):
    """Yield ``(line number, object)`` for each non-blank line of the binary file ``f``.

    ``object`` is the parsed JSON object (a dict), or None for a line that is
    too long, not UTF-8, not JSON, or JSON that is not an object. A blank line
    yields nothing, but it counts in the line numbers.
    """
    for number, line in enumerate(iter_lines(f), 1):
        if line == "":
            continue
        try:
            data = json.loads(line) if line is not None else None
        except json.JSONDecodeError:
            data = None
        yield number, data if isinstance(data, dict) else None
