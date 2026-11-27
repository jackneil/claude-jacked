"""The scanners of the loss report for the languages with braces: TypeScript, JavaScript, Go and Rust.

``jacked.testdiet.loss_scan`` says what each scanner counts. One pass over
a file (``_Index``) finds where each bracket closes, the comments and
strings, and the damage (a string that does not close, a bracket with no
partner). A body that holds damage is not read.
"""

from __future__ import annotations

import bisect
import functools
import re

from jacked.testdiet.model import BODY_NOT_READ, COMPUTED_CASES, Declaration, body_print

# A scanner for the languages with braces --------------------------------

def _skip_quoted(text: str, i: int, quote: str, multiline: bool) -> int:
    """Return the index after the string that starts at ``text[i]``, or -1 when it does not close."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n" and not multiline:
            return -1
        j += 1
    return -1


_CHAR = re.compile(r"'(?:\\.[^'\n]{0,9}|[^'\\\n])'")
_PAIRS = {"(": ")", "[": "]", "{": "}"}
# A "/" after one of these characters (or after one of these words) is in
# expression position, so it starts a regex literal. "<" and "}" are left out,
# so a JSX close tag ("</div>", "{y} />") is never read as a regex.
_REGEX_AFTER = set("(,=:[!&|?;{+-*%~^>")
_REGEX_WORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
                "throw", "instanceof", "yield", "await"}


def _regex_start(text: str, j: int) -> bool:
    """Return True when the "/" at ``text[j]`` is in expression position (it starts a regex literal)."""
    k = j - 1
    while k >= 0 and text[k] in " \t\r\n":
        k -= 1
    if k < 0 or text[k] in _REGEX_AFTER:
        return True
    end = k + 1
    while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
        k -= 1
    return text[k + 1:end] in _REGEX_WORDS


def _skip_regex(text: str, j: int) -> int:
    """Return the index after the regex literal at ``text[j]``, or -1 when it does not close on its line.

    A "/" inside a class (``[...]``) or after a backslash does not end it.
    A literal that does not close on its line is a division, not a regex.
    """
    i, in_class = j + 1, False
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            return -1
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        i += 1
    return -1


class _Index:
    """One pass over a file: where each bracket closes, the comments and strings, and the damage.

    Damage is a string that does not close (it ends at the line end) or a
    bracket that has no partner. A body that holds damage is not read.
    """

    def __init__(self, text: str, quotes: str, multiline: str, chars: bool, regex: bool = False) -> None:
        self.closes: dict[int, int] = {}
        self.spans: dict[int, int] = {}
        #: The start of each comment or string that does not close.
        self.unclosed: list[int] = []
        damage: list[int] = []
        stack: list[tuple[str, int]] = []
        j, size = 0, len(text)
        while j < size:
            ch = text[j]
            char = _CHAR.match(text, j) if chars and ch == "'" else None
            if char:
                end = char.end()
            elif ch in quotes:
                end = _skip_quoted(text, j, ch, ch in multiline)
                if end < 0:
                    damage.append(j)
                    self.unclosed.append(j)
                    newline = text.find("\n", j)
                    end = size if newline < 0 else newline
            elif text.startswith("//", j):
                newline = text.find("\n", j)
                end = size if newline < 0 else newline
            elif text.startswith("/*", j):
                close = text.find("*/", j + 2)
                if close < 0:
                    damage.append(j)
                    self.unclosed.append(j)
                end = size if close < 0 else close + 2
            elif regex and ch == "/" and _regex_start(text, j) and (end := _skip_regex(text, j)) > 0:
                pass
            else:
                if ch in _PAIRS:
                    stack.append((_PAIRS[ch], j))
                elif ch in ")]}":
                    if any(want == ch for want, _ in stack):
                        while stack[-1][0] != ch:
                            damage.append(stack.pop()[1])
                        self.closes[stack.pop()[1]] = j + 1
                    else:
                        damage.append(j)
                j += 1
                continue
            self.spans[j] = end
            j = end
        self._starts = sorted(self.spans)
        self._damage = sorted(damage)

    def damage_reason(self, text: str) -> str | None:
        """The file-level reason when a comment or a string does not close, else None."""
        if not self.unclosed:
            return None
        return f"a comment or a string does not close at line {text.count(chr(10), 0, self.unclosed[0]) + 1}"

    def in_code(self, pos: int) -> bool:
        """Return True when ``pos`` is not in a comment or a string."""
        k = bisect.bisect_right(self._starts, pos) - 1
        return k < 0 or pos >= self.spans[self._starts[k]]

    def body(self, start: int) -> int:
        """Return the index after the bracket that closes the one at ``start``, or -1 when it is not read."""
        end = self.closes.get(start, -1)
        k = bisect.bisect_left(self._damage, start)
        if end < 0 or (k < len(self._damage) and self._damage[k] < end):
            return -1
        return end


def _count(pattern: re.Pattern[str], text: str, start: int, end: int) -> int | None:
    return None if end < 0 else len(pattern.findall(text, start, end))


def _unread(end: int, cases: int | None) -> tuple[str, ...]:
    return ((BODY_NOT_READ,) if end < 0 else ()) + ((COMPUTED_CASES,) if cases is None else ())


# TypeScript and JavaScript ----------------------------------------------

_JS_HEAD = re.compile(r"(?<![\w$.])(?P<fn>test|it|xit|xtest)(?P<mods>(?:\s*\.\s*[A-Za-z]+)*)\s*")
_JS_MODS = {"each", "skip", "only", "concurrent", "failing", "fixme"}
_JS_SKIPS = {"skip", "fixme"}
_JS_ASSERT = re.compile(r"(?<![\w$.])(?:expect|assert)\s*[.(]")
_JS_QUOTES = "'\"`"


def _ws(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _elements(text: str, index: _Index, start: int, close: int) -> int:
    """The number of top-level elements between the bracket at ``start`` and the index ``close``."""
    rows, content, j = 0, False, start + 1
    while j < close - 1:
        if j in index.spans:
            content = content or text[j] in "'\"`"
            j = index.spans[j]
            continue
        if text[j] in _PAIRS:
            content, j = True, index.closes.get(j, close - 1)
            continue
        if text[j] == ",":
            rows, content = rows + 1, False
        elif not text[j].isspace():
            content = True
        j += 1
    return rows + (1 if content else 0)


def _array_rows(text: str, index: _Index, start: int, end: int) -> int | None:
    """The number of elements of the literal array that fills the call ``text[start:end]``, else None."""
    k = _ws(text, start + 1)
    close = index.closes.get(k, -1) if k < len(text) and text[k] == "[" else -1
    if close < 0 or _ws(text, close) != end - 1:
        return None
    return _elements(text, index, k, close)


def _print(text: str, start: int, end: int) -> str | None:
    """The ``body_print`` of ``text[start:end]``, or None when the body is not read."""
    return body_print(text[start:end]) if 0 <= start < end else None


def script(text: str) -> tuple[list[Declaration], str | None]:
    index = _Index(text, _JS_QUOTES, "`", chars=False, regex=True)
    found: list[Declaration] = []
    for head in _JS_HEAD.finditer(text):
        mods = re.findall(r"[A-Za-z]+", head["mods"])
        if not set(mods) <= _JS_MODS or not index.in_code(head.start()):
            continue
        i, cases = head.end(), 1
        if "each" in mods:
            if text.startswith("(", i):
                close = index.closes.get(i, -1)
                cases = None if close < 0 else _array_rows(text, index, i, close)
            elif text.startswith("`", i):
                close = _skip_quoted(text, i, "`", True)
                rows = [line for line in text[i + 1:close - 1].splitlines() if line.strip()]
                cases = max(len(rows) - 1, 0)
            else:
                continue
            if close < 0:
                continue
            i = _ws(text, close)
        if not text.startswith("(", i):
            continue
        start = _ws(text, i + 1)
        if start >= len(text) or text[start] not in _JS_QUOTES:
            continue
        name_end = _skip_quoted(text, start, text[start], text[start] == "`")
        if name_end < 0:
            continue
        end = index.body(i)
        skipped = head["fn"].startswith("x") or bool(_JS_SKIPS & set(mods))
        found.append(Declaration(text[start + 1:name_end - 1], _count(_JS_ASSERT, text, name_end, end),
                                 skipped, cases, _unread(end, cases), _print(text, name_end, end)))
    return found, index.damage_reason(text)


# Go and Rust ------------------------------------------------------------

_GO_HEAD = re.compile(r"^func[ \t]+(?P<name>Test(?![a-z])\w*)[ \t]*\((?P<params>[^)]*)\)", re.M)
_GO_RECEIVER = re.compile(r"\s*(\w+)\s+\*testing\.(?:T|B|F|TB)\b")
_RUST_TEST = re.compile(r"#\[(?:\w+::)*test\b")
_RUST_FN = re.compile(r"\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)")
_RUST_ASSERT = re.compile(r"\b\w*assert\w*!")


@functools.lru_cache(maxsize=64)
def _go_patterns(receiver: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    name = re.escape(receiver)
    return (re.compile(rf"(?<![\w.]){name}\.(?:Errorf?|Fatalf?|Fail|FailNow)\(|(?<![\w.])(?:assert|require)\.\w+\("),
            re.compile(rf"(?<![\w.]){name}\.Skip(?:f|Now)?\("),
            re.compile(rf"(?<![\w.]){name}\.Run\("))


def _go_literal(text: str, index: _Index, start: int, limit: int) -> tuple[int, int] | None:
    """Find the slice literal whose type starts at ``start`` (after ``[]``): (open brace, close), else None.

    A struct type (``[]struct{...}{...}``) is skipped to the brace of the values.
    """
    open_ = text.find("{", start, limit)
    if open_ >= 0 and re.search(r"\bstruct\s*$", text[start:open_]):
        close = index.closes.get(open_, -1)
        open_ = _ws(text, close) if close >= 0 else -1
    close = index.closes.get(open_, -1) if 0 <= open_ < limit and text[open_] == "{" else -1
    return None if close < 0 else (open_, close)


def _go_table(text: str, index: _Index, brace: int, loop: int, name: str) -> int | None:
    """The elements of the slice literal ``name`` that the function assigns before the loop, else None.

    A name that is assigned again between its literal and the loop gives None.
    """
    word = re.escape(name)
    found = [m for m in re.finditer(rf"(?<![\w.]){word}\s*:?=\s*\[\]", text[:loop])
             if m.start() > brace and index.in_code(m.start())]
    literal = _go_literal(text, index, found[-1].end(), loop) if found else None
    if literal is None:
        return None
    again = re.compile(rf"(?<![\w.]){word}\s*(?:,[^=\n]*)?[:+]?=(?!=)")
    if any(index.in_code(m.start()) for m in again.finditer(text, literal[1], loop)):
        return None
    return _elements(text, index, *literal)


_GO_FUNC = re.compile(r"func\b")


def _go_body_brace(text: str, index: _Index, start: int, end: int) -> int:
    """Return the body brace of the for header that starts at ``start``, or -1 when there is none.

    The body brace is the first ``{`` at paren and bracket depth 0 that comes
    after a space or a ``)``. A ``{`` directly after another character
    (``[]string{``, ``struct{``, ``Type{``) opens a composite literal, and
    the scan goes on after its close. gofmt writes ``X {`` for a body and
    ``T{`` for a literal. Go also requires parentheses around a bare type
    literal in a for header. So this rule reads gofmt'ed code exactly.
    A ``func`` keyword at depth 0 makes the next spaced ``{`` the body of a
    function literal (``range func() []int { ... }()``). The scan skips that
    body and the call after it, and goes on.
    """
    j, in_func = start, False
    while j < end:
        if j in index.spans:
            j = index.spans[j]
            continue
        ch = text[j]
        if _GO_FUNC.match(text, j) and (j == 0 or not (text[j - 1].isalnum() or text[j - 1] in "_.")):
            j, in_func = j + 4, True
            continue
        if ch in "([{":
            if ch == "{" and (text[j - 1].isspace() or text[j - 1] == ")"):
                if not in_func:
                    return j
                in_func = False
            close = index.closes.get(j, -1)
            if close < 0:
                return -1
            j = close
            continue
        if ch in ")]}":
            return -1
        j += 1
    return -1


def _go_loops(text: str, index: _Index, brace: int, end: int) -> list[tuple[int, int, int]]:
    """Each for loop of a body as (head end, body brace, body close).

    A loop with no body brace has the body brace -1, and it holds all the
    text from its head to ``end``. Its cases are not counted.
    """
    loops = []
    for loop in re.finditer(r"\bfor\b", text[:end]):
        if loop.start() <= brace or not index.in_code(loop.start()):
            continue
        open_ = _go_body_brace(text, index, loop.end(), end)
        close = index.closes.get(open_, -1) if open_ >= 0 else -1
        loops.append((loop.end(), open_, close) if close >= 0 else (loop.end(), -1, end))
    return loops


def _range_rows(text: str, index: _Index, brace: int, head: int, open_: int) -> int | None:
    if open_ < 0:
        return None
    target = re.search(r"\brange\s+", text[head:open_])
    if target is None:
        return None
    start = head + target.end()
    if text.startswith("[]", start):
        literal = _go_literal(text, index, start + 2, open_)
        return None if literal is None or _ws(text, literal[1]) != open_ else _elements(text, index, *literal)
    name = re.fullmatch(r"(\w+)\s*", text[start:open_])
    return None if name is None else _go_table(text, index, brace, head, name.group(1))


def _go_cases(text: str, index: _Index, brace: int, end: int, runs: re.Pattern[str]) -> int | None:
    """Each t.Run outside a loop counts 1. In a range loop over a slice literal, its rows."""
    loops = _go_loops(text, index, brace, end)
    total: int | None = 0
    for run in runs.finditer(text, brace, end):
        if not index.in_code(run.start()):
            continue
        inside = [item for item in loops if item[0] <= run.start() < item[2]]
        if not inside:
            rows = 1
        else:
            head, open_, _ = max(inside, key=lambda item: item[0])
            rows = _range_rows(text, index, brace, head, open_)
        total = None if total is None or rows is None else total + rows
    return total


def go(text: str) -> tuple[list[Declaration], str | None]:
    index = _Index(text, "\"`", "`", chars=True)
    found: list[Declaration] = []
    for head in _GO_HEAD.finditer(text):
        if head["name"] == "TestMain" or not index.in_code(head.start()):
            continue
        receiver = _GO_RECEIVER.match(head["params"])
        asserts, skips, runs = _go_patterns(receiver.group(1) if receiver else "t")
        brace = text.find("{", head.end())
        end = -1 if brace < 0 else index.body(brace)
        skipped = None if end < 0 else bool(skips.search(text, brace, end))
        cases = None if end < 0 else _go_cases(text, index, brace, end, runs)
        found.append(Declaration(head["name"], _count(asserts, text, brace, end), skipped, cases,
                                 _unread(end, cases) if end >= 0 else (BODY_NOT_READ,),
                                 _print(text, brace, end)))
    return found, index.damage_reason(text)


def rust(text: str) -> tuple[list[Declaration], str | None]:
    index = _Index(text, "\"", "\"", chars=True)
    found: list[Declaration] = []
    for head in _RUST_TEST.finditer(text):
        j = index.closes.get(head.start() + 1, -1)
        if j < 0 or not index.in_code(head.start()) or not re.fullmatch(
                r"#\[(?:\w+::)*test(?:\(.*\))?\]", text[head.start():j], re.S):
            continue
        ignored = False
        while True:
            k = _ws(text, j)
            if text.startswith("//", k):
                newline = text.find("\n", k)
                j = len(text) if newline < 0 else newline
            elif text.startswith("/*", k):
                close = text.find("*/", k + 2)
                j = len(text) if close < 0 else close + 2
            elif text.startswith("#[", k) and index.closes.get(k + 1, -1) > 0:
                ignored = ignored or bool(re.match(r"#\[\s*ignore\b", text[k:]))
                j = index.closes[k + 1]
            else:
                break
        fn = _RUST_FN.match(text, j)
        if fn is None:
            continue
        brace = text.find("{", fn.end())
        end = -1 if brace < 0 else index.body(brace)
        found.append(Declaration(fn["name"], _count(_RUST_ASSERT, text, brace, end), ignored, 1, _unread(end, 1),
                                 _print(text, brace, end)))
    return found, index.damage_reason(text)
