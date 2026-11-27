"""Tests for jacked.testdiet.ids: one normalized test ID for each runner.

The IDs of a real report must equal the IDs of the real listing of the same
project, so that a check of removed and added tests can compare them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jacked.testdiet.ids import detect_runner, ids_from_listing, normalize
from jacked.testdiet.junit import read_junit
from jacked.testdiet.model import ListingError
from tests._testdiet import (
    REPORT_ROOT,
    fixture_listing,
    fixture_project,
    fixture_reports,
    private_hits,
)

LISTED_BY_NAME = ("pytest", "vitest", "playwright", "nextest")    # this tuple is fixed by the plan
# "vitest list" prints no skipped test, even with --no-staticParse.
LISTING_OMITS_SKIPPED = {"vitest"}
LOCATION = re.compile(r":\d+:\d+")


def _read(runner: str):
    return read_junit(fixture_reports(runner), root=fixture_project(runner), report_root=REPORT_ROOT)


def _listed(capture: str, runner: str | None = None) -> list[str]:
    return ids_from_listing(runner or capture, fixture_listing(capture), root=Path(REPORT_ROOT))


@pytest.mark.parametrize("runner", LISTED_BY_NAME)
def test_ids_of_the_report_equal_ids_of_the_listing(runner):
    cases = _read(runner).tests
    if runner in LISTING_OMITS_SKIPPED:
        assert any(case.state == "skipped" for case in cases)
        cases = [case for case in cases if case.state != "skipped"]
    listed = _listed(runner)
    assert len(listed) == len(set(listed))
    assert sorted(listed) == sorted(case.id for case in cases)


def test_jest_lists_files_only_and_each_report_file_is_listed():
    listed = _listed("jest")
    assert sorted(listed) == ["src/other.test.js", "src/sample.test.js"]
    assert all(" > " not in item for item in listed)
    report_files = {case.file for case in _read("jest").tests}
    assert report_files == set(listed)
    for case in _read("jest").tests:
        assert case.id.startswith(case.file + " > ")


def test_go_lists_top_level_tests_and_each_report_id_starts_with_a_listed_name():
    listed = _listed("go")
    assert "example.com/sample/calc.TestTable" in listed
    assert all("/" not in item.rsplit(".", 1)[1] for item in listed)
    for case in _read("go").tests:
        assert any(case.id == name or case.id.startswith(name + "/") for name in listed), case.id
    assert "example.com/sample/calc.TestGroup/nested/in_nested_group" in {c.id for c in _read("go").tests}


def test_pytest_class_parametrized_and_nested_ids_have_the_file():
    cases = {case.id: case for case in _read("pytest").tests}
    for test_id in ("tests/test_sample.py::TestGroup::test_in_class",
                    "tests/test_sample.py::TestGroup::TestNested::test_in_nested_class",
                    "tests/test_sample.py::test_parametrized[1]",
                    "tests/test_sample.py::test_parametrized[2]"):
        assert cases[test_id].file == "tests/test_sample.py"


def test_a_playwright_id_has_no_line_or_column():
    ids = [case.id for case in _read("playwright").tests] + _listed("playwright")
    assert "tests/sample.spec.js › group › nested › in nested group" in ids
    assert LOCATION.search(fixture_listing("playwright"))
    assert not any(LOCATION.search(item) for item in ids)


def test_a_playwright_test_that_moved_down_keeps_its_id():
    assert fixture_listing("playwright") != fixture_listing("playwright_moved")
    before = {case.id for case in _read("playwright").tests}
    after = {case.id for case in _read("playwright_moved").tests}
    assert before == after and "tests/sample.spec.js › passes" in after
    assert sorted(_listed("playwright")) == sorted(_listed("playwright_moved", "playwright"))


@pytest.mark.parametrize("runner,classname,file", [
    ("pytest", "..", None),
    ("pytest", "tests..test_sample", None),
    ("pytest", "tests.test_sample", "../tests/test_sample.py"),
    ("vitest", "../src/a.test.js", None),
    ("vitest", "/abs/src/a.test.js", None),
    ("vitest", "C:\\abs\\a.test.js", None),
    ("vitest", "src//a.test.js", None),
    ("vitest", "C:a.test.js", None),
    ("vitest", "C:..\\x.js", None),
    ("playwright", "C:a.spec.js", None),
    ("jest", "", "C:..\\x.test.js"),
    ("playwright", "tests/../../a.spec.js", None),
    ("jest", "", "../a.test.js"),
    ("jest", "", "/abs/a.test.js"),
    ("go", "example.com/sample/../calc", None),
    ("nextest", "../sample", None),
])
def test_a_class_name_that_leaves_the_root_gives_no_file(tmp_path, runner, classname, file):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_bytes(b"def test_a():\n    pass\n")
    _, found = normalize(runner, classname, "test_a", file, root=tmp_path, report_root=REPORT_ROOT)
    assert found is None


def test_a_listing_of_a_module_that_cannot_import_raises_listing_error():
    with pytest.raises(ListingError, match="error"):
        ids_from_listing("pytest", fixture_listing("pytest_import_error"), root=Path(REPORT_ROOT))


@pytest.mark.parametrize("runner,line", [
    ("vitest", "Error: Failed to load url ./missing.js"),
    ("go", "FAIL\texample.com/sample/calc [build failed]"),
    ("nextest", "error[E0425]: cannot find value `x` in this scope"),
    ("playwright", "Error: No tests found"),
    ("jest", "Error: Could not find a config file"),
])
def test_an_error_line_of_each_listing_raises_listing_error(runner, line):
    with pytest.raises(ListingError):
        ids_from_listing(runner, line + "\n", root=Path(REPORT_ROOT))


def test_a_listing_with_crlf_line_ends_gives_the_same_ids():
    # A Windows checkout can hold the listing with CRLF already.
    text = fixture_listing("pytest").replace("\r\n", "\n")
    assert ids_from_listing("pytest", text.replace("\n", "\r\n"), root=Path(".")) == _listed("pytest")


def test_go_names_of_two_packages_stay_apart():
    text = "TestA\nok  \texample.com/m/a\t0.1s\nTestA\nok  \texample.com/m/b\t0.1s\n"
    assert ids_from_listing("go", text, root=Path(".")) == ["example.com/m/a.TestA", "example.com/m/b.TestA"]


@pytest.mark.parametrize("runner", ["pytest", "vitest", "jest", "playwright", "go", "nextest"])
def test_detect_runner_names_each_real_report(runner):
    assert read_junit(fixture_reports(runner), root=fixture_project(runner)).runner == runner


def test_detect_runner_gives_unknown_for_a_report_it_does_not_know():
    assert detect_runner({"name": "custom"}, {"classname": "a.b"}) == "unknown"


def test_private_hits_finds_a_login_as_a_whole_word():
    assert private_hits("made by jack.", host="box.lan", login="jack", home="/home/jack") == ["jack"]
    assert private_hits("JACK ran it", host="box.lan", login="jack", home="/home/jack") == ["jack"]


def test_private_hits_accepts_a_word_that_only_contains_the_login():
    assert private_hits("jacked jack_x xjack 1jack", host="box.lan", login="jack", home="/home/jack") == []


def test_private_hits_finds_the_home_path_with_either_separator():
    for text in ("at /home/jack/x", "at \\home\\jack\\x", "AT /HOME/JACK/x"):
        assert "/home/jack" in private_hits(text, host="h.lan", login="zz", home="/home/jack")
    assert private_hits("C:/Users/jo/x", host="h", login="zz", home="C:\\Users\\jo") == ["C:\\Users\\jo"]


def test_private_hits_finds_the_host_and_its_first_part():
    assert private_hits("on box.lan", host="box.lan", login="zz", home="/h/zz") == ["box.lan", "box"]
    assert private_hits("on box today", host="box.lan", login="zz", home="/h/zz") == ["box"]
    assert private_hits("on boxes", host="box.lan", login="zz", home="/h/zz") == []


# --------------------------------------------------------------------------- the lookup memo


class _Counter:
    def __init__(self, monkeypatch):
        from jacked.testdiet import ids

        self.calls = 0
        for name in ("_is_file_in_root", "_read_tree_file"):
            real = getattr(ids, name)
            monkeypatch.setattr(ids, name, self._wrap(real))
        real_listdir, real_walk = ids.os.listdir, ids.os.walk
        monkeypatch.setattr(ids.os, "listdir", self._wrap(real_listdir))
        monkeypatch.setattr(ids.os, "walk", self._wrap(real_walk))
        monkeypatch.setattr(ids.tomllib, "loads", self._wrap(ids.tomllib.loads))

    def _wrap(self, func):
        def counted(*args, **kwargs):
            self.calls += 1
            return func(*args, **kwargs)
        return counted


def _lookup_tree(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_bytes(b"def test_x():\n    pass\n")
    (root / "go.mod").write_bytes(b"module example.com/m\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "a_test.go").write_bytes(b"package pkg\n\nfunc TestA(t *testing.T) {}\n")
    (root / "Cargo.toml").write_bytes(b'[package]\nname = "crate"\n')
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_bytes(b"#[test]\nfn test_b() {}\n")
    return root


@pytest.mark.parametrize("call", [
    lambda lookup: lookup.pytest_module("tests.test_a"),
    lambda lookup: lookup.go_file("example.com/m/pkg", "TestA"),
    lambda lookup: lookup.rust_file("crate", "test_b"),
], ids=["pytest", "go", "nextest"])
def test_a_second_lookup_of_the_same_key_does_not_touch_the_file_system(tmp_path, monkeypatch, call):
    from jacked.testdiet.ids import TreeLookup

    lookup = TreeLookup(_lookup_tree(tmp_path))
    counter = _Counter(monkeypatch)
    first = call(lookup)
    assert first is not None and counter.calls > 0
    before = counter.calls
    assert call(lookup) == first
    assert counter.calls == before


def test_the_go_lookup_lists_a_package_folder_one_time(tmp_path, monkeypatch):
    from jacked.testdiet.ids import TreeLookup

    root = _lookup_tree(tmp_path)
    (root / "pkg" / "b_test.go").write_bytes(b"package pkg\n\nfunc TestB(t *testing.T) {}\n")
    lookup = TreeLookup(root)
    from jacked.testdiet import ids

    listed = []
    real = ids.os.listdir
    monkeypatch.setattr(ids.os, "listdir", lambda path: listed.append(path) or real(path))
    assert lookup.go_file("example.com/m/pkg", "TestA") == "pkg/a_test.go"
    assert lookup.go_file("example.com/m/pkg", "TestB") == "pkg/b_test.go"
    assert len(listed) == 1


def test_a_playwright_location_needs_ascii_digits():
    from jacked.testdiet import ids

    assert ids._PLAYWRIGHT_LOCATION.match("a.spec.ts:12:3 › t")
    assert ids._PLAYWRIGHT_LOCATION.match("a.spec.ts:١٢:3 › t") is None
