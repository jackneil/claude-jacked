"""No em-dash (U+2014) in text a user can read.

House rule: product UI copy, CLI output and user-visible errors carry no
em-dash. Write a period, comma, colon or parentheses instead, or a plain
hyphen where a dash is truly needed. Internal text stays exempt: comments,
docstrings and logger calls.

What the scan reads:

* every ``.js`` and ``.html`` file under ``jacked/data/web``. JS comments
  (``//``, ``/* */``), HTML comments (``<!-- -->``) and CSS comments are
  removed (an HTML comment inside a JS string never renders either), a
  ``<script>`` body is read like a ``.js`` file, and lines that
  call ``console.*`` are skipped;
* every ``.py`` file under ``jacked``: each string constant (f-string parts
  too) that is not a bare string statement (a docstring) and not inside a
  logger call.

An escaped dash (``\\u2014``, ``\\u{2014}``) and an HTML entity (``&mdash;``,
``&#8212;``, ``&#x2014;``) count too: they render the same character.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import jacked

EM_DASH = "\u2014"
PACKAGE = Path(jacked.__file__).resolve().parent
WEB = PACKAGE / "data" / "web"
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_DASH_FORMS = re.compile(r"\\u2014|\\u\{2014\}|&mdash;|&#8212;|&#x2014;", re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.S | re.I)
_STYLE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.S | re.I)


def _literal_dashes(src: str) -> str:
    """``src`` with every escaped or entity em-dash turned into the literal one.

    >>> _literal_dashes(r"a \\u2014 b &mdash; c") == "a \u2014 b \u2014 c"
    True
    """
    return _DASH_FORMS.sub(EM_DASH, src)


def _blank(text: str) -> str:
    """Only the newlines of ``text``, so line numbers hold."""
    return "\n" * text.count("\n")


def js_dash_lines(src: str) -> list[int]:
    """Line numbers of em-dashes outside JS comments.

    A small lexer: it tracks strings, template literals (with ``${}``
    nesting) and comments, so ``https://`` inside a string is not a comment.

    >>> js_dash_lines("const a = 'x \u2014 y'; // ok \u2014 here")
    [1]
    >>> js_dash_lines("/* \u2014 */\\nlet b = `t ${c ? '\u2014' : ''}`;")
    [2]
    >>> js_dash_lines(r"const e = '\\u2014';")
    [1]
    """
    src = _literal_dashes(src)
    out: list[int] = []
    templates: list[int] = []  # brace depth inside each open ${ }
    state, quote, line, i, n = "code", "", 1, 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
        if state == "code":
            if c == "/" and nxt == "/":
                state, i = "line", i + 2
                continue
            if c == "/" and nxt == "*":
                state, i = "block", i + 2
                continue
            if c in "'\"":
                state, quote = "str", c
            elif c == "`":
                state = "tpl"
            elif c == "{" and templates:
                templates[-1] += 1
            elif c == "}" and templates:
                if templates[-1] == 0:
                    templates.pop()
                    state = "tpl"
                else:
                    templates[-1] -= 1
            elif c == EM_DASH:
                out.append(line)
        elif state == "line":
            if c == "\n":
                state = "code"
        elif state == "block":
            if c == "*" and nxt == "/":
                state, i = "code", i + 2
                continue
        elif state == "str":
            if c == "\\":
                i += 2
                continue
            if c == quote or c == "\n":
                state = "code"
            elif c == EM_DASH:
                out.append(line)
        else:  # template literal
            if c == "\\":
                i += 2
                continue
            if c == "`":
                state = "code"
            elif c == "$" and nxt == "{":
                templates.append(0)
                state, i = "code", i + 2
                continue
            elif c == EM_DASH:
                out.append(line)
        i += 1
    return out


def html_dash_lines(src: str) -> list[int]:
    """Line numbers of em-dashes in a page's visible text and its scripts.

    >>> html_dash_lines("<!-- \u2014 -->\\n<style>/* \u2014 */</style>\\n<p>a &mdash; b</p>")
    [3]
    >>> html_dash_lines("<script>\\n// \u2014\\nx = '\u2014';\\n</script>")
    [3]
    """
    src = _literal_dashes(_HTML_COMMENT.sub(lambda m: _blank(m.group(0)), src))
    src = _STYLE.sub(
        lambda m: m.group(1) + _CSS_COMMENT.sub(lambda c: _blank(c.group(0)), m.group(2)) + m.group(3),
        src,
    )
    hits: list[int] = []

    def _script(m: re.Match) -> str:
        offset = src.count("\n", 0, m.start(2))
        hits.extend(offset + no for no in js_dash_lines(m.group(2)))
        return m.group(1) + _blank(m.group(2)) + m.group(3)

    rest = _SCRIPT.sub(_script, src)
    hits.extend(no for no, text in enumerate(rest.splitlines(), 1) if EM_DASH in text)
    return sorted(set(hits))


def web_offenders(root: Path = WEB) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".js", ".html"):
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        if path.suffix == ".js":
            # An HTML comment inside a JS template string never renders.
            hits = js_dash_lines(_HTML_COMMENT.sub(lambda m: _blank(m.group(0)), src))
        else:
            hits = html_dash_lines(src)
        for no in sorted(set(hits)):
            if "console." in lines[no - 1]:
                continue
            found.append(f"{path.relative_to(root).as_posix()}:{no}: {lines[no - 1].strip()[:100]}")
    return found


def _exempt_ids(tree: ast.AST) -> set[int]:
    """ids of bare string statements (docstrings, attribute docstrings) and of
    every node inside a logger call."""
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            exempt.add(id(node.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _LOG_METHODS:
            owner = node.func.value
            name = getattr(owner, "id", None) or getattr(owner, "attr", None) or ""
            if "log" in name.lower():
                exempt.update(id(sub) for sub in ast.walk(node))
    return exempt


def python_offenders(root: Path = PACKAGE) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt = _exempt_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and EM_DASH in _literal_dashes(node.value) and id(node) not in exempt):
                found.append((path.relative_to(root).as_posix(), node.lineno, node.value.strip()[:100]))
    return [f"{rel}:{line}: {text!r}" for rel, line, text in sorted(found)]


def test_no_em_dash_in_dashboard_text():
    offenders = web_offenders()
    assert offenders == [], "em-dash in dashboard text:\n" + "\n".join(offenders)


def test_no_em_dash_in_python_user_text():
    offenders = python_offenders()
    assert offenders == [], "em-dash in a user-visible Python string:\n" + "\n".join(offenders)


def test_the_scans_catch_a_planted_dash(tmp_path):
    """Each scan must be able to fail, or a green run proves nothing."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "toast.js").write_text(
        "// a comment \u2014 is fine\n"
        "const url = 'https://example.com'; /* \u2014 */\n"
        "toast(`Saved \u2014 ${n} items`);\n"
        "console.log('debug \u2014 fine');\n"
        "const e = '\\u2014';\n"
        "const h = 'a &mdash; b';\n",
        encoding="utf-8",
    )
    (web / "page.html").write_text(
        "<!-- note \u2014 -->\n"
        "<p>Hi \u2014 there</p>\n"
        "<script>\n"
        "// comment \u2014 fine\n"
        "show('Saved &mdash; ok');\n"
        "</script>\n"
        "<style>/* \u2014 */</style>\n",
        encoding="utf-8",
    )
    hits = web_offenders(web)
    assert [h.split(":")[1] for h in hits if h.startswith("toast.js")] == ["3", "5", "6"]
    assert [h.split(":")[1] for h in hits if h.startswith("page.html")] == ["2", "5"]

    py = tmp_path / "pkg"
    py.mkdir()
    (py / "mod.py").write_text(
        '"""Doc \u2014 fine."""\n'
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f():\n"
        '    """Doc \u2014 fine."""\n'
        "    logger.warning('internal \u2014 fine %s', 1)\n"
        "    raise ValueError(f'Account {1} is disabled \u2014 enable it')\n"
        "    return '&mdash;'\n",
        encoding="utf-8",
    )
    hits = python_offenders(py)
    assert [h.split(":")[1] for h in hits] == ["7", "8"]
