"""The scanners of the loss report: the tests that the source of one file declares.

A declaration is a test that the source of a file declares:

- Python: a function or a method whose name starts with ``test``, read with
  ``ast``, also inside a module-level or class-level ``if``/``else``. A class
  gives the form ``Class::name``. A file that does not parse is read with a
  line pattern, which gives names only.
- TypeScript and JavaScript: the first string argument of ``test(`` and
  ``it(``, also with ``.each``, ``.skip``, ``.only``, ``.concurrent``,
  ``.failing`` and ``.fixme``, and ``xit(`` and ``xtest(``. A call inside a
  comment or a string is not a declaration.
- Go: ``func TestXxx(`` in a ``_test.go`` file. ``TestMain`` is not a test.
- Rust: the function after ``#[test]`` (or an attribute such as
  ``#[tokio::test]``). Attributes and comments can be between them.

The assertions of a test:

- Python: each ``assert`` statement, and each call whose name starts with ``assert``.
- TypeScript and JavaScript: each ``expect`` and ``assert`` call.
- Go: ``t.Error``, ``t.Errorf``, ``t.Fatal``, ``t.Fatalf``, ``t.Fail`` and
  ``t.FailNow`` (with the receiver name of the signature), and the ``assert.``
  and ``require.`` packages.
- Rust: each macro whose name holds ``assert``.

The cases of a test:

- Python: the product of the lengths of the literal lists or tuples of the
  ``parametrize`` marks on the function and on its class.
- TypeScript and JavaScript: the rows of a literal ``.each([...])`` array, or
  the data rows of a ``.each`` template table.
- Go: the number of ``t.Run(`` calls in the body.
- Rust: always 1.

A case list that the source computes (a name, a call, a comprehension) gives
no count. A fixture parameter and a ``pytestmark`` parametrize are not counted.

The skip marks: Python ``skip``, ``skipif``, ``skipIf`` and ``skipUnless``
on the function or its class, a ``pytestmark`` with one of them (module or
class), and a ``pytest.skip(`` or ``self.skipTest(`` call in the body;
TypeScript and JavaScript ``.skip``, ``.fixme``, ``xit`` and ``xtest``; Go
``t.Skip``, ``t.Skipf`` and ``t.SkipNow``; Rust ``#[ignore]``.

A value that the source cannot give is None, and ``unread`` says why: the
file does not parse, the body does not close (or holds a string that does
not close), or the case list is computed.
"""

from __future__ import annotations

import ast
import bisect
import functools
import re
import warnings
from dataclasses import dataclass

#: A reason in ``Declaration.unread``.
NOT_PARSED = "does not parse"
BODY_NOT_READ = "body not read"
COMPUTED_CASES = "computed cases"

_SCRIPT = re.compile(r"\.[cm]?[jt]sx?$")
_PY_SKIPS = {"skip", "skipif", "skipIf", "skipUnless"}


@dataclass(frozen=True)
class Declaration:
    """One test of a file.

    ``asserts``, ``skipped`` and ``cases`` are None when the source cannot
    give them. ``unread`` holds the reason of each None value.
    """

    name: str
    asserts: int | None
    skipped: bool | None
    cases: int | None = 1
    unread: tuple[str, ...] = ()


def declarations(path: str, text: str) -> list[str]:
    """Return the names of the tests that ``text`` declares, in source order.

    >>> declarations("t/test_a.py", "def test_one():\\n    pass\\n")
    ['test_one']
    """
    return [found.name for found in scan(path, text)]


def scan_kind(path: str) -> bool:
    """Return True when ``scan`` reads the file at ``path`` (by its name only)."""
    return path.endswith((".py", "_test.go", ".rs")) or bool(_SCRIPT.search(path))


def scan(path: str, text: str) -> list[Declaration]:
    """Return each test that ``text`` declares, with its assertions, cases and skip mark."""
    if path.endswith(".py"):
        return _python(text)
    if _SCRIPT.search(path):
        return _script(text)
    if path.endswith("_test.go"):
        return _go(text)
    if path.endswith(".rs"):
        return _rust(text)
    return []


# Python ------------------------------------------------------------------

def _python(text: str) -> list[Declaration]:
    text = text[1:] if text.startswith("﻿") else text
    try:
        with warnings.catch_warnings():
            # A warning (an escape such as "\d") must not stop the parse.
            warnings.simplefilter("ignore")
            module = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _python_lines(text)
    found: list[Declaration] = []
    _python_body(module.body, "", _pytestmark_skips(module.body), 1, found)
    return found


def _python_body(body: list[ast.stmt], prefix: str, skipped: bool, cases: int | None,
                 found: list[Declaration]) -> None:
    for node in body:
        if isinstance(node, ast.If):
            for branch in (node.body, node.orelse):
                _python_body(branch, prefix, skipped, cases, found)
        elif isinstance(node, ast.ClassDef):
            marked = skipped or _decorated_skip(node) or _pytestmark_skips(node.body)
            _python_body(node.body, f"{prefix}{node.name}::", marked, _times(cases, _param_cases(node)),
                         found)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            inner = [sub for stmt in node.body for sub in ast.walk(stmt)]
            asserts = sum(1 for sub in inner if isinstance(sub, ast.Assert)
                          or (isinstance(sub, ast.Call) and _callee(sub.func).startswith("assert")))
            in_body = any(isinstance(sub, ast.Call) and _is_skip_call(sub.func) for sub in inner)
            number = _times(cases, _param_cases(node))
            found.append(Declaration(prefix + node.name, asserts, skipped or _decorated_skip(node) or in_body,
                                     number, () if number is not None else (COMPUTED_CASES,)))


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Call):
        func = func.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_skip_call(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr == "skipTest" or (func.attr == "skip" and isinstance(func.value, ast.Name)
                                       and func.value.id == "pytest")


def _decorated_skip(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_callee(deco) in _PY_SKIPS for deco in node.decorator_list)


def _pytestmark_skips(body: list[ast.stmt]) -> bool:
    for node in body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else [])
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            marks = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            if any(_callee(mark) in _PY_SKIPS for mark in marks):
                return True
    return False


def _param_cases(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    number: int | None = 1
    for deco in node.decorator_list:
        if not (isinstance(deco, ast.Call) and _callee(deco.func) == "parametrize"):
            continue
        values = deco.args[1] if len(deco.args) >= 2 else next(
            (k.value for k in deco.keywords if k.arg == "argvalues"), None)
        literal = isinstance(values, (ast.List, ast.Tuple, ast.Set)) and not any(
            isinstance(e, ast.Starred) for e in values.elts)
        number = _times(number, len(values.elts) if literal else None)
    return number


def _times(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else a * b


_PY_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<name>\w+)")
_PY_CLASS = re.compile(r"^(?P<indent>[ \t]*)class[ \t]+(?P<name>\w+)")


def _python_lines(text: str) -> list[Declaration]:
    """The line pattern for a Python file that does not parse. It gives names only.

    A def inside a def is not a test. A line inside a triple-quoted string
    (a docstring) does not end a class.
    """
    found: list[Declaration] = []
    scopes: list[tuple[int, str, str]] = []
    quote = None
    for line in text.splitlines():
        if quote is not None:
            if line.count(quote) % 2:
                quote = None
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = len(line) - len(line.lstrip(" \t"))
        while scopes and scopes[-1][0] >= depth:
            scopes.pop()
        match = _PY_CLASS.match(line)
        kind = "class"
        if not match:
            match, kind = _PY_DEF.match(line), "def"
        if match:
            if kind == "def" and match["name"].startswith("test") and not any(k == "def" for _, k, _ in scopes):
                prefix = "".join(f"{name}::" for _, k, name in scopes if k == "class")
                found.append(Declaration(prefix + match["name"], None, None, None, (NOT_PARSED,)))
            scopes.append((depth, kind, match["name"]))
        for mark in ('"""', "'''"):
            if line.count(mark) % 2:
                quote = mark
                break
    return found


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


class _Index:
    """One pass over a file: where each bracket closes, the comments and strings, and the damage.

    Damage is a string that does not close (it ends at the line end) or a
    bracket that has no partner. A body that holds damage is not read.
    """

    def __init__(self, text: str, quotes: str, multiline: str, chars: bool) -> None:
        self.closes: dict[int, int] = {}
        self.spans: dict[int, int] = {}
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
                    newline = text.find("\n", j)
                    end = size if newline < 0 else newline
            elif text.startswith("//", j):
                newline = text.find("\n", j)
                end = size if newline < 0 else newline
            elif text.startswith("/*", j):
                close = text.find("*/", j + 2)
                if close < 0:
                    damage.append(j)
                end = size if close < 0 else close + 2
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


def _array_rows(text: str, index: _Index, start: int, end: int) -> int | None:
    """The number of elements of the literal array that fills the call ``text[start:end]``, else None."""
    k = _ws(text, start + 1)
    close = index.closes.get(k, -1) if k < len(text) and text[k] == "[" else -1
    if close < 0 or _ws(text, close) != end - 1:
        return None
    rows, content, j = 0, False, k + 1
    while j < close - 1:
        if j in index.spans:
            content = content or text[j] in _JS_QUOTES
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


def _script(text: str) -> list[Declaration]:
    index = _Index(text, _JS_QUOTES, "`", chars=False)
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
                                 skipped, cases, _unread(end, cases)))
    return found


# Go and Rust ------------------------------------------------------------

_GO_HEAD = re.compile(r"^func[ \t]+(?P<name>Test(?![a-z])\w*)[ \t]*\((?P<params>[^)]*)\)", re.M)
_GO_RECEIVER = re.compile(r"\s*(\w+)\s+\*testing\.(?:T|B|F|TB)\b")
_RUST_HEAD = re.compile(
    r"#\[(?:\w+::)*test(?:\([^)]*\))?\](?P<attrs>(?:\s*(?:#\[[^\]]*\]|//[^\n]*))*)\s*"
    r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)")
_RUST_ASSERT = re.compile(r"\b\w*assert\w*!")


@functools.lru_cache(maxsize=64)
def _go_patterns(receiver: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    name = re.escape(receiver)
    return (re.compile(rf"(?<![\w.]){name}\.(?:Errorf?|Fatalf?|Fail|FailNow)\(|(?<![\w.])(?:assert|require)\.\w+\("),
            re.compile(rf"(?<![\w.]){name}\.Skip(?:f|Now)?\("),
            re.compile(rf"(?<![\w.]){name}\.Run\("))


def _go(text: str) -> list[Declaration]:
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
        cases = _count(runs, text, brace, end)
        found.append(Declaration(head["name"], _count(asserts, text, brace, end), skipped, cases,
                                 (BODY_NOT_READ,) if end < 0 else ()))
    return found


def _rust(text: str) -> list[Declaration]:
    index = _Index(text, "\"", "\"", chars=True)
    found: list[Declaration] = []
    for head in _RUST_HEAD.finditer(text):
        if not index.in_code(head.start()):
            continue
        brace = text.find("{", head.end())
        end = -1 if brace < 0 else index.body(brace)
        attrs = re.sub(r"//[^\n]*", "", head["attrs"])
        found.append(Declaration(head["name"], _count(_RUST_ASSERT, text, brace, end),
                                 bool(re.search(r"#\[ignore\b", attrs)), 1, _unread(end, 1)))
    return found
