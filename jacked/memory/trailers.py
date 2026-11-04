"""The commit ledger trailers (#94i): read ``Why:``, ``Tried:`` and ``Next:`` lines.

Merge capture asks git for the trailers of the commits that a merge brings
in (``git log --format=%(trailers:key=Why,key=Tried,key=Next,unfold)``) and
gives the output to ``parse_trailers``, then to ``cap_trailers``. The lines
go into the distillation prompt and into the fallback note.

Trailer text comes from commits, so it is untrusted data: each value goes
through the shared control-character filter (``jacked.memory.sanitize``).
This module runs no git and reads no file.
"""
from __future__ import annotations

import logging

from jacked.memory.sanitize import one_line

logger = logging.getLogger(__name__)

#: The ledger trailer keys, in their canonical case.
TRAILER_KEYS = ("Why", "Tried", "Next")

#: The trailers go into the prompt and the fallback note, and recall reads
#: the note at each session start, so they have the same size budget as the
#: PR body. The oldest trailers stay (the first Why of the branch), each
#: value is cut to a readable length, and a marker line counts the trailers
#: that go. Each trim is logged, never silent.
MAX_TRAILER_LINES = 30
TRAILER_VALUE_MAX = 300
TRAILER_CUT_MARK = " [trimmed]"


def parse_trailers(out: str) -> list[str]:
    """Ledger ``Key: value`` lines (any key case), control characters stripped, deduped.

    git ends each trailer record with LF only, so the split is on LF. A
    Unicode line separator (U+2028, NEL) inside a value stays in the value,
    and ``one_line`` turns it into a space.

    >>> parse_trailers("why: a\\nCo-Authored-By: x\\nTried: b\\u2028c\\nWhy: a\\n")
    ['Why: a', 'Tried: b c']
    """
    canon = {k.lower(): k for k in TRAILER_KEYS}
    lines = []
    for key, sep, value in (raw.partition(":") for raw in out.split("\n")):
        key, value = canon.get(key.strip().lower()), one_line(value)
        if sep and key and value:
            lines.append(f"{key}: {value}")
    return list(dict.fromkeys(lines))


def cap_trailers(lines: list[str]) -> list[str]:
    """At most ``MAX_TRAILER_LINES`` lines, each value cut to ``TRAILER_VALUE_MAX``.

    The dedupe reads the complete lines, before any cut, so two different
    values with the same first ``TRAILER_VALUE_MAX`` characters both count.
    The oldest lines stay. A cut value ends with ``TRAILER_CUT_MARK``, and a
    last line ``(+N more trailers)`` counts the distinct lines that go.

    >>> cap_trailers(["Why: a", "Next: b", "Why: a"])
    ['Why: a', 'Next: b']
    >>> cap_trailers(["Why: " + "x" * 400])[0].endswith("x [trimmed]")
    True
    """
    distinct = list(dict.fromkeys(lines))
    kept = distinct[:MAX_TRAILER_LINES]
    out: list[str] = []
    cut = 0
    for line in kept:
        key, sep, value = line.partition(": ")
        if len(value) > TRAILER_VALUE_MAX:
            line = f"{key}{sep}{value[:TRAILER_VALUE_MAX]}{TRAILER_CUT_MARK}"
            cut += 1
        out.append(line)
    if cut:
        logger.debug("commit trailers: %d value(s) cut to %d characters", cut, TRAILER_VALUE_MAX)
    omitted = len(distinct) - len(kept)
    if not omitted:
        return out
    logger.debug("commit trailers: trailer list trimmed to %d of %d", len(kept), len(distinct))
    return [*out, f"(+{omitted} more trailers)"]
