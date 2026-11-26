"""Shared helpers for the tests of jacked.testdiet.

Import them as ``from tests._testdiet import ...``. Each test module that
needs a repository defines its own fixture from ``make_repo``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "testdiet"
REPORTS = FIXTURES / "reports"
PROJECTS = FIXTURES / "projects"
REPORT_ROOT = "/work"

# A sample report has the name of its capture. Most captures use the project
# of the same name.
_PROJECT_OF = {"pytest_rerun": "pytest_rerun", "pytest_import_error": "pytest_import_error"}


def fixture_reports(runner: str) -> list[Path]:
    """Return the JUnit files of the sample capture ``runner``."""
    path = REPORTS / f"{runner}.xml"
    if not path.is_file():
        raise FileNotFoundError(f"no sample report {path.name}; run tests/fixtures/testdiet/capture.py")
    return [path]


def fixture_project(runner: str) -> Path:
    """Return the folder of the sample project that the capture ``runner`` ran.

    In the sample reports, ``/work`` is this folder.
    """
    return PROJECTS / _PROJECT_OF.get(runner, runner)


def fixture_listing(runner: str) -> str:
    """Return the text of the test listing of the sample capture ``runner``."""
    return (REPORTS / f"{runner}.list.txt").read_bytes().decode("utf-8")


def provenance() -> dict:
    """Return the content of PROVENANCE.json."""
    return json.loads((FIXTURES / "PROVENANCE.json").read_bytes())


def private_hits(text: str, *, host: str, login: str, home: str) -> list[str]:
    """Return each private value that ``text`` holds.

    The home path matches as text, with ``/`` or ``\\`` as the separator. The
    host name (the full name and its first part) and the login name match as
    a whole word: the character before and the character after are not a
    letter, a digit or an underscore. The match ignores case.

    >>> private_hits("built by jack.", host="box.lan", login="jack", home="/home/jack")
    ['jack']
    >>> private_hits("jacked", host="box.lan", login="jack", home="/home/jack")
    []
    """
    hits: list[str] = []
    lowered = text.lower()
    if home:
        forms = {home, home.replace("\\", "/"), home.replace("/", "\\")}
        for form in sorted(forms):
            if form and form.lower() in lowered and home not in hits:
                hits.append(home)
    words = [login]
    if host:
        words += [host, host.split(".", 1)[0]]
    for word in words:
        if not word or word in hits:
            continue
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            hits.append(word)
    return hits


def make_repo(path: Path) -> Path:
    """Make a git repository at ``path`` with one commit, and return ``path``.

    The git config is hermetic, as in tests/unit/test_git_reality.py: no
    system file, an empty global file and a fixed author. Call it inside a
    test that set ``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL`` and the
    author variables, or give those values here through the environment of
    the test.
    """
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, f"git {args[0]} failed: {proc.stderr}"

    git("init", "-q", "-b", "main")
    (path / "README.md").write_bytes(b"sample\n")
    git("add", "README.md")
    git("-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "first")
    return path
