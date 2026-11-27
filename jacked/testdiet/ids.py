"""One normalized test ID for each runner, and the IDs of a test listing.

The ID of each runner has the form that its own listing prints, so the IDs
of a report and the IDs of a listing can be compared:

========== ================================================ =====================
Runner     ID                                               Listing
========== ================================================ =====================
pytest     ``tests/test_a.py::Class::test_b[1]``            ``pytest --collect-only -q``
vitest     ``src/a.test.js > group > test``                 ``vitest list --no-staticParse``
jest       ``src/a.test.js > group > test``                 ``jest --listTests`` (files only)
playwright ``tests/a.spec.js › group › test``               ``playwright test --list``
go         ``example.com/mod/pkg.TestA/sub``                ``go test -list . ./...`` (top level only)
nextest    ``crate tests::test_a`` (binary ID, test name)   ``cargo nextest list``
========== ================================================ =====================

The jest ID needs these jest-junit settings: ``addFileAttribute`` true,
``classNameTemplate`` ``{classname}``, ``titleTemplate`` ``{title}`` and
``ancestorSeparator`` `` > ``. The Playwright ID has no line and no column, so
a test that moves in its file keeps its ID.

A JUnit file of pytest, Go and nextest holds no file path. For these runners
the file comes from a lookup in the tree. A lookup that the tree does not
prove gives ``None``. The ``file`` of a case is always a relative POSIX path
inside the root.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from jacked.regular_file import open_regular_fd
from jacked.testdiet.model import SCRIPT_FILE_RE, ListingError

_SKIP_DIRS = {".git", "node_modules", "target", "vendor", "__pycache__"}
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")
_PLAYWRIGHT_LOCATION = re.compile(r"^(?P<file>.+?):\d+:\d+(?P<rest> › .*)$")


def _read_tree_file(path: Path) -> bytes | None:
    """Return the bytes of a regular file of the tree, or None."""
    try:
        fd = open_regular_fd(path)
    except OSError:
        return None
    try:
        parts = []
        while chunk := os.read(fd, 1 << 16):
            parts.append(chunk)
        return b"".join(parts)
    finally:
        os.close(fd)


def _is_file_in_root(root: Path, rel: str) -> bool:
    path = root / rel
    if not path.is_file():
        return False
    real_root = os.path.realpath(root)
    return os.path.realpath(path).startswith(real_root.rstrip(os.sep) + os.sep)


def relative_path(text: str, *, root: Path, report_root: str | None = None) -> str | None:
    """Return ``text`` as a relative POSIX path inside the root, or None.

    An absolute path that starts with ``report_root`` (or with the root
    itself) becomes relative to it. The match is on text with ``/``
    separators. A path that is absolute in a different way, or that holds
    ``..``, ``.``, an empty part or a part with ``:`` (a Windows drive),
    gives None.

    >>> relative_path("/work/src/a.js", root=Path("/x"), report_root="/work")
    'src/a.js'
    >>> relative_path("../a.js", root=Path("/x")) is None
    True
    """
    value = text.replace("\\", "/")
    if value.startswith("/") or _WINDOWS_ABSOLUTE.match(value):
        prefixes = [report_root] if report_root else []
        prefixes += [root.as_posix(), Path(os.path.realpath(root)).as_posix()]
        for prefix in prefixes:
            prefix = prefix.replace("\\", "/").rstrip("/")
            if prefix and value.startswith(prefix + "/"):
                value = value[len(prefix) + 1:]
                break
        else:
            return None
    if value.startswith("./"):
        value = value[2:]
    parts = value.split("/")
    # A part with ":" can be a Windows drive ("C:a.js" is relative to the
    # current folder of drive C, not to the root) or an alternate data stream.
    if not value or any(part in ("", ".", "..") or ":" in part for part in parts):
        return None
    return value


class TreeLookup:
    """Lookups in the tree for runners whose report holds no file path.

    One object reads each file, lists each folder and answers each lookup
    one time: the answers are kept for the life of the object, because the
    tree does not change during one read. Make a new one for each read.
    """

    def __init__(self, root: Path):
        self.root = root
        self._manifests: dict[str, list[str]] = {}
        self._texts: dict[str, bytes | None] = {}
        self._memo: dict[tuple, object] = {}

    def _cached(self, key: tuple, compute):
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def _in_root(self, rel: str) -> bool:
        return self._cached(("file", rel), lambda: _is_file_in_root(self.root, rel))

    def _listing(self, folder: str) -> list[str] | None:
        def read() -> list[str] | None:
            try:
                return sorted(os.listdir(self.root / folder))
            except OSError:
                return None
        return self._cached(("listdir", folder), read)

    def _text(self, rel: str) -> bytes | None:
        if rel not in self._texts:
            self._texts[rel] = _read_tree_file(self.root / rel) if self._in_root(rel) else None
        return self._texts[rel]

    def manifests(self, name: str) -> list[str]:
        """Return the relative paths of each file called ``name`` in the tree."""
        if name not in self._manifests:
            found = []
            for folder, dirs, files in os.walk(self.root):
                dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
                if name in files:
                    rel = Path(os.path.relpath(Path(folder) / name, self.root)).as_posix()
                    found.append(rel)
            self._manifests[name] = found
        return self._manifests[name]

    def pytest_module(self, dotted: str) -> tuple[str, list[str]] | None:
        """Find the longest dotted prefix that is a ``.py`` file. Return it and the rest."""
        return self._cached(("pytest", dotted), lambda: self._pytest_module(dotted))

    def _pytest_module(self, dotted: str) -> tuple[str, list[str]] | None:
        parts = dotted.split(".")
        if not dotted or any(part in ("", "..") for part in parts):
            return None
        for end in range(len(parts), 0, -1):
            rel = "/".join(parts[:end]) + ".py"
            if relative_path(rel, root=self.root) and self._in_root(rel):
                return rel, parts[end:]
        return None

    def go_file(self, package: str, test: str) -> str | None:
        """Find the ``_test.go`` file of ``package`` that declares ``func test(``."""
        return self._cached(("go", package, test), lambda: self._go_file(package, test))

    def _go_file(self, package: str, test: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", test):
            return None
        declares = re.compile(rb"^func\s+" + re.escape(test.encode()) + rb"\s*\(", re.MULTILINE)
        for mod_file in self.manifests("go.mod"):
            match = re.search(rb"^module\s+\"?([^\s\"]+)", self._text(mod_file) or b"", re.MULTILINE)
            if not match:
                continue
            module = match.group(1).decode("utf-8", errors="replace")
            if package == module:
                sub = ""
            elif package.startswith(module + "/"):
                sub = package[len(module) + 1:]
            else:
                continue
            parent = Path(mod_file).parent.as_posix()
            folder = "/".join(p for p in ("" if parent == "." else parent, sub) if p)
            if folder and relative_path(folder, root=self.root) is None:
                return None
            names = self._listing(folder)
            if names is None:
                return None
            for name in names:
                rel = f"{folder}/{name}" if folder else name
                if name.endswith("_test.go") and declares.search(self._text(rel) or b""):
                    return rel
        return None

    def _crate_dirs(self) -> dict[str, str]:
        return self._cached(("crates",), self._read_crate_dirs)

    def _read_crate_dirs(self) -> dict[str, str]:
        crates: dict[str, str] = {}
        for rel in self.manifests("Cargo.toml"):
            try:
                data = tomllib.loads((self._text(rel) or b"").decode("utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue
            name = data.get("package", {}).get("name") if isinstance(data.get("package"), dict) else None
            if isinstance(name, str):
                folder = Path(rel).parent.as_posix()
                crates.setdefault(name, "" if folder == "." else folder)
                crates.setdefault(name.replace("-", "_"), "" if folder == "." else folder)
        return crates

    def rust_file(self, binary_id: str, test: str) -> str | None:
        """Find the file that declares the nextest test ``test`` of ``binary_id``."""
        return self._cached(("rust", binary_id, test), lambda: self._rust_file(binary_id, test))

    def _rust_file(self, binary_id: str, test: str) -> str | None:
        crate, _, kind = binary_id.partition("::")
        crates = self._crate_dirs()
        if crate not in crates:
            return None
        base = crates[crate]

        def join(*parts: str) -> str:
            return "/".join(p for p in (base, *parts) if p)

        if not kind:
            entries = [join("src", "lib.rs")]
        elif kind.startswith("bin/"):
            name = kind[4:]
            entries = [join("src", "bin", f"{name}.rs"), join("src", "bin", name, "main.rs")]
            if name in (crate, crate.replace("_", "-")):
                entries.insert(0, join("src", "main.rs"))
        else:
            folder = {"example": "examples", "bench": "benches"}.get(kind.partition("/")[0], "tests")
            name = kind.partition("/")[2] if "/" in kind else kind
            entries = [join(folder, f"{name}.rs"), join(folder, name, "main.rs")]
        entry = next((e for e in entries if relative_path(e, root=self.root) and self._in_root(e)), None)
        if entry is None:
            return None
        parts = test.split("::")
        modules, leaf = parts[:-1], parts[-1]
        entry_path = Path(entry)
        module_base = entry_path.parent if entry_path.name in ("lib.rs", "main.rs", "mod.rs") else (
            entry_path.parent / entry_path.stem)
        found = entry
        for end in range(len(modules), 0, -1):
            stem = (module_base / "/".join(modules[:end])).as_posix()
            hit = next((c for c in (f"{stem}.rs", f"{stem}/mod.rs")
                        if relative_path(c, root=self.root) and self._in_root(c)), None)
            if hit:
                found = hit
                break
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", leaf):
            return None
        declares = re.compile(rb"\bfn\s+" + re.escape(leaf.encode()) + rb"\b")
        return found if declares.search(self._text(found) or b"") else None


def _normalize(runner: str, classname: str, name: str, file: str | None, root: Path,
               report_root: str | None, lookup: TreeLookup) -> tuple[str, str | None]:
    if runner == "pytest":
        found = None
        rel = relative_path(file, root=root, report_root=report_root) if file else None
        if rel and rel.endswith(".py"):
            module = rel[:-3].replace("/", ".")
            if classname == module or classname.startswith(module + "."):
                found = (rel, classname[len(module) + 1:].split(".") if classname != module else [])
        if found is None and file is None:
            found = lookup.pytest_module(classname) if classname else lookup.pytest_module(name)
        if found is None:
            return (f"{classname}::{name}" if classname else name), None
        path, rest = found
        if not classname:
            return "::".join([path, *rest]), path
        return "::".join([path, *rest, name]), path
    if runner in ("vitest", "playwright"):
        rel = relative_path(classname, root=root, report_root=report_root) if classname else None
        separator = " > " if runner == "vitest" else " › "
        head = rel or classname
        return (f"{head}{separator}{name}" if head else name), rel
    if runner == "jest":
        rel = relative_path(file, root=root, report_root=report_root) if file else None
        return " > ".join(p for p in (rel or file, classname, name) if p), rel
    if runner == "go":
        return f"{classname}.{name}", lookup.go_file(classname, name.split("/", 1)[0])
    if runner == "nextest":
        return f"{classname} {name}", lookup.rust_file(classname, name)
    rel = relative_path(file, root=root, report_root=report_root) if file else None
    return (f"{classname}::{name}" if classname else name), rel


def normalize(runner: str, classname: str, name: str, file: str | None, *, root: Path,
              report_root: str | None = None, lookup: TreeLookup | None = None) -> tuple[str, str | None]:
    """Return the normalized ID and the file of one test case.

    ``classname``, ``name`` and ``file`` are the attributes of the
    ``testcase`` element (``file`` is None when it has none). ``lookup``
    reads the tree at ``root``; give one ``TreeLookup`` for all the cases
    of one report, so each file and folder is read one time.

    >>> normalize("vitest", "src/a.test.js", "group > b", None, root=Path("/x"))
    ('src/a.test.js > group > b', 'src/a.test.js')
    """
    return _normalize(runner, classname, name, file, root, report_root, lookup or TreeLookup(root))


def _listing_error(runner: str, line: str) -> bool:
    stripped = line.strip()
    if runner == "pytest":
        return bool(re.match(r"^(ERROR|E\s|=+ ERRORS =+$)", stripped)
                    or re.search(r"\d+ errors? (in|during)", stripped))
    if runner == "go":
        return bool(re.match(r"^(FAIL\b|#|go: |can't load package)", stripped))
    if runner == "nextest":
        return bool(re.match(r"^error(\[|:)", stripped))
    return bool(re.match(r"^(Error\b|FAIL\b|SyntaxError\b)", stripped))


def ids_from_listing(runner: str, text: str, *, root: Path) -> list[str]:
    """Return the test IDs of a listing, in the form of ``normalize``.

    The listing of jest names files only, and the listing of Go names the
    top-level tests only. An error line raises ``ListingError``.

    >>> ids_from_listing("pytest", "a.py::t\\n\\n1 test collected in 0.01s\\n", root=Path("."))
    ['a.py::t']
    """
    ids: list[str] = []
    pending: list[str] = []
    # pytest prints the IDs first; after the first blank line come the
    # summary and the errors. The reader looks for an error in each line.
    in_ids = True
    for line in text.splitlines():
        if _listing_error(runner, line):
            raise ListingError(f"the {runner} listing holds an error line: {line.strip()[:200]}")
        stripped = line.strip()
        if not stripped:
            in_ids = in_ids and not (runner == "pytest" and ids)
            continue
        if runner == "pytest":
            if in_ids and "::" in stripped:
                ids.append(stripped)
        elif runner == "vitest":
            head, sep, _ = stripped.partition(" > ")
            if sep and SCRIPT_FILE_RE.search(head):
                ids.append(stripped)
        elif runner == "jest":
            rel = relative_path(stripped, root=root)
            if rel:
                ids.append(rel)
        elif runner == "playwright":
            match = _PLAYWRIGHT_LOCATION.match(stripped)
            if match:
                ids.append(match.group("file") + match.group("rest"))
        elif runner == "go":
            ok = re.match(r"^ok\s+(\S+)", stripped)
            if ok:
                ids.extend(f"{ok.group(1)}.{name}" for name in pending)
                pending = []
            elif re.fullmatch(r"(Test|Benchmark|Example|Fuzz)\w*", stripped):
                pending.append(stripped)
        elif runner == "nextest":
            if re.fullmatch(r"\S+ \S+", stripped):
                ids.append(stripped)
        else:
            ids.append(stripped)
    if pending:
        raise ListingError(f"the {runner} listing has test names with no package line")
    return ids


def detect_runner(suite_attributes: Mapping[str, str], first_case: Mapping[str, str]) -> str:
    """Return the runner that wrote a report, or ``"unknown"``.

    ``suite_attributes`` holds the attributes of the root element, plus the
    properties of the first suite (a ``property`` named ``go.version`` is
    the key ``go.version``). ``first_case`` holds the attributes of the
    first ``testcase``.

    >>> detect_runner({"name": "vitest tests"}, {})
    'vitest'
    """
    name = suite_attributes.get("name", "")
    by_name = {"pytest tests": "pytest", "pytest": "pytest", "vitest tests": "vitest",
               "jest tests": "jest", "nextest-run": "nextest"}
    if name in by_name:
        return by_name[name]
    if "go.version" in suite_attributes:
        return "go"
    if "id" in suite_attributes and SCRIPT_FILE_RE.search(first_case.get("classname", "")):
        return "playwright"
    return "unknown"
