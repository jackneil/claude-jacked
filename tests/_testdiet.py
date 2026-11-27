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


#: The machine identities of ``fill_store``. Fixed values, so a test can
#: prove that no view prints them.
LOCAL_ID = "1f" * 16
LOCAL_TWIN_ID = "2e" * 16
TREE_1 = "a1" * 20
TREE_2 = "b2" * 20
COMMIT_1 = "c1" * 20
COMMIT_2 = "c2" * 20
COMMAND = "d4" * 32
#: The failing set that ``fill_store`` puts in three runs.
FAILING_ID = "tests/test_calc.py::test_fails"


def make_case(test_id: str, file: str | None, seconds: float | None, state: str = "passed",
          failure_type: str | None = None):
    from jacked.testdiet.model import CaseResult

    return CaseResult(id=test_id, file=file, seconds=seconds, state=state,
                      failure_type=failure_type, attempts=1)


def store_run(store: Path, when: str, *, exit_code: int | None, tests=None, kind: str | None = "full",
              machine: str = "local", machine_id: str | None = LOCAL_ID, tree: str | None = TREE_1,
              commit: str = COMMIT_1, command_sha: str | None = COMMAND, source: str = "run",
              wall: float | None = 100.0, on_default_branch: bool | None = None,
              folder: str | None = ".") -> str:
    """Write one run into ``store`` and return its run ID.

    ``when`` is the UTC time of the run ID, as ``YYYYMMDDTHHMMSS``. ``tests``
    is a list of CaseResult, or None for a run with no report.
    """
    from datetime import datetime, timezone

    from jacked.testdiet import history
    from jacked.testdiet.model import SCHEMA, STATES, RunData, RunMeta, TreeId

    moment = datetime.strptime(when, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    data = None if tests is None else RunData(runner="pytest", tests=tuple(tests), seconds_reported=None)
    cases = () if data is None else data.tests
    meta = RunMeta(
        id=history.new_run_id(moment), started=moment.isoformat(timespec="seconds"), source=source,
        wall_seconds=wall, exit_code=exit_code, verdict=history.verdict_of(exit_code, data),
        kind=kind, label=None, runner=None if data is None else "pytest", command_sha=command_sha,
        inputs_sha=None, machine=machine, machine_id=machine_id,
        tree=None if tree is None else TreeId(commit=commit, tree=tree, clean=True),
        tree_changed=None if tree is None else False, on_default_branch=on_default_branch,
        counts=None if data is None else {s: sum(1 for c in cases if c.state == s) for s in STATES},
        seconds_reported=None, load_start=None, load_end=None, child_cpu_seconds=None,
        failing=tuple(sorted(c.id for c in cases if c.state in ("failed", "error"))),
        flaky=tuple(sorted(c.id for c in cases if c.state == "flaky")),
        failing_sig=history.failing_signature(cases), folder=folder, schema=SCHEMA,
    )
    history.write_run(store, meta, data)
    return meta.id


def fill_store(path: Path) -> Path:
    """Fill a history store at ``path`` and return ``path``.

    Two machines: ``local`` (identity ``LOCAL_ID``) and ``ci`` (records with
    no identity). Two trees. Green and red verdicts. The failing set of
    ``FAILING_ID`` is in three runs of ``local``. The newest full run is a
    green run of ``local`` on ``TREE_1`` whose files hold 10, 5, 3, 1 and 1
    seconds, and one test with no seconds.
    """
    green = [make_case("tests/test_calc.py::test_adds", "tests/test_calc.py", 1.0)]
    red = green + [make_case(FAILING_ID, "tests/test_calc.py", 0.5, "failed", "AssertionError")]
    newest = [
        make_case("tests/test_a.py::test_one", "tests/test_a.py", 6.0),
        make_case("tests/test_a.py::test_two", "tests/test_a.py", 4.0),
        make_case("tests/test_b.py::test_one", "tests/test_b.py", 5.0),
        make_case("tests/test_c.py::test_one", "tests/test_c.py", 3.0),
        make_case("tests/test_d.py::test_one", "tests/test_d.py", 1.0),
        make_case("tests/test_e.py::test_one", "tests/test_e.py", 1.0),
        make_case("tests/test_f.py::test_no_time", "tests/test_f.py", None),
    ]
    flaky = green + [make_case("tests/test_calc.py::test_flaky", "tests/test_calc.py", 0.2, "flaky")]
    store_run(path, "20261120T080000", exit_code=0, tests=green, wall=100.0, on_default_branch=True)
    store_run(path, "20261121T080000", exit_code=0, tests=flaky, wall=120.0, on_default_branch=True)
    store_run(path, "20261122T080000", exit_code=1, tests=red, tree=TREE_2, commit=COMMIT_2)
    store_run(path, "20261122T090000", exit_code=1, tests=red, tree=TREE_2, commit=COMMIT_2,
              kind="targeted")
    store_run(path, "20261123T080000", exit_code=1, tests=red, tree=TREE_2, commit=COMMIT_2)
    # The ci machine: records of a different machine, with no identity.
    ci = dict(machine="ci", machine_id=None, source="record", command_sha=None, wall=40.0)
    store_run(path, "20261124T080000", exit_code=0, tests=[make_case("ci::test_slow", "tests/test_ci.py", 30.0),
                                                           make_case("ci::test_fast", "tests/test_ci.py", 2.0)],
              **ci)
    store_run(path, "20261124T090000", exit_code=None, tests=green, **ci)
    store_run(path, "20261124T100000", exit_code=0, tests=None, **ci)
    store_run(path, "20261124T110000", exit_code=0, tests=red, **ci)
    store_run(path, "20261125T080000", exit_code=0, tests=newest, wall=90.0, on_default_branch=True)
    return path
