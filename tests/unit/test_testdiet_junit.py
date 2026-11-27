"""Tests for jacked.testdiet.junit: real reports of six runners in one form.

The sample reports come from real runs (tests/fixtures/testdiet/capture.py).
Nobody edits them by hand. A few tests write a small report into tmp_path
to prove one rule of the reader.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from jacked.testdiet import junit
from jacked.testdiet.junit import read_junit
from jacked.testdiet.model import JUnitError
from tests._testdiet import (
    FIXTURES,
    PROJECTS,
    REPORT_ROOT,
    fixture_project,
    fixture_reports,
    provenance,
)

RUNNERS = ["pytest", "vitest", "jest", "playwright", "go", "nextest"]
REPO_ROOT = Path(__file__).resolve().parents[2]
# cargo-nextest does not write a test that it did not run (an ignored test)
# into its JUnit file; the real report has no skipped case.
REPORTS_SKIPPED = {"pytest", "vitest", "jest", "playwright", "go"}


def _read(runner: str):
    return read_junit(fixture_reports(runner), root=fixture_project(runner), report_root=REPORT_ROOT)


def _write(tmp_path: Path, body: str, name: str = "junit.xml") -> Path:
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    return path


@pytest.mark.parametrize("runner", RUNNERS)
def test_real_report_gives_the_normalized_fields(runner):
    data = _read(runner)
    assert data.runner == runner
    states = {case.state for case in data.tests}
    assert {"passed", "failed"} <= states
    assert ("skipped" in states) == (runner in REPORTS_SKIPPED)
    assert data.seconds_reported is not None and data.seconds_reported > 0
    for case in data.tests:
        assert case.id and case.attempts == 1
        assert case.state in ("passed", "failed", "skipped")
        assert case.seconds is None or case.seconds >= 0
    assert len({case.id for case in data.tests}) == len(data.tests)


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_test_that_sleeps_half_a_second_reads_as_half_a_second(runner):
    data = read_junit(fixture_reports(runner), root=fixture_project(runner), report_root=REPORT_ROOT)
    slept = [t for t in data.tests if "sleeps_half_a_second" in t.id]
    assert len(slept) == 1 and 0.4 <= slept[0].seconds <= 5.0


@pytest.mark.parametrize("runner", RUNNERS)
def test_each_case_of_a_real_report_has_a_file_inside_the_project(runner):
    project = fixture_project(runner)
    data = _read(runner)
    for case in data.tests:
        assert case.file is not None, case.id
        assert not case.file.startswith("/") and ".." not in case.file.split("/")
        assert (project / case.file).is_file(), case.file


def test_a_report_path_outside_the_report_root_gives_no_file(tmp_path):
    report = _write(tmp_path, (
        '<testsuites name="vitest tests"><testsuite name="s">'
        '<testcase classname="/elsewhere/src/a.test.js" name="t" time="0.1"/>'
        '<testcase classname="/work/src/a.test.js" name="u" time="0.1"/>'
        "</testsuite></testsuites>"))
    data = read_junit([report], root=tmp_path, report_root="/work")
    files = {case.id: case.file for case in data.tests}
    assert files == {"/elsewhere/src/a.test.js > t": None, "src/a.test.js > u": "src/a.test.js"}


def _raw_texts(path: Path) -> list[str]:
    """Each message attribute and each element text of a raw report."""
    texts = []
    for element in ET.parse(path).iter():
        message = element.get("message")
        if message and message.strip():
            texts.append(message.strip())
        if element.text and element.text.strip():
            texts.append(element.text.strip())
    return texts


@pytest.mark.parametrize("runner", RUNNERS)
def test_no_message_and_no_text_of_a_failure_is_kept(runner):
    data = _read(runner)
    texts = [text for path in fixture_reports(runner) for text in _raw_texts(path)]
    assert texts, "the sample report holds a failure text"
    kept = repr(data)
    for text in texts:
        assert text not in kept
    assert "sample-failure-message" not in kept


def test_the_failure_type_is_kept_as_a_class_name():
    failed = [case for case in _read("vitest").tests if case.state == "failed"]
    assert [case.failure_type for case in failed] == ["AssertionError"]
    # pytest and jest write no type, and gotestsum writes an empty one.
    for runner in ("pytest", "jest", "go"):
        assert [c.failure_type for c in _read(runner).tests if c.state == "failed"] == [None]


def test_a_test_that_failed_and_passed_on_a_rerun_is_flaky():
    """A control: it passed before the b-L3 per-runner rule too, and it must keep passing.

    gotestsum --rerun-fails writes the failed attempt and the passed attempt.
    """
    data = read_junit(fixture_reports("go_rerun"), root=fixture_project("go_rerun"), report_root=REPORT_ROOT)
    cases = {case.id: case for case in data.tests}
    rerun = cases["example.com/sample/calc.TestFailsThenPasses"]
    assert (rerun.state, rerun.attempts, rerun.failure_type) == ("flaky", 2, None)
    assert cases["example.com/sample/calc.TestPasses"].state == "passed"


def test_the_pytest_rerun_report_shows_two_attempts_and_no_failure():
    """A control of b-L3: it passes on the code before the per-runner rule by design."""
    # pytest-rerunfailures writes the failed first attempt as an empty
    # testcase: the report does not show that it failed. The state is the
    # state that the report shows.
    data = read_junit(fixture_reports("pytest_rerun"), root=fixture_project("pytest_rerun"),
                      report_root=REPORT_ROOT)
    cases = {case.id: case for case in data.tests}
    rerun = cases["tests/test_rerun.py::test_fails_then_passes"]
    assert (rerun.state, rerun.attempts) == ("passed", 2)
    assert cases["tests/test_rerun.py::test_passes"].attempts == 1


def test_a_flaky_failure_element_counts_as_a_failed_attempt(tmp_path):
    report = _write(tmp_path, (
        '<testsuites name="nextest-run"><testsuite name="c">'
        '<testcase classname="c" name="t" time="1.5"><flakyFailure type="panic" message="m">x</flakyFailure>'
        "</testcase>"
        '<testcase classname="c" name="u" time="2"><failure type="panic"/><rerunFailure type="panic"/>'
        "</testcase></testsuite></testsuites>"))
    cases = {c.id: c for c in read_junit([report], root=tmp_path).tests}
    assert (cases["c t"].state, cases["c t"].attempts, cases["c t"].failure_type) == ("flaky", 2, "panic")
    assert (cases["c u"].state, cases["c u"].attempts) == ("failed", 2)


def test_a_passed_and_a_skipped_attempt_is_not_flaky(tmp_path):
    report = _write(tmp_path, (
        '<testsuites name="vitest tests"><testsuite name="s">'
        '<testcase classname="a.test.js" name="t" time="0.25"/>'
        '<testcase classname="a.test.js" name="t" time="0.5"><skipped/></testcase>'
        "</testsuite></testsuites>"))
    (case,) = read_junit([report], root=tmp_path).tests
    assert (case.state, case.attempts, case.seconds) == ("passed", 2, 0.75)


def test_a_testcase_with_failure_and_error_is_an_error(tmp_path):
    report = _write(tmp_path, (
        '<testsuites name="vitest tests"><testsuite name="s">'
        '<testcase classname="a.test.js" name="t"><failure type="F"/><error type="E"/></testcase>'
        "</testsuite></testsuites>"))
    (case,) = read_junit([report], root=tmp_path).tests
    assert (case.state, case.failure_type, case.seconds) == ("error", "E", None)


def test_two_report_files_with_one_id_give_one_case(tmp_path):
    first = fixture_reports("vitest")[0]
    second = tmp_path / "copy.xml"
    shutil.copyfile(first, second)
    one = _read("vitest")
    two = read_junit([first, second], root=fixture_project("vitest"), report_root=REPORT_ROOT)
    assert [c.id for c in two.tests] == [c.id for c in one.tests]
    for single, double in zip(one.tests, two.tests):
        assert double.attempts == 2
        assert double.state == single.state
        if single.seconds is not None:
            assert double.seconds == pytest.approx(2 * single.seconds)


def test_a_truncated_file_raises_junit_error(tmp_path):
    raw = fixture_reports("pytest")[0].read_bytes()
    report = tmp_path / "cut.xml"
    report.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(JUnitError):
        read_junit([report], root=tmp_path)


def test_a_file_with_a_doctype_raises_junit_error(tmp_path):
    report = _write(tmp_path, '<?xml version="1.0"?><!DOCTYPE testsuites SYSTEM "x.dtd"><testsuites/>')
    with pytest.raises(JUnitError, match="DOCTYPE"):
        read_junit([report], root=tmp_path)


def test_a_file_with_an_entity_declaration_raises_before_a_case_is_read(tmp_path, monkeypatch):
    report = _write(tmp_path, (
        '<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        '<testsuites><testsuite><testcase classname="c" name="&b;"/></testsuite></testsuites>'))
    seen: list[str] = []
    real_start = junit._Target.start

    def start(self, tag, attrib):
        seen.append(tag)
        return real_start(self, tag, attrib)

    monkeypatch.setattr(junit._Target, "start", start)
    with pytest.raises(JUnitError, match="DOCTYPE"):
        read_junit([report], root=tmp_path)
    assert seen == []


def test_a_path_that_is_not_a_regular_file_raises_junit_error(tmp_path):
    with pytest.raises(JUnitError):
        read_junit([tmp_path], root=tmp_path)
    with pytest.raises(JUnitError):
        read_junit([tmp_path / "missing.xml"], root=tmp_path)


def test_the_report_of_a_collection_error_has_an_error_case_for_the_module():
    data = read_junit(fixture_reports("pytest_import_error"), root=fixture_project("pytest_import_error"),
                      report_root=REPORT_ROOT)
    (case,) = data.tests
    assert (case.id, case.file, case.state) == ("tests/test_broken.py", "tests/test_broken.py", "error")


def _fixture_files() -> list[Path]:
    # A __pycache__ folder is local output of a run, and git ignores it.
    return [p for p in FIXTURES.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


def test_no_fixture_holds_a_host_name_or_a_home_path():
    """No fixture holds a host name or a home path.

    This test cannot know the login of the machine that captured the reports
    (on CI the login is the one of the runner), so capture.py checks the
    login with private_hits at capture time.
    """
    import re

    files = _fixture_files()
    assert files
    for path in files:
        text = path.read_bytes().decode("utf-8", errors="replace")
        if path.suffix in (".xml", ".json"):
            for value in re.findall(r'hostname="([^"]*)"', text):
                assert value == "gate.example", path.name
        for marker in ("/Users/", "/home/", "\\Users\\"):
            assert marker not in text, f"{path.name} holds {marker}"
    reports = [p for p in files if p.suffix == ".xml"]
    assert any('hostname="gate.example"' in p.read_text(encoding="utf-8") for p in reports)


def test_provenance_names_the_two_rewrites_as_the_only_changes():
    data = provenance()
    assert data["changes"] == [
        "The absolute root of the temporary project is replaced by /work.",
        "Each hostname attribute is replaced by gate.example.",
    ]
    names = {run["name"] for run in data["runs"]}
    assert set(RUNNERS) | {"pytest_rerun", "pytest_import_error", "playwright_moved", "go_rerun"} == names
    listed = {name for run in data["runs"] for name in run["files"]}
    assert listed == {path.name for path in (FIXTURES / "reports").iterdir()}


def test_pytest_collects_nothing_under_the_fixture_projects():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER")}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         str(PROJECTS.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "::" not in proc.stdout


def test_the_fixture_folder_has_no_npm_manifest():
    banned = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"}
    for path in FIXTURES.rglob("*"):
        assert path.name not in banned and path.name != "node_modules", path


def test_an_old_expat_is_refused_with_its_version(tmp_path, monkeypatch):
    import pyexpat

    from jacked.testdiet.model import JUnitError

    report = tmp_path / "r.xml"
    report.write_bytes(b'<testsuites><testsuite name="s"><testcase name="t"/></testsuite></testsuites>')
    monkeypatch.setattr(pyexpat, "version_info", (2, 2, 9))
    with pytest.raises(JUnitError) as caught:
        read_junit([report], root=tmp_path)
    assert "2.2.9" in str(caught.value) and "2.4.1" in str(caught.value)


# --------------------------------------------------------------------------- failures at suite level


@pytest.mark.parametrize("body, count", [
    (b'<testsuites failures="1"><testsuite name="s" failures="1"><testcase classname="a" name="t"/>'
     b'</testsuite></testsuites>', 1),
    (b'<testsuite name="s"><failure message="setup broke"/><testcase classname="a" name="t"/></testsuite>', 1),
    (b'<testsuites errors="2"><testsuite name="s" errors="2"><testcase classname="a" name="t"><error/>'
     b'</testcase></testsuite></testsuites>', 1),
    (b'<testsuites failures="1"><testsuite name="s" failures="1"><testcase classname="a" name="t"><failure/>'
     b'</testcase></testsuite></testsuites>', 0),
])
def test_a_failure_that_only_the_suite_counts_makes_exit_0_a_conflict(tmp_path, body, count):
    from jacked.testdiet import history

    report = tmp_path / "r.xml"
    report.write_bytes(body)
    data = read_junit([report], root=tmp_path)
    assert data.suite_failures == count
    assert history.verdict_of(0, data) == ("conflict" if count else history.verdict_of(0, data))
    assert history.verdict_of(1, data) == "failed"


def test_each_sample_report_has_no_failure_at_suite_level_only():
    from tests._testdiet import REPORTS, REPORT_ROOT

    paths = sorted(REPORTS.glob("*.xml"))
    assert len(paths) >= 10
    for path in paths:
        assert read_junit([path], root=Path("/nowhere"), report_root=REPORT_ROOT).suite_failures == 0, path.name


# --------------------------------------------------------------------------- same-ID elements, by runner


def test_the_go_rerun_capture_stays_flaky_and_its_gate_stays_passed():
    """A control of b-L3: it passes on the code before the per-runner rule by design.

    The rule changed the other runners; this pins that the real go capture keeps its result.
    """
    from jacked.testdiet import history

    data = read_junit(fixture_reports("go_rerun"), root=fixture_project("go_rerun"), report_root=REPORT_ROOT)
    assert {c.id: c.state for c in data.tests}["example.com/sample/calc.TestFailsThenPasses"] == "flaky"
    assert history.verdict_of(0, data) == "passed"


@pytest.mark.parametrize("children, state", [
    (("", "<failure/>"), "failed"),           # passed, then failed: the last attempt decides
    (("<failure/>", "<failure/>"), "failed"),
    (("<failure/>", ""), "flaky"),             # failed, then passed
    (("<failure/>", "<error/>"), "error"),
])
def test_a_go_report_gives_each_id_the_state_of_its_last_attempt(tmp_path, children, state):
    cases = "".join(f'<testcase classname="example.com/x" name="TestA">{c}</testcase>' for c in children)
    report = _write(tmp_path, f'<testsuites><testsuite name="example.com/x">{cases}</testsuite></testsuites>')
    (case,) = read_junit([report], root=tmp_path, runner="go").tests
    assert (case.state, case.attempts) == (state, 2)


@pytest.mark.parametrize("runner", ["jest", "vitest", "pytest", "playwright", "nextest"])
def test_two_tests_with_one_id_in_one_report_are_failing_when_one_failed(tmp_path, runner):
    from jacked.testdiet import history

    report = _write(tmp_path, (
        '<testsuites name="jest tests"><testsuite name="a.test.js">'
        '<testcase classname="works" name="works" file="a.test.js"/>'
        '<testcase classname="works" name="works" file="a.test.js"><failure type="E"/></testcase>'
        "</testsuite></testsuites>"))
    (case,) = read_junit([report], root=tmp_path, runner=runner).tests
    assert (case.state, case.failure_type) == ("failed", "E")
    data = read_junit([report], root=tmp_path, runner=runner)
    assert history.verdict_of(0, data) == "conflict" and history.failing_of(data) == (case.id,)


def test_a_suite_count_with_non_ascii_digits_counts_zero(tmp_path):
    report = _write(tmp_path, '<testsuites failures="١"><testsuite name="s" failures="١">'
                              '<testcase classname="a" name="t"/></testsuite></testsuites>')
    assert read_junit([report], root=tmp_path).suite_failures == 0
