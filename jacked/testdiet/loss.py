"""The loss report: the test declarations that a change removes or weakens.

``loss_report`` reads git only. It needs no history and no test run. It
compares each touched test file of the base commit with the work tree, so a
change that is not committed counts, and an untracked file counts too.

A declaration is a test that the source of a file declares:

- Python: a function or a method whose name starts with ``test``, read with
  ``ast``. A class gives the form ``Class::name``. A file that does not parse
  is read with a line pattern.
- TypeScript and JavaScript: the first string argument of ``test(`` and
  ``it(``, also with ``.each``, ``.skip``, ``.only``, ``.concurrent``,
  ``.failing`` and ``.fixme``, and ``xit(`` and ``xtest(``.
- Go: ``func TestXxx(`` in a ``_test.go`` file. ``TestMain`` is not a test.
- Rust: the function after ``#[test]`` (or an attribute such as ``#[tokio::test]``).

The names of all touched files are compared as one multiset. So a renamed
file, a test that moves to a different touched file and an edited parameter
value give no loss. A test that gets a new name inside one file shows as one
removed and one added name, with net 0.

``fewer_assertions`` and ``skips_added`` are report lines, not a verdict. An
assertion is an ``assert`` statement, or a call whose name starts with
``assert`` (Python), an ``expect`` or ``assert`` call (TypeScript and
JavaScript), ``t.Error``, ``t.Fatal`` and the ``assert.`` and ``require.``
packages (Go), or a macro whose name holds ``assert`` (Rust). A count that
the source cannot give (the Python line pattern, a body that does not close)
is None, and then there is no comparison.
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from jacked.testdiet import tree
from jacked.testdiet.model import one_line

#: The ref of the default branch. There is no fetch and no network call.
DEFAULT_REF = "refs/remotes/origin/HEAD"
#: The name of the default base in the output.
DEFAULT_NAME = "origin/HEAD"
_ZERO = re.compile(r"0+")
_NOT_READ_MODES = ("160000", "120000")  # a submodule and a symbolic link
_SCRIPT = re.compile(r"\.[cm]?[jt]sx?$")
_PY_SKIPS = {"skip", "skipif", "skipIf", "skipUnless"}


class LossError(RuntimeError):
    """An input of the loss report cannot be read (a ref, a tree or a file)."""


@dataclass(frozen=True)
class LossReport:
    """The loss report. Each name has the form ``path::name``.

    ``base`` is the base commit: the merge base of HEAD and ``base_ref``.
    ``base_ref`` is the ref that the user gave, or ``origin/HEAD``. ``net``
    is the number of declarations now,
    minus the number at the base, over the touched files.
    """

    base: str
    base_ref: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    net: int
    fewer_assertions: tuple[str, ...]
    skips_added: tuple[str, ...]


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


# Git --------------------------------------------------------------------

def _rev(top: Path, spec: str) -> str | None:
    code, out = tree._git(top, "rev-parse", "--verify", "-q", "--end-of-options", spec)
    lines = tree._lines(out)
    return lines[0] if code == 0 and len(lines) == 1 else None


def _base_commit(top: Path, base: str | None) -> str:
    """Return the merge base of HEAD and ``base`` (the default branch when ``base`` is None).

    A ref that is an ancestor of HEAD is its own merge base. A branch that
    moved ahead after the fork gives the fork point, so its new tests are
    not read as removed.
    """
    if base is not None:
        tip = _rev(top, f"{base}^{{commit}}")
        if tip is None:
            raise LossError(f"the base {base!r} is not a commit of this repository")
    else:
        tip = _rev(top, f"{DEFAULT_REF}^{{commit}}")
        if tip is None:
            raise LossError(f"{DEFAULT_REF} does not exist. Give --base <branch>, "
                            "or run: git remote set-head origin --auto")
    code, out = tree._git(top, "merge-base", "--end-of-options", "HEAD", tip)
    lines = tree._lines(out)
    if code != 0 or len(lines) != 1:
        name = DEFAULT_NAME if base is None else repr(base)
        raise LossError(f"HEAD and {name} have no merge base. Give a --base that shares history with HEAD.")
    return lines[0]


def _touched(top: Path, base: str, head_tree: str, env: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (path, base blob, head blob) for each touched test file. An empty blob is ``""``."""
    code, out = tree._git(top, "diff-tree", "-r", "-z", "--no-renames", "--end-of-options",
                          base, head_tree, env=env)
    if code != 0:
        raise LossError(f"git diff-tree exited with {code}")
    fields = out.split(b"\0")
    touched = []
    for i in range(0, len(fields) - 1, 2):
        meta = os.fsdecode(fields[i]).lstrip(":").split()
        path = os.fsdecode(fields[i + 1])
        if len(meta) < 4 or not scan_kind(path):
            continue
        blobs = [sha if mode not in _NOT_READ_MODES and not _ZERO.fullmatch(sha) else ""
                 for mode, sha in ((meta[0], meta[2]), (meta[1], meta[3]))]
        touched.append((path, blobs[0], blobs[1]))
    return touched


def scan_kind(path: str) -> bool:
    """Return True when ``scan`` reads the file at ``path`` (by its name only)."""
    return path.endswith((".py", "_test.go", ".rs")) or bool(_SCRIPT.search(path))


def _read(top: Path, blob: str, env: dict[str, str]) -> bytes | None:
    if not blob:
        return None
    code, out = tree._git(top, "cat-file", "blob", blob, env=env)
    if code != 0:
        raise LossError(f"git cannot read the object {blob}")
    return out


def loss_report(cwd: Path, base: str | None = None) -> LossReport:
    """Compare the test declarations of the touched files at ``base`` and in the work tree.

    ``base`` is a ref or a commit. The compared commit is the merge base of
    HEAD and ``base``. With no ``base``, it is the merge base of HEAD and the
    default branch (``refs/remotes/origin/HEAD``). An
    input that cannot be read raises ``LossError`` (or ``tree.TreeError``
    outside a git work tree).
    """
    from jacked.testdiet.record import work_tree_top

    top = work_tree_top(Path(cwd))
    commit = _base_commit(top, base)
    objects = Path(tempfile.mkdtemp(prefix="jacked-loss-"))
    try:
        head = tree.tree_id(top, objects=objects)
        if head is None:
            raise LossError("git cannot give the tree of the work tree")
        code, out = tree._git(top, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        if code != 0:
            raise LossError("git did not give the object path")
        env = {"GIT_OBJECT_DIRECTORY": str(objects),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": tree._alternates(tree._lines(out)[0])}
        sides: tuple[dict[str, list[Declaration]], dict[str, list[Declaration]]] = ({}, {})
        for path, *blobs in _touched(top, commit, head.tree, env):
            for side, blob in zip(sides, blobs):
                data = _read(top, blob, env)
                if data is not None:
                    side[path] = scan(path, data.decode("utf-8", "replace"))
    finally:
        tree.remove_tree(objects)
    return _compare(commit, *sides, base_ref=DEFAULT_NAME if base is None else base)


def _compare(base: str, before: dict[str, list[Declaration]],
             after: dict[str, list[Declaration]], *, base_ref: str = DEFAULT_NAME) -> LossReport:
    old = Counter(d.name for found in before.values() for d in found)
    new = Counter(d.name for found in after.values() for d in found)
    removed = _surplus(before, after, old - new)
    added = _surplus(after, before, new - old)
    weaker, skips = [], []
    for path, now, then in _pairs(before, after):
        if now.asserts is not None and then.asserts is not None and now.asserts < then.asserts:
            weaker.append(f"{path}::{now.name}")
        if now.skipped is True and then.skipped is False:
            skips.append(f"{path}::{now.name}")
    return LossReport(base=base, base_ref=base_ref, added=added, removed=removed, net=sum(new.values()) - sum(old.values()),
                      fewer_assertions=tuple(sorted(weaker)), skips_added=tuple(sorted(skips)))


def _surplus(side: dict[str, list[Declaration]], other: dict[str, list[Declaration]],
             extra: Counter) -> tuple[str, ...]:
    """Name each extra declaration of ``side``, first in the files where its count went down."""
    here = {path: Counter(d.name for d in found) for path, found in side.items()}
    there = {path: Counter(d.name for d in found) for path, found in other.items()}
    names = []
    for name, number in extra.items():
        places = []
        for path in sorted(p for p in here if here[p][name]):
            count = here[path][name]
            places += [(count <= there.get(path, Counter())[name], path)] * count
        names += [f"{path}::{name}" for _, path in sorted(places)[:number]]
    return tuple(sorted(names))


def _pairs(before: dict[str, list[Declaration]], after: dict[str, list[Declaration]]):
    """Pair each test of the work tree with the same test at the base: same file first, then a move."""
    old: dict[tuple[str, str], list[Declaration]] = defaultdict(list)
    for path, found in before.items():
        for d in found:
            old[(path, d.name)].append(d)
    moved: list[tuple[str, Declaration]] = []
    for path in sorted(after):
        for d in after[path]:
            if old[(path, d.name)]:
                yield path, d, old[(path, d.name)].pop(0)
            else:
                moved.append((path, d))
    spare: dict[str, list[Declaration]] = defaultdict(list)
    for (path, name), rest in sorted(old.items()):
        spare[name] += rest
    for path, d in moved:
        if spare[d.name]:
            yield path, d, spare[d.name].pop(0)


def render(report: LossReport) -> str:
    """Return the report as text lines. Each name is on one line, with no control character."""
    from jacked.testdiet.record import clean_text

    lines = [f"base {report.base[:12]} ({one_line(report.base_ref)})", f"net {report.net:+d}" if report.net else "net 0",
             f"added {len(report.added)}, removed {len(report.removed)}"]
    for title, names in (("removed", report.removed), ("added", report.added),
                         ("fewer assertions", report.fewer_assertions),
                         ("skip mark added", report.skips_added)):
        lines += [f"{title} {one_line(name)}" for name in names]
    return clean_text("\n".join(lines))
