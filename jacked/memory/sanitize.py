"""The one sanitizer for text that jacked prints or puts in a session's context.

Note bodies, commit messages, remote errors and file names can carry terminal
escapes, C1 controls or bidi overrides. ``jacked.memory.vault`` re-exports
these names, so ``vault.strip_control`` and ``vault.one_line`` stay valid.
"""
from __future__ import annotations

import re

# Control chars except tab and newline (CR goes too), stripped from any text we
# print through Rich (search excerpts, git error tails) or put in a session's
# context (the recall brief, the git reality block, the load budget lines): a
# note body or a remote-derived string could carry a terminal escape/OSC
# sequence. C1 controls go too (U+009B is a one-character CSI), and so do the
# bidi marks, embeddings, overrides and isolates (U+200E, U+200F, U+202A to
# U+202E, U+2066 to U+2069), which can show text in an order that is not its
# real order. This is the one sanitizer for that text.
# The set is built from code points, not from regex ranges, so a reader (and a
# static analyser) sees each block by name: C0 without tab and newline, DEL and
# C1, the two bidi marks, the bidi embeddings and overrides, the bidi isolates.
_CONTROL_CODEPOINTS = (
    *range(0x00, 0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0),
    0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A),
)
_CONTROL_CHARS_RE = re.compile("[" + "".join(re.escape(chr(c)) for c in _CONTROL_CODEPOINTS) + "]")


def strip_control(s: str) -> str:
    """Strip control chars (keeps tab and newline; strips CR) via the single regex.

    Vault content is echoed to terminals and injected into session context, so
    a note body or a remote-derived string could carry a terminal escape/OSC
    sequence; this is the one place that regex is applied.
    """
    return _CONTROL_CHARS_RE.sub("", s or "")


#: The fence of every block of untrusted data that jacked puts in a session's
#: context or a prompt. It has a fixed length: ``fence_safe`` removes every
#: backtick from the data, so no data line can close it.
FENCE = "```"


def fence_safe(s: str) -> str:
    """Change each backtick in ``s`` to an apostrophe.

    Text from a repository or a file goes into a session's context inside a
    fenced block or a code span. Without backticks it cannot close the fence
    or the span, whatever its length.

    >>> fence_safe("a ``` b")
    "a ''' b"
    """
    return s.replace("`", "'")


def one_line(s: str) -> str:
    """Control-stripped, whitespace-collapsed single line.

    Whitespace collapses first, so a line-break control (CR, NEL, U+2028)
    keeps the words on each side of it apart.

    >>> one_line("a\\rb\\x85c  d\\x1b[0m")
    'a b c d[0m'
    """
    return " ".join(strip_control(" ".join((s or "").split())).split())
