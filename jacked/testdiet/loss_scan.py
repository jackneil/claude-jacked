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
- Go: each ``t.Run(`` call in the body counts 1. A ``t.Run`` inside a
  ``for _, x := range NAME`` loop, where NAME is a slice composite literal
  that the same function assigns, or a ``range []T{...}`` over an inline
  literal on one line or on many lines, counts the top-level elements of
  that literal. The loop body is the first ``{`` of the for header that
  comes after a space or a ``)``; a ``{`` directly after a type opens a
  literal. A literal that is assigned again before the loop (``NAME =``,
  ``NAME = append(``), a loop over any other value and a for header with no
  body brace give no count.
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
import re
import warnings

from jacked.testdiet import loss_braces
# Declaration and the reasons live in model, so loss_braces needs no import of this module.
from jacked.testdiet.model import (  # noqa: F401 -- loss and the tests read them from here
    BODY_NOT_READ,
    COMPUTED_CASES,
    NOT_PARSED,
    SCRIPT_FILE_RE,
    Declaration,
    body_print,
)

_PY_SKIPS = {"skip", "skipif", "skipIf", "skipUnless"}
#: The default test file name of jest, vitest and Playwright: "<name>.test.ts", "<name>.spec.mjs".
_SCRIPT_TEST_NAME = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")


def declarations(path: str, text: str) -> list[str]:
    """Return the names of the tests that ``text`` declares, in source order.

    >>> declarations("t/test_a.py", "def test_one():\\n    pass\\n")
    ['test_one']
    """
    return [found.name for found in scan(path, text)]


def scan_kind(path: str) -> bool:
    """Return True when ``scan`` reads the file at ``path`` (by its name only).

    A Python file is a test file only by the default file names of pytest
    (``test_*.py``, ``*_test.py``), so a helper ``def test_x`` in
    ``src/db.py`` is not a test. A TypeScript or JavaScript file is a test
    file only by the default names of jest, vitest and Playwright
    (``*.test.*``, ``*.spec.*``, or a file in a ``__tests__`` folder).

    >>> scan_kind("tests/test_a.py"), scan_kind("src/db.py"), scan_kind("web/a.test.ts")
    (True, False, True)
    """
    name = path.rsplit("/", 1)[-1]
    if path.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    if SCRIPT_FILE_RE.search(path):
        return bool(_SCRIPT_TEST_NAME.search(name)) or f"/{path}".count("/__tests__/") > 0
    return path.endswith(("_test.go", ".rs"))


def scan(path: str, text: str) -> list[Declaration]:
    """Return each test that ``text`` declares, with its assertions, cases and skip mark."""
    return scan_file(path, text)[0]


def scan_file(path: str, text: str) -> tuple[list[Declaration], str | None]:
    """Return the tests of ``text``, and the reason when the file as a whole is not read.

    The reason is ``NOT_PARSED`` for a Python file that does not parse. For
    a script, Go or Rust file, it names the line of a comment or a string
    that does not close: the scan of the rest of that file is not sure.
    """
    if path.endswith(".py"):
        return _python(text)
    if SCRIPT_FILE_RE.search(path):
        return loss_braces.script(text)
    if path.endswith("_test.go"):
        return loss_braces.go(text)
    if path.endswith(".rs"):
        return loss_braces.rust(text)
    return [], None


# Python ------------------------------------------------------------------

def _python(text: str) -> tuple[list[Declaration], str | None]:
    text = text[1:] if text.startswith("\ufeff") else text
    try:
        with warnings.catch_warnings():
            # A warning (an escape such as "\d") must not stop the parse.
            warnings.simplefilter("ignore")
            module = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _python_lines(text), NOT_PARSED
    found: list[Declaration] = []
    _python_body(module.body, "", _pytestmark_skips(module.body), 1, found)
    return found, None


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
                                     number, () if number is not None else (COMPUTED_CASES,),
                                     body_print("".join(ast.dump(stmt) for stmt in node.body))))


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
