"""Ratchet: a log call never formats the text of a caught exception itself.

The dashboard log viewer reaches every dashboard client, remote ones too.
Its boundary (``jacked.api.log_capture``) replaces each exception that a
record carries as an argument with ``describe(exc)``, and the host's log
file keeps the full text. The boundary cannot see text that the call site
already formatted into the message (an f-string, ``str(exc)``, ``%`` or
``.format()``), so this test refuses that shape in ``jacked/``.

Inside ``except ... as exc:``, a log call may use ``exc`` only as:

- a bare argument: ``logger.warning("x failed: %s", exc)``;
- ``exc_info=exc``;
- the argument of a values-free helper (``describe``, ``type``, ...);
- an attribute that holds a code, not text (``exc.code``, ``exc.errno``,
  ``exc.response.status_code``, ...).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "jacked"

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
VALUES_FREE_CALLS = frozenset(
    {"type", "describe", "os_error_reason", "public_message", "_describe_refusal", "isinstance"}
)
CODE_ATTRIBUTES = frozenset(
    {"code", "reason", "size", "errno", "returncode", "status_code", "__class__", "__name__"}
)


def _is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in LOG_METHODS:
        return False
    owner = node.func.value
    if isinstance(owner, ast.Name):
        return "log" in owner.id.lower()
    if isinstance(owner, ast.Attribute):
        return "log" in owner.attr.lower()
    return isinstance(owner, ast.Call)  # logging.getLogger(...).warning(...)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _allowed_use(ref: ast.Name, top: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether one reference to the exception name in a log argument is safe."""
    if ref is top:
        return True  # a bare argument: the dashboard boundary describes it
    node: ast.AST = ref
    parent = parents[node]
    while isinstance(parent, ast.Attribute) and parent.value is node:
        node, parent = parent, parents[parent]
    if node is not ref:
        return isinstance(node, ast.Attribute) and node.attr in CODE_ATTRIBUTES
    if isinstance(parent, ast.Call) and node in parent.args:
        func = parent.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in VALUES_FREE_CALLS
    return False


def violations_in(source: str, filename: str = "<src>") -> list[str]:
    """Return ``file:line`` for each log call that formats a caught exception.

    >>> violations_in("try:\\n    pass\\nexcept Exception as e:\\n    log.warning(f'x {e}')\\n")
    ['<src>:4']
    >>> violations_in("try:\\n    pass\\nexcept Exception as e:\\n    log.warning('x %s', e)\\n")
    []
    """
    tree = ast.parse(source)
    parents = _parents(tree)
    found: list[str] = []
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        for call in ast.walk(handler):
            if not _is_log_call(call):
                continue
            tops = list(call.args) + [
                kw.value for kw in call.keywords if kw.arg != "exc_info"
            ]
            for top in tops:
                refs = [
                    n for n in ast.walk(top)
                    if isinstance(n, ast.Name) and n.id == handler.name
                ]
                if any(not _allowed_use(ref, top, parents) for ref in refs):
                    found.append(f"{filename}:{call.lineno}")
                    break
    return found


@pytest.mark.parametrize(
    "line",
    [
        "log.warning(f'failed: {exc}')",
        "log.warning('failed: %s', str(exc))",
        "log.warning('failed: %r', repr(exc))",
        "log.warning('failed: %s' % exc)",
        "log.warning('failed: {}'.format(exc))",
        "log.warning('failed: %s', exc.args)",
        "log.warning('failed: %s', exc.response.text)",
        "self._logger.error(f'failed: {exc!r}')",
        "logging.getLogger(__name__).info('x %s', f'{exc}')",
    ],
)
def test_detector_refuses_formatted_exception_text(line):
    source = f"try:\n    pass\nexcept Exception as exc:\n    {line}\n"
    assert violations_in(source) == ["<src>:4"]


@pytest.mark.parametrize(
    "line",
    [
        "log.warning('failed: %s', exc)",
        "log.warning('failed', exc_info=exc)",
        "log.warning('failed: %s: %s', type(exc).__name__, exc)",
        "log.warning('failed: %s', describe(exc))",
        "log.warning('failed: code=%s', exc.code)",
        "log.warning('failed: HTTP %s', exc.response.status_code)",
        "log.warning(f'failed for {account_id}: %s', exc)",
        "warnings.warn(f'{exc}')",
    ],
)
def test_detector_allows_values_free_shapes(line):
    source = f"try:\n    pass\nexcept Exception as exc:\n    {line}\n"
    assert violations_in(source) == []


def test_no_log_call_in_jacked_formats_a_caught_exception():
    found: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT.parent).as_posix()
        found.extend(violations_in(path.read_text(encoding="utf-8"), rel))
    assert not found, (
        "These log calls format a caught exception into the message, so the "
        "text bypasses the dashboard redaction boundary. Pass the exception "
        "as a bare argument (`logger.warning('x failed: %s', exc)`), or use "
        "describe(exc) where the text can carry an HTTP or OAuth body:\n  "
        + "\n  ".join(found)
    )
