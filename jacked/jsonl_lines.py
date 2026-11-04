"""Read the lines of a JSONL file (a Claude Code transcript) with a bound.

Standard library only, so ``jacked.recover`` stays usable on a bare install.
A line over ``LINE_LIMIT`` bytes is read in small pieces and skipped, so a
reader never holds it whole. A line that is not UTF-8, or that is nested
too deep for the parser, is skipped too.
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
    ``LINE_LIMIT`` bytes (the LF not counted), or a line that is not UTF-8,
    yields None: the caller skips it, and the read goes on at the next line.
    The limit is the bytes on disk, so the CR of a CRLF line counts toward
    it. The limit is 32 MiB, so this one byte changes nothing for a real
    record. The pieces of a long line are dropped as they are read. Each skip
    goes to the DEBUG log with the line number, never with the content.
    """
    number = 0
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
        number += 1
        if buf is None:
            logger.debug(
                "jsonl: skipped line %d of %d bytes (the limit is %d)", number, size, LINE_LIMIT
            )
            yield None
            continue
        try:
            text = buf.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            logger.debug("jsonl: skipped line %d (%s)", number, type(exc).__name__)
            text = None
        yield text


def parse_line(line: str | None, number: int) -> tuple[bool, object]:
    """Return ``(True, value)`` for a JSON line, else ``(False, None)``.

    ``line`` comes from ``iter_lines`` (None is a line it skipped), and
    ``number`` is its line number. A line that the parser cannot take
    because it is nested too deep (``RecursionError``) is malformed too: the
    read goes on at the next line, and the skip goes to the DEBUG log.

    >>> parse_line('{"a": 1}', 1), parse_line("{", 2), parse_line("[" * 100_000, 3)
    ((True, {'a': 1}), (False, None), (False, None))
    """
    if line is None:
        return False, None
    try:
        return True, json.loads(line)
    except json.JSONDecodeError:
        return False, None
    except RecursionError:
        logger.debug("jsonl: skipped line %d, nested too deep to parse", number)
        return False, None


def iter_records(path: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/garbled
    lines. Tolerates a crash-truncated final line (it is simply skipped).
    A line over ``LINE_LIMIT`` bytes, not in UTF-8 or nested too deep to
    parse is skipped too."""
    try:
        with open(path, "rb") as f:
            for number, line in enumerate(iter_lines(f), 1):
                if line == "":
                    continue
                ok, data = parse_line(line, number)
                if ok:
                    yield data
    except (IOError, OSError):
        return


def iter_objects(f):
    """Yield ``(line number, object)`` for each non-blank line of the binary file ``f``.

    ``object`` is the parsed JSON object (a dict), or None for a line that is
    too long, not UTF-8, not JSON, nested too deep to parse, or JSON that is
    not an object. A blank line yields nothing, but it counts in the line
    numbers.
    """
    for number, line in enumerate(iter_lines(f), 1):
        if line == "":
            continue
        data = parse_line(line, number)[1]
        yield number, data if isinstance(data, dict) else None
