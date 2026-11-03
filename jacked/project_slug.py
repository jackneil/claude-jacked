"""Name a Claude Code project folder the way Claude Code names it.

Claude Code keeps the transcripts and the auto memory of a session folder in
``<config dir>/projects/<name>/``. This module owns the one copy of the
algorithm that makes ``<name>``, as Claude Code 2.1.317 writes it:

1. Each UTF-16 unit of the path that is not an ASCII letter or digit becomes
   ``-``. A character outside the BMP is two units, so it gives two hyphens.
2. A name of 200 units or fewer is the result.
3. A longer name keeps its first 200 units and gets ``-`` and the base 36 of
   the absolute value of a signed 32-bit hash of the path over its UTF-16
   units.

The encoded name is ASCII, so its length in characters is its length in
UTF-16 units. Use ``project_dir_name`` for a folder on disk: the raw
``encode`` of a long path can be longer than the file name limit of the
file system.

Standard library only, so ``jacked.recover`` stays usable on a bare install.
"""
from __future__ import annotations

#: Claude Code keeps a project folder name to this many UTF-16 units, then adds a hash.
NAME_LIMIT = 200


def encode(path: str) -> str:
    """Return ``path`` with each UTF-16 unit that is not an ASCII letter or digit as ``-``.

    This is step 1 only, with no length limit.

    >>> encode("/tmp/\\U0001F600 x")
    '-tmp----x'
    >>> encode("C:\\\\Users\\\\me")
    'C--Users-me'
    """
    return "".join(
        c if c.isascii() and c.isalnum() else "--" if ord(c) > 0xFFFF else "-"
        for c in str(path)
    )


def project_dir_name(path: str) -> str:
    """Return the folder name that Claude Code gives ``path`` under ``projects/``.

    >>> project_dir_name("/Users/jack.neil/Github/claude-jacked")
    '-Users-jack-neil-Github-claude-jacked'
    >>> project_dir_name("/tmp/" + "a" * 196)[195:]
    'aaaaa-r0traf'
    """
    name = encode(path)
    if len(name) <= NAME_LIMIT:
        return name
    return f"{name[:NAME_LIMIT]}-{base36(abs(string_hash(path)))}"


def string_hash(text: str) -> int:
    """Return the signed 32-bit hash of ``text`` over UTF-16 units (the Claude Code hash).

    >>> string_hash("abc"), string_hash("")
    (96354, 0)
    """
    value = 0
    units = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(units), 2):
        value = ((value << 5) - value + (units[i] | units[i + 1] << 8)) & 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


def base36(number: int) -> str:
    """Return ``number`` in lowercase base 36, as JavaScript ``toString(36)`` does.

    >>> base36(0), base36(35), base36(36)
    ('0', 'z', '10')
    """
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out
