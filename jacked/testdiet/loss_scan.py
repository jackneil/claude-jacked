"""The scanners of the loss report: the tests that the source of one file declares.

A declaration is a test that the source of a file declares:

- Python: a function or a method whose name starts with ``test``, read with
  ``ast``. A class gives the form ``Class::name``. A file that does not parse
  is read with a line pattern.
- TypeScript and JavaScript: the first string argument of ``test(`` and
  ``it(``, also with ``.each``, ``.skip``, ``.only``, ``.concurrent``,
  ``.failing`` and ``.fixme``, and ``xit(`` and ``xtest(``.
- Go: ``func TestXxx(`` in a ``_test.go`` file. ``TestMain`` is not a test.
- Rust: the function after ``#[test]`` (or an attribute such as ``#[tokio::test]``).

An assertion is an ``assert`` statement, or a call whose name starts with
``assert`` (Python), an ``expect`` or ``assert`` call (TypeScript and
JavaScript), ``t.Error``, ``t.Fatal`` and the ``assert.`` and ``require.``
packages (Go), or a macro whose name holds ``assert`` (Rust). A count that
the source cannot give (the Python line pattern, a body that does not close)
is None.
"""

from __future__ import annotations

import ast
import re
import warnings
from dataclasses import dataclass

_SCRIPT = re.compile(r"\.[cm]?[jt]sx?$")
_PY_SKIPS = {"skip", "skipif", "skipIf", "skipUnless"}

@dataclass(frozen=True)
class Declaration:
    """One test of a file. ``asserts`` and ``skipped`` are None when the source cannot give them."""

    name: str
    asserts: int | None
    skipped: bool | None


def declarations(path: str, text: str) -> list[str]:
    """Return the names of the tests that ``text`` declares, in source order.

    >>> declarations("t/test_a.py", "def test_one():\\n    pass\\n")
    ['test_one']
    """
    return [found.name for found in scan(path, text)]


def scan(path: str, text: str) -> list[Declaration]:
    """Return each test that ``text`` declares, with its assertion count and its skip mark."""
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
    text = text[1:] if text.startswith("\ufeff") else text
    try:
        with warnings.catch_warnings():
            # A warning (an escape such as "\d") must not stop the parse.
            warnings.simplefilter("ignore")
            module = ast.parse(text)
    except (SyntaxError, ValueError):
        return _python_lines(text)
    found: list[Declaration] = []
    _python_body(module.body, "", False, found)
    return found


def _python_body(body: list[ast.stmt], prefix: str, class_skipped: bool,
                 found: list[Declaration]) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            _python_body(node.body, f"{prefix}{node.name}::", class_skipped or _py_skipped(node),
                         found)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            asserts = sum(1 for sub in ast.walk(node)
                          if isinstance(sub, ast.Assert)
                          or (isinstance(sub, ast.Call) and _callee(sub.func).startswith("assert")))
            found.append(Declaration(prefix + node.name, asserts, class_skipped or _py_skipped(node)))


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Call):
        func = func.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _py_skipped(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_callee(deco) in _PY_SKIPS for deco in node.decorator_list)


_PY_DEF = re.compile(r"^(?P<indent>[ \t]*)(?:async[ \t]+)?def[ \t]+(?P<name>test\w*)")
_PY_CLASS = re.compile(r"^(?P<indent>[ \t]*)class[ \t]+(?P<name>\w+)")


def _python_lines(text: str) -> list[Declaration]:
    """The line pattern for a Python file that does not parse. It gives names only."""
    found: list[Declaration] = []
    classes: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = len(line) - len(line.lstrip(" \t"))
        while classes and classes[-1][0] >= depth:
            classes.pop()
        match = _PY_CLASS.match(line)
        if match:
            classes.append((depth, match["name"]))
            continue
        match = _PY_DEF.match(line)
        if match:
            prefix = "".join(f"{name}::" for _, name in classes)
            found.append(Declaration(prefix + match["name"], None, None))
    return found


# A scanner for the languages with braces --------------------------------

def _skip_quoted(text: str, i: int, quote: str) -> int:
    """Return the index after the string that starts at ``text[i]``, or -1 when it does not close."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n" and quote != "`":
            return -1
        j += 1
    return -1


_CHAR = re.compile(r"'(?:\\.[^'\n]{0,9}|[^'\\\n])'")


def _close(text: str, i: int, quotes: str, chars: bool = False) -> int:
    """Return the index after the bracket that closes the one at ``text[i]``, or -1.

    Strings in ``quotes`` and the comments ``//`` and ``/* */`` are skipped.
    With ``chars``, a char literal such as ``'{'`` is skipped too (Go and
    Rust). A Rust lifetime such as ``'a`` has no closing quote, so it is not
    a char literal.
    """
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    j = i
    while 0 <= j < len(text):
        ch = text[j]
        char = _CHAR.match(text, j) if chars and ch == "'" else None
        if char:
            j = char.end()
            continue
        if ch in quotes:
            j = _skip_quoted(text, j, ch)
            continue
        if text.startswith("//", j):
            end = text.find("\n", j)
            j = len(text) if end < 0 else end
            continue
        if text.startswith("/*", j):
            end = text.find("*/", j + 2)
            j = len(text) if end < 0 else end + 2
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in ")]}":
            if not stack or stack.pop() != ch:
                return -1
            if not stack:
                return j + 1
        j += 1
    return -1


def _count(pattern: re.Pattern[str], text: str, start: int, end: int) -> int | None:
    return None if end < 0 else len(pattern.findall(text, start, end))


# TypeScript and JavaScript ----------------------------------------------

_JS_HEAD = re.compile(r"(?<![\w$.])(?P<fn>test|it|xit|xtest)(?P<mods>(?:\s*\.\s*[A-Za-z]+)*)\s*")
_JS_MODS = {"each", "skip", "only", "concurrent", "failing", "fixme"}
_JS_SKIPS = {"skip", "fixme"}
_JS_ASSERT = re.compile(r"(?<![\w$.])(?:expect|assert)\s*[.(]")
_JS_QUOTES = "'\"`"


def _script(text: str) -> list[Declaration]:
    found: list[Declaration] = []
    for head in _JS_HEAD.finditer(text):
        mods = re.findall(r"[A-Za-z]+", head["mods"])
        if not set(mods) <= _JS_MODS:
            continue
        i = head.end()
        if "each" in mods:
            if i < len(text) and text[i] == "(":
                i = _close(text, i, _JS_QUOTES)
            elif i < len(text) and text[i] == "`":
                i = _skip_quoted(text, i, "`")
            else:
                continue
            if i < 0:
                continue
            i += len(text[i:]) - len(text[i:].lstrip())
        if i >= len(text) or text[i] != "(":
            continue
        start = i + 1 + len(text[i + 1:]) - len(text[i + 1:].lstrip())
        if start >= len(text) or text[start] not in _JS_QUOTES:
            continue
        name_end = _skip_quoted(text, start, text[start])
        if name_end < 0:
            continue
        end = _close(text, i, _JS_QUOTES)
        skipped = head["fn"].startswith("x") or bool(_JS_SKIPS & set(mods))
        found.append(Declaration(text[start + 1:name_end - 1], _count(_JS_ASSERT, text, name_end, end),
                                 skipped))
    return found


# Go and Rust ------------------------------------------------------------

_GO_HEAD = re.compile(r"^func[ \t]+(?P<name>Test(?![a-z])\w*)[ \t]*\(", re.M)
_GO_ASSERT = re.compile(r"(?<![\w.])(?:t|b|tb)\.(?:Errorf?|Fatalf?|Fail|FailNow)\(|(?<![\w.])(?:assert|require)\.\w+\(")
_GO_SKIP = re.compile(r"(?<![\w.])(?:t|b|tb)\.Skip(?:f|Now)?\(")
_RUST_HEAD = re.compile(
    r"#\[(?:\w+::)*test(?:\([^)]*\))?\](?P<attrs>(?:\s*#\[[^\]]*\])*)\s*"
    r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)")
_RUST_ASSERT = re.compile(r"\b\w*assert\w*!")


def _go(text: str) -> list[Declaration]:
    found: list[Declaration] = []
    for head in _GO_HEAD.finditer(text):
        if head["name"] == "TestMain":
            continue
        brace = text.find("{", head.end())
        end = -1 if brace < 0 else _close(text, brace, "\"`", chars=True)
        skipped = None if end < 0 else bool(_GO_SKIP.search(text, brace, end))
        found.append(Declaration(head["name"], _count(_GO_ASSERT, text, brace, end), skipped))
    return found


def _rust(text: str) -> list[Declaration]:
    found: list[Declaration] = []
    for head in _RUST_HEAD.finditer(text):
        brace = text.find("{", head.end())
        end = -1 if brace < 0 else _close(text, brace, "\"", chars=True)
        found.append(Declaration(head["name"], _count(_RUST_ASSERT, text, brace, end),
                                 "#[ignore" in head["attrs"]))
    return found


def scan_kind(path: str) -> bool:
    """Return True when ``scan`` reads the file at ``path`` (by its name only)."""
    return path.endswith((".py", "_test.go", ".rs")) or bool(_SCRIPT.search(path))
