"""Every headless ``claude -p`` launch in jacked goes through one helper.

``jacked.memory.capture.call_claude`` is the only place that may build a
``claude -p`` argv. It gives the session no tools, no MCP server and no hooks,
because its prompt is untrusted transcript text. A second launch site would
not inherit those flags, so this scan fails when one appears.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import jacked

PACKAGE = Path(jacked.__file__).resolve().parent
PRINT_FLAGS = {"-p", "--print"}
ALLOWED = {("memory/capture.py", "call_claude")}


def _launch_sites():
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            consts = [e.value for e in node.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if not PRINT_FLAGS.intersection(consts):
                continue
            # `ps -p <pid>` is a process lookup, not a Claude session.
            first = node.elts[0] if node.elts else None
            if isinstance(first, ast.Constant) and first.value == "ps":
                continue
            func = node
            while func is not None and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = parents.get(func)
            yield rel, func.name if func else "<module>", node.lineno


def test_only_call_claude_builds_a_print_mode_argv():
    sites = list(_launch_sites())
    unexpected = [s for s in sites if (s[0], s[1]) not in ALLOWED]
    assert unexpected == [], (
        "a new `claude -p` launch site must go through "
        "jacked.memory.capture.call_claude so it gets no tools, no MCP and no hooks: "
        f"{unexpected}"
    )
    # The scan must still see the one allowed site, or it proves nothing.
    assert [(s[0], s[1]) for s in sites] == sorted(ALLOWED)


def _schemas_passed_to_call_claude():
    """Every ``schema=<NAME>`` handed to ``call_claude``, resolved to its value."""
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).with_suffix("").as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "call_claude":
                continue
            for kw in node.keywords:
                if kw.arg == "schema" and isinstance(kw.value, ast.Name):
                    module = importlib.import_module("jacked." + rel.replace("/", "."))
                    yield f"{rel}:{node.lineno}", getattr(module, kw.value.id)


def test_every_structured_output_schema_has_an_object_root():
    """The API takes the schema as a tool ``input_schema``, which needs
    ``"type": "object"`` at the root. A top-level anyOf/oneOf/allOf is refused
    with HTTP 400, and the caller then silently falls back."""
    found = list(_schemas_passed_to_call_claude())
    assert len(found) >= 2, found  # triage + merge distillation
    for where, schema in found:
        assert schema.get("type") == "object", where
        assert not {"anyOf", "oneOf", "allOf"} & set(schema), where


def _docstring_ids(tree):
    """ids of bare string statements: docstrings and attribute docstrings."""
    return {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_no_claude_print_command_hidden_in_a_string():
    """Second net: a launch written as one string (``shlex.split("claude -p
    ...")``, ``shell=True``) has no list literal for the scan above to see."""
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs
                    and ("claude -p" in node.value or "claude --print" in node.value)):
                hits.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert hits == [], hits
