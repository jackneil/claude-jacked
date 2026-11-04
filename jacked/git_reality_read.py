"""Read the git output that the git reality block shows, in fixed memory.

``jacked.data.hooks.git_reality`` gives the stdout of ``git log`` and
``git status`` to these readers through ``jacked.git_guard.run_git``, chunk
by chunk. The log reader keeps at most ``RECORD_LIMIT`` characters of each
commit field and counts the rest. The status reader keeps only counts. So a
30 MB commit message or a status with a million entries never sits in memory
whole, and no count changes.
"""
from __future__ import annotations

from jacked.memory.sanitize import one_line, strip_control

#: The most characters of one commit subject or body that the block reads. The
#: caps (40 lines, 600 tokens, about 2,400 characters) show far less, so this
#: limit never changes what fits. It stops a huge commit message from being
#: stripped and split in full. A clipped body gets a last line that says how
#: many characters were cut.
RECORD_LIMIT = 16_384

# The bytes of each commit field that the log reader keeps. UTF-8 takes at
# most 4 bytes for a character, so this holds RECORD_LIMIT characters.
_FIELD_KEEP = 4 * RECORD_LIMIT
# UTF-8 continuation bytes. A dropped character is counted by its first byte.
_CONTINUATION = bytes(range(0x80, 0xC0))

# Porcelain v1 status letters (git-status(1), "Short Format"). X is the index
# column and Y is the work-tree column.
_STAGED = frozenset("MTADRC")  # X: "Changes to be committed"
_CHANGED = frozenset("MTARC")  # Y, other than D: "Changes not staged for commit"
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})  # "Unmerged paths"


class LogReader:
    """Read ``git log -z`` output into at most RECORD_LIMIT characters per field.

    Each commit field (sha, subject, body) keeps its first ``_FIELD_KEEP``
    bytes. The characters past that are counted, not kept, so a body of any
    size costs a fixed amount of memory and its clipped count stays exact
    for UTF-8 text. ``records`` holds ``(sha, subject, body, dropped body
    characters)`` for each commit.

    >>> r = LogReader(); r.feed(b"abc1234\\x1fFix it\\x1fWhy: x\\x00"), r.close(), r.records
    (True, None, [('abc1234', 'Fix it', 'Why: x', 0)])
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, int]] = []
        self._start()

    def _start(self) -> None:
        self._fields = [bytearray(), bytearray(), bytearray()]
        self._dropped = 0
        self._field = 0

    def _add(self, piece: bytes) -> None:
        kept = self._fields[self._field]
        room = max(0, _FIELD_KEEP - len(kept))
        kept += piece[:room]
        if len(piece) > room and self._field == 2:
            self._dropped += len(piece[room:].translate(None, _CONTINUATION))

    def feed(self, chunk: bytes) -> bool:
        pos = 0
        while True:
            nul = chunk.find(b"\0", pos)
            end = len(chunk) if nul < 0 else nul
            while self._field < 2:
                sep = chunk.find(b"\x1f", pos, end)
                if sep < 0:
                    break
                self._add(chunk[pos:sep])
                self._field += 1
                pos = sep + 1
            self._add(chunk[pos:end])
            if nul < 0:
                return True
            self._finish()
            pos = nul + 1

    def _finish(self) -> None:
        sha, subject, body = (bytes(f).decode("utf-8", errors="replace") for f in self._fields)
        if (sha + subject + body).strip() or self._dropped:
            self.records.append((sha, subject, body, self._dropped))
        self._start()

    def close(self) -> None:
        self._finish()


class StatusCounter:
    """Count ``git status --porcelain=v1 -z`` entries the way ``git status`` groups them.

    Each entry is "XY PATH". One path can count twice: "MM" is a staged change
    and also a change that is not staged.

    - untracked: "??"
    - conflicted: an unmerged pair (DD, AU, UD, UA, DU, AA, UU)
    - staged: X is M, T, A, D, R or C
    - deleted: Y is D (the deletion is not staged)
    - modified: Y is M, T, A, R or C (the change is not staged)

    A rename or a copy has one more NUL field, its source path, which is not
    an entry. An untracked folder is one entry, as ``git status`` shows it.
    The count happens as the output comes: only the first 3 bytes and the
    length of each NUL-ended field are kept, so the memory is fixed however
    many entries there are. An entry that is not "XY PATH" sets ``error``
    and stops the read.

    >>> c = StatusCounter(); c.feed(b"R  new.txt\\x00MD-old.txt\\x00 M a.txt\\x00?? b.txt\\x00")
    True
    >>> c.counts
    {'modified': 1, 'staged': 1, 'deleted': 0, 'untracked': 1, 'conflicted': 0}
    """

    def __init__(self) -> None:
        self.counts = dict.fromkeys(("modified", "staged", "deleted", "untracked", "conflicted"), 0)
        self.error: str | None = None
        self._head = b""
        self._size = 0
        self._skip = False

    def feed(self, chunk: bytes) -> bool:
        pos = 0
        while True:
            nul = chunk.find(b"\0", pos)
            piece = chunk[pos:] if nul < 0 else chunk[pos:nul]
            self._head += piece[: 3 - len(self._head)]
            self._size += len(piece)
            if nul < 0:
                return True
            if not self._entry():
                return False
            pos = nul + 1

    def close(self) -> None:
        if self._size:  # a last entry without its NUL
            self._entry()

    def _entry(self) -> bool:
        head, size = self._head.decode("latin-1"), self._size
        self._head, self._size = b"", 0
        if self._skip:  # the source path of a rename or a copy
            self._skip = False
            return True
        if size < 4 or head[2] != " ":
            self.error = "git status printed an entry that is not 'XY PATH'"
            return False
        x, y = head[0], head[1]
        self._skip = "R" in (x, y) or "C" in (x, y)
        counts = self.counts
        if x + y == "??":
            counts["untracked"] += 1
        elif x + y in _UNMERGED:
            counts["conflicted"] += 1
        else:
            if x in _STAGED:
                counts["staged"] += 1
            if y == "D":
                counts["deleted"] += 1
            elif y in _CHANGED:
                counts["modified"] += 1
        return True


def commit_lines(records: list[tuple[str, str, str, int]]) -> list[str]:
    """Turn the ``LogReader`` records into data lines, one line each.

    A commit gives "  <sha> <subject>", then "    <line>" for each body line.
    Blank body lines are left out, because they carry no data. A body is
    split on every line break, Unicode separators included, so each part
    gets the indent of a body line. Subject and body are cut to
    RECORD_LIMIT characters, and a cut body ends with a clipped line.

    >>> commit_lines([("abc1234", "Fix it", "Why: x\\n\\nNext: y\\n", 0)])
    ['  abc1234 Fix it', '    Why: x', '    Next: y']
    """
    lines: list[str] = []
    for sha, subject, body, dropped in records:
        clipped = max(0, len(body) - RECORD_LIMIT) + dropped
        subject, body = subject[:RECORD_LIMIT], body[:RECORD_LIMIT]
        lines.append(f"  {one_line(sha)} {one_line(subject)}".rstrip())
        for line in body.splitlines():
            line = strip_control(line).rstrip()
            if line.strip():
                lines.append(f"    {line}")
        if clipped:
            lines.append(f"    [clipped: {clipped} more characters]")
    return lines
