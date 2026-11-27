"""Tests for jacked.testdiet.loss and the command ``jacked tests loss``.

Each repository is a throwaway one under ``tmp_path`` with a hermetic git
config. The base is the commit that ``refs/remotes/origin/HEAD`` names, as
the default base of the command, or a ``--base`` value.
"""

from __future__ import annotations

import json
import os
import shutil
import warnings
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked.cli import main
from jacked.testdiet import loss, loss_scan
from tests._testdiet import make_repo

CALC = b"""\
import pytest


def test_adds():
    assert 1 + 1 == 2


def test_subtracts():
    assert 2 - 1 == 1
    assert 3 - 1 == 2


@pytest.mark.parametrize("n", [1, 2])
def test_doubles(n):
    assert n * 2 == n + n
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
    global_config = tmp_path / "gitconfig-global"
    global_config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)
    path = make_repo(tmp_path / "repo")
    write(path, "tests/test_calc.py", CALC)
    commit(path, "base tests")
    # The default branch, as a clone has it, with no remote and no fetch.
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(path, "checkout", "-q", "-b", "work")
    monkeypatch.chdir(path)
    return path


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def write(repo: Path, rel: str, data: bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def no_loss(report: loss.LossReport) -> None:
    assert (report.removed, report.added, report.net) == ((), (), 0)
    assert (report.fewer_assertions, report.skips_added) == ((), ())
    assert (report.fewer_cases, report.not_read, report.files_not_read) == ((), (), ())


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_a_removed_test_is_named_and_net_is_minus_one(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"def test_adds():\n    assert 1 + 1 == 2\n", b""))
    commit(repo, "drop one test")
    report = loss.loss_report(repo)
    assert report.removed == ("tests/test_calc.py::test_adds",)
    assert (report.added, report.net) == ((), -1)
    assert report.base == git(repo, "rev-parse", "main").strip()
    assert report.base_ref == "origin/HEAD"


def test_a_renamed_file_with_the_same_tests_reports_no_loss(repo):
    git(repo, "mv", "tests/test_calc.py", "tests/test_math.py")
    commit(repo, "rename")
    report = loss.loss_report(repo)
    no_loss(report)
    assert ("tests/test_calc.py::test_adds", "tests/test_math.py::test_adds") in report.moved


def test_a_test_moved_to_a_different_file_reports_no_loss(repo):
    block = b"def test_adds():\n    assert 1 + 1 == 2\n"
    write(repo, "tests/test_calc.py", CALC.replace(block, b""))
    write(repo, "tests/test_more.py", block)
    commit(repo, "move one test")
    no_loss(loss.loss_report(repo))


def test_an_edited_parameter_value_reports_no_loss(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"[1, 3, 5]"))
    commit(repo, "edit a parameter")
    no_loss(loss.loss_report(repo))


def test_added_tests_are_reported_as_growth(repo):
    write(repo, "tests/test_calc.py", CALC + b"\n\ndef test_multiplies():\n    assert 2 * 2 == 4\n")
    write(repo, "tests/test_new.py", b"def test_one():\n    assert True\n")
    commit(repo, "add tests")
    report = loss.loss_report(repo)
    assert report.added == ("tests/test_calc.py::test_multiplies", "tests/test_new.py::test_one")
    assert (report.removed, report.net) == ((), 2)


def test_a_method_in_a_test_class_is_a_declaration(repo):
    body = b"class TestCalc:\n    def test_adds(self):\n        assert 1 + 1 == 2\n\n    def helper(self):\n        pass\n"
    assert loss.declarations("tests/test_cls.py", body.decode()) == ["TestCalc::test_adds"]
    write(repo, "tests/test_cls.py", body)
    commit(repo, "class")
    write(repo, "tests/test_cls.py", b"class TestCalc:\n    def helper(self):\n        pass\n")
    report = loss.loss_report(repo, "HEAD")
    assert report.removed == ("tests/test_cls.py::TestCalc::test_adds",)
    assert report.net == -1


def test_a_test_with_one_assertion_less_is_named(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"    assert 3 - 1 == 2\n", b""))
    commit(repo, "weaken")
    report = loss.loss_report(repo)
    assert report.fewer_assertions == ("tests/test_calc.py::test_subtracts",)
    assert (report.removed, report.net) == ((), 0)


def test_a_new_skip_mark_is_named(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"def test_adds", b"@pytest.mark.skip\ndef test_adds"))
    commit(repo, "skip")
    report = loss.loss_report(repo)
    assert report.skips_added == ("tests/test_calc.py::test_adds",)
    assert (report.removed, report.net, report.fewer_assertions) == ((), 0, ())


def test_a_change_that_is_not_committed_counts(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"def test_adds():\n    assert 1 + 1 == 2\n", b""))
    write(repo, "tests/test_untracked.py", b"def test_new():\n    assert True\n")
    report = loss.loss_report(repo)
    assert report.removed == ("tests/test_calc.py::test_adds",)
    assert report.added == ("tests/test_untracked.py::test_new",)
    assert report.net == 0
    assert git(repo, "status", "--porcelain") == " M tests/test_calc.py\n?? tests/test_untracked.py\n", \
        "the real index must not change"


TS = """\
import { test, it, expect, describe } from "vitest";

describe("calc", () => {
  test("adds", () => { expect(1 + 1).toBe(2); });
  it('subtracts', () => { expect(2 - 1).toBe(1); expect(3 - 1).toBe(2); });
  test.each([[1], [2]])("doubles %i", (n) => { expect(n * 2).toBe(n + n); });
  test.each`
    a    | b
    ${1} | ${2}
  `("tagged $a", ({ a, b }) => { expect(a).toBeLessThan(b); });
  test.skip("later", () => { expect(true).toBe(true); });
  it.only(`only one`, () => {});
  test.describe("not a test", () => {});
  const ok = /x/.test("not a test");
  obj.it("not a test");
});
"""
GO = """\
package calc

import "testing"

func TestAdd(t *testing.T) {
\tif 1+1 != 2 {
\t\tt.Errorf("bad")
\t}
}

func TestLater(t *testing.T) {
\tt.Skip("later")
}

func TestMain(m *testing.M) {}

func Testing() {}

func helper() {}
"""
RUST = """\
fn helper() {}

#[test]
fn adds() {
    assert_eq!(1 + 1, 2);
    assert!(true);
}

#[test]
#[ignore]
fn later() {}

#[tokio::test]
async fn async_one() {
    assert!(true);
}

fn not_a_test() {}
"""
TS_GO_RUST_CASES = [
    (".ts", TS, ["adds", "subtracts", "doubles %i", "tagged $a", "later", "only one"]),
    (".js", TS, ["adds", "subtracts", "doubles %i", "tagged $a", "later", "only one"]),
    ("_test.go", GO, ["TestAdd", "TestLater"]),
    (".rs", RUST, ["adds", "later", "async_one"]),
]


@pytest.mark.parametrize("ext,body,expected", TS_GO_RUST_CASES)
def test_declarations_of_typescript_go_and_rust(ext, body, expected):
    assert loss.declarations("src/calc" + ext, body) == expected


@pytest.mark.parametrize("ext,body,asserts,skipped", [
    (".ts", TS, {"adds": 1, "subtracts": 2, "later": 1, "only one": 0},
     {"adds": False, "later": True, "only one": False}),
    ("_test.go", GO, {"TestAdd": 1, "TestLater": 0}, {"TestAdd": False, "TestLater": True}),
    (".rs", RUST, {"adds": 2, "later": 0, "async_one": 1}, {"adds": False, "later": True}),
])
def test_assertions_and_skip_marks_of_typescript_go_and_rust(ext, body, asserts, skipped):
    found = {d.name: d for d in loss.scan("src/calc" + ext, body)}
    assert {name: found[name].asserts for name in asserts} == asserts
    assert {name: found[name].skipped for name in skipped} == skipped


def test_a_go_file_that_is_not_a_test_file_has_no_declarations():
    assert loss.declarations("calc.go", GO) == []


def test_cli_loss_exits_0_with_a_loss_and_with_none(repo):
    none = _invoke("tests", "loss")
    assert none.exit_code == 0
    assert "net 0" in none.output
    write(repo, "tests/test_calc.py", CALC.replace(b"def test_adds():\n    assert 1 + 1 == 2\n", b""))
    lost = _invoke("tests", "loss")
    assert lost.exit_code == 0
    assert "net -1" in lost.output
    assert "removed tests/test_calc.py::test_adds" in lost.output
    data = json.loads(_invoke("tests", "loss", "--json").output)
    assert data["removed"] == ["tests/test_calc.py::test_adds"] and data["net"] == -1
    assert (data["base"], data["base_ref"]) == (git(repo, "rev-parse", "main").strip(), "origin/HEAD")
    assert lost.output.startswith(f"base {data['base'][:12]} (origin/HEAD)\n")
    explicit = _invoke("tests", "loss", "--base", "main")
    assert explicit.exit_code == 0 and "net -1" in explicit.output


def test_cli_loss_gives_one_line_and_exit_1_for_an_input_it_cannot_read(repo, tmp_path, monkeypatch):
    bad = _invoke("tests", "loss", "--base", "no-such-ref")
    assert bad.exit_code == 1
    assert bad.output.count("\n") == 1 and "no-such-ref" in bad.output
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    nodefault = _invoke("tests", "loss")
    assert nodefault.exit_code == 1
    assert nodefault.output.count("\n") == 1 and "--base" in nodefault.output
    assert "git remote set-head origin --auto" in nodefault.output
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    away = _invoke("tests", "loss", "--base", "main")
    assert away.exit_code == 1
    assert away.output.count("\n") == 1
    assert away.output.startswith("jacked tests: this folder is not in the work tree of a git repository (")
    assert "not a git repository" in away.output.split("(", 1)[1]


def test_a_test_name_with_a_control_character_prints_on_one_line(repo):
    write(repo, "tests/test_names.ts", b'test("bad\x1b[31m na\\nme", () => {});\n')
    commit(repo, "names")
    write(repo, "tests/test_names.ts", b"\n")
    out = _invoke("tests", "loss", "--base", "HEAD").output
    line = next(line for line in out.splitlines() if line.startswith("removed "))
    assert "\x1b" not in out
    assert line.startswith("removed tests/test_names.ts::bad")


def test_a_python_file_with_a_syntax_error_still_gives_declarations(repo):
    broken = CALC + b"\n\ndef test_broken(:\n"
    names = loss.declarations("tests/test_calc.py", broken.decode())
    assert names == ["test_adds", "test_subtracts", "test_doubles", "test_broken"]
    nested = b"class TestA:\n    def test_one(self):\n        pass\n\ndef test_two(:\n"
    assert loss.declarations("t/test_x.py", nested.decode()) == ["TestA::test_one", "test_two"]
    write(repo, "tests/test_calc.py", broken)
    report = loss.loss_report(repo)
    assert report.added == ("tests/test_calc.py::test_broken",)
    assert report.removed == ()
    # The line pattern cannot count assertions, so no test is named as weaker.
    assert (report.fewer_assertions, report.skips_added) == ((), ())
    assert report.not_read == ()
    assert report.files_not_read == (("tests/test_calc.py", "does not parse"),)
    out = _invoke("tests", "loss").output
    assert "not read file tests/test_calc.py (does not parse)" in out.splitlines()


def _link_or_skip(link: Path, target: str) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this system cannot make a symbolic link")


def test_a_symbolic_link_is_not_read(repo):
    _link_or_skip(repo / "tests" / "test_link.py", "test_calc.py")
    report = loss.loss_report(repo)
    assert (report.removed, report.added, report.net) == ((), (), 0)
    assert report.files_not_read == (("tests/test_link.py", "symbolic link"),)
    assert report.not_read == ()


def test_a_regular_file_that_becomes_a_symbolic_link_is_not_read_and_not_removed(repo):
    (repo / "tests" / "test_calc.py").unlink()
    write(repo, "tests/real.py", CALC)
    _link_or_skip(repo / "tests" / "test_calc.py", "real.py")
    out = _invoke("tests", "loss").output
    assert not [line for line in out.splitlines() if line.startswith("removed ")]
    assert [line for line in out.splitlines() if line.startswith("not read ")] == [
        "not read file tests/test_calc.py (type change)"]


def test_one_of_two_tests_with_one_name_is_removed_from_the_file_that_lost_it():
    one = loss.Declaration("works", 1, False)
    before = {"a.test.ts": [one, one], "b.test.ts": [one]}
    after = {"a.test.ts": [one], "b.test.ts": [one]}
    report = loss._compare("base", before, after)
    assert (report.removed, report.added, report.net) == (("a.test.ts::works",), (), -1)


def test_a_python_warning_does_not_turn_the_file_into_the_line_pattern():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        found = loss.scan("t/test_a.py", 'def test_a():\n    assert "\\d"\n')
    assert [(d.name, d.asserts) for d in found] == [("test_a", 1)]


def test_a_python_file_with_a_byte_order_mark_is_read_with_ast():
    found = loss.scan("t/test_a.py", "\ufeffdef test_a():\n    assert 1\n")
    assert [(d.name, d.asserts) for d in found] == [("test_a", 1)]


def test_a_rust_char_literal_with_a_brace_does_not_move_the_end_of_a_body():
    body = ("mod tests {\n    #[test]\n    fn one() {\n        let c = '{';\n        assert!(c == '{');\n    }\n"
            "    #[test]\n    fn two() {\n        let d = '}';\n        assert_eq!(d, '}');\n        assert!(true);\n    }\n}\n")
    assert {d.name: d.asserts for d in loss.scan("src/lib.rs", body)} == {"one": 1, "two": 2}


def test_a_test_name_with_a_real_line_break_prints_on_one_line(repo):
    write(repo, "tests/test_lines.ts", b"test(`first\nsecond`, () => {});\n")
    commit(repo, "names")
    write(repo, "tests/test_lines.ts", b"\n")
    out = _invoke("tests", "loss", "--base", "HEAD").output
    assert "removed tests/test_lines.ts::first\\nsecond" in out.splitlines()


def test_a_base_branch_that_moved_ahead_reports_no_loss_for_its_new_tests(repo):
    git(repo, "checkout", "-q", "main")
    write(repo, "tests/test_calc.py", CALC + b"\n\ndef test_on_main_only():\n    assert True\n")
    commit(repo, "main moves ahead")
    fork = git(repo, "merge-base", "main", "work").strip()
    git(repo, "checkout", "-q", "work")
    write(repo, "tests/test_calc.py", CALC + b"\n\ndef test_on_work():\n    assert True\n")
    report = loss.loss_report(repo, "main")
    assert (report.base, report.base_ref) == (fork, "main")
    assert (report.removed, report.added, report.net) == ((), ("tests/test_calc.py::test_on_work",), 1)
    out = _invoke("tests", "loss", "--base", "main")
    assert out.exit_code == 0 and f"base {fork[:12]} (main)" in out.output


def test_a_base_with_no_shared_history_exits_1_with_one_line(repo):
    git(repo, "checkout", "-q", "--orphan", "other")
    git(repo, "rm", "-rq", "--cached", ".")
    commit(repo, "unrelated")
    git(repo, "checkout", "-q", "-f", "work")
    out = _invoke("tests", "loss", "--base", "other")
    assert out.exit_code == 1
    assert out.output.count("\n") == 1 and "no merge base" in out.output


# The scanners: cases, bodies that are not read, and the gaps of the first review.

def by_name(path: str, body: str) -> dict[str, loss.Declaration]:
    return {d.name: d for d in loss.scan(path, body)}


def test_a_jsx_apostrophe_marks_only_its_own_body_as_not_read():
    body = ('test("before", () => { expect(1).toBe(1); });\n'
            'test("jsx", () => {\n  render(<p>Don\'t</p>);\n  expect(1).toBe(1);\n});\n'
            'test("after", () => { expect(2).toBe(2); });\n')
    found = by_name("a.test.tsx", body)
    assert (found["jsx"].asserts, found["jsx"].unread) == (None, (loss_scan.BODY_NOT_READ,))
    assert (found["before"].asserts, found["after"].asserts) == (1, 1)
    assert found["after"].unread == ()


def test_python_cases_are_the_product_of_the_literal_parametrize_lists():
    body = ('import pytest\n\n@pytest.mark.parametrize("a", [1, 2])\nclass TestA:\n'
            '    @pytest.mark.parametrize("b", (1, 2, 3))\n    def test_x(self, a, b):\n        pass\n\n'
            '@pytest.mark.parametrize("n", VALUES)\ndef test_computed(n):\n    pass\n\n'
            'def test_plain():\n    pass\n')
    found = by_name("t/test_a.py", body)
    assert found["TestA::test_x"].cases == 6
    assert (found["test_computed"].cases, found["test_computed"].unread) == (None, (loss_scan.COMPUTED_CASES,))
    assert found["test_plain"].cases == 1


def test_js_cases_are_the_rows_of_a_literal_each_table():
    body = ('test.each([[1, 2], [3, 4], [5, 6],])("row %i", () => {});\n'
            'test.each`\n  a | b\n  ${1} | ${2}\n  ${3} | ${4}\n`("tagged $a", () => {});\n'
            'test.each(rows)("computed", () => {});\n'
            'test("plain", () => {});\n')
    found = by_name("a.test.ts", body)
    assert [found[n].cases for n in ("row %i", "tagged $a", "computed", "plain")] == [3, 2, None, 1]
    assert found["computed"].unread == (loss_scan.COMPUTED_CASES,)


def test_go_reads_the_receiver_name_and_counts_its_subtests():
    body = ('package calc\n\nfunc TestTable(tt *testing.T) {\n\ttt.Run("a", func(tt *testing.T) {})\n'
            '\ttt.Run("b", func(tt *testing.T) { tt.Fatal("x") })\n\ttt.Errorf("y")\n}\n\n'
            'func TestSkip(tt *testing.T) {\n\ttt.Skip("later")\n}\n')
    found = by_name("calc_test.go", body)
    assert (found["TestTable"].cases, found["TestTable"].asserts) == (2, 2)
    assert (found["TestSkip"].skipped, found["TestTable"].skipped) == (True, False)


def test_a_js_test_inside_a_comment_is_not_a_declaration():
    body = ('// test("in a line comment", () => {});\n/* it("in a block comment", () => {}); */\n'
            'test("real", () => {});\n')
    assert loss.declarations("a.test.js", body) == ["real"]


def test_rust_allows_attributes_and_doc_comments_between_the_test_attribute_and_fn():
    body = ('#[test]\n/// Adds.\n#[should_panic]\n// #[ignore] is only a comment here\nfn adds() {\n'
            '    assert!(true);\n}\n')
    found = by_name("src/lib.rs", body)
    assert (found["adds"].asserts, found["adds"].skipped) == (1, False)


def test_python_finds_tests_in_an_if_block_and_reads_pytestmark_and_an_in_body_skip():
    body = ('import pytest, sys\npytestmark = [pytest.mark.skipif(sys.platform == "x", reason="r")]\n\n'
            'if sys.version_info >= (3, 9):\n    def test_new():\n        pass\nelse:\n'
            '    def test_old():\n        pass\n')
    assert [(d.name, d.skipped) for d in loss.scan("t/test_a.py", body)] == [
        ("test_new", True), ("test_old", True)]
    cls = ('import pytest\n\nclass TestA:\n    pytestmark = pytest.mark.skip\n\n    def test_one(self):\n'
           '        pass\n\ndef test_body():\n    pytest.skip("later")\n\ndef test_free():\n    pass\n')
    assert [(d.name, d.skipped) for d in loss.scan("t/test_b.py", cls)] == [
        ("TestA::test_one", True), ("test_body", True), ("test_free", False)]


def test_the_line_pattern_skips_a_nested_def_and_keeps_the_class_across_a_docstring():
    body = ('class TestA:\n    """Doc.\n\nColumn zero text.\n"""\n\n    def test_one(self):\n'
            '        def test_helper():\n            pass\n\ndef test_two(:\n')
    found = loss.scan("t/test_a.py", body)
    assert [d.name for d in found] == ["TestA::test_one", "test_two"]
    assert {d.unread for d in found} == {(loss_scan.NOT_PARSED,)}


def test_a_recursion_error_of_the_parser_uses_the_line_pattern(monkeypatch):
    def deep(*args, **kwargs):
        raise RecursionError("too deep")

    monkeypatch.setattr(loss_scan.ast, "parse", deep)
    found = loss.scan("t/test_a.py", "def test_one():\n    assert 1\n")
    assert [(d.name, d.asserts, d.unread) for d in found] == [("test_one", None, (loss_scan.NOT_PARSED,))]


def test_a_large_file_with_bodies_that_do_not_close_is_read_in_one_pass():
    import time

    body = 'test("t%d", () => {\n' * 4000
    start = time.monotonic()
    found = [d for d in loss.scan("big.test.js", body % tuple(range(4000)))]
    assert len(found) == 4000 and all(d.asserts is None for d in found)
    assert time.monotonic() - start < 5.0


# The report: not read, cases, moves and the git errors.

def test_a_jsx_apostrophe_prints_a_not_read_line(repo):
    body = b'test("jsx", () => {\n  render(<p>Don\'t</p>);\n  expect(1).toBe(1);\n});\n'
    write(repo, "tests/view.test.tsx", body)
    commit(repo, "jsx")
    write(repo, "tests/view.test.tsx", body + b"\n")
    out = _invoke("tests", "loss", "--base", "HEAD").output
    assert "not read tests/view.test.tsx::jsx (body not read)" in out.splitlines()


def test_a_cut_parametrize_list_prints_fewer_cases(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"[1]"))
    report = loss.loss_report(repo)
    assert report.fewer_cases == (("tests/test_calc.py::test_doubles", 2, 1),)
    assert (report.removed, report.fewer_assertions, report.not_read) == ((), (), ())
    assert "fewer cases tests/test_calc.py::test_doubles 2 -> 1" in _invoke("tests", "loss").output.splitlines()


def test_a_list_of_three_cut_to_one_prints_fewer_cases_and_an_edited_value_prints_nothing(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"[1, 2, 3]"))
    commit(repo, "three cases")
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"[1]"))
    assert loss.loss_report(repo, "HEAD").fewer_cases == (("tests/test_calc.py::test_doubles", 3, 1),)
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"[1, 2, 4]"))
    no_loss(loss.loss_report(repo, "HEAD"))


def test_a_computed_case_list_prints_not_read(repo):
    write(repo, "tests/test_calc.py", CALC.replace(b"[1, 2]", b"VALUES"))
    report = loss.loss_report(repo)
    assert report.not_read == (("tests/test_calc.py::test_doubles", "computed cases"),)
    assert report.fewer_cases == ()


def test_a_test_that_leaves_one_file_and_arrives_in_another_prints_a_moved_line(repo):
    write(repo, "tests/a_test.py", b"def test_init():\n    assert 1\n")
    commit(repo, "a")
    (repo / "tests" / "a_test.py").unlink()
    write(repo, "tests/b_test.py", b"def test_init():\n    assert 2\n")
    report = loss.loss_report(repo, "HEAD")
    assert report.moved == (("tests/a_test.py::test_init", "tests/b_test.py::test_init"),)
    assert (report.removed, report.added) == ((), ())
    out = _invoke("tests", "loss", "--base", "HEAD").output
    assert "moved tests/a_test.py::test_init -> tests/b_test.py::test_init" in out.splitlines()


def test_a_renamed_class_prints_moved_lines_and_no_removed_or_added_line(repo):
    write(repo, "tests/test_cls.py", b"class TestOld:\n    def test_one(self):\n        assert 1\n")
    commit(repo, "class")
    write(repo, "tests/test_cls.py", b"class TestNew:\n    def test_one(self):\n        assert 1\n")
    report = loss.loss_report(repo, "HEAD")
    assert report.moved == (("tests/test_cls.py::TestOld::test_one", "tests/test_cls.py::TestNew::test_one"),)
    assert (report.removed, report.added, report.net) == ((), (), 0)


def test_the_real_object_store_does_not_change(repo):
    write(repo, "tests/test_calc.py", CALC + b"\n\ndef test_new():\n    assert 1\n")
    write(repo, "tests/test_other.py", b"def test_x():\n    assert 1\n")
    before = git(repo, "count-objects", "-v")
    loss.loss_report(repo)
    assert git(repo, "count-objects", "-v") == before


def test_a_branch_with_no_commit_says_so(repo):
    git(repo, "checkout", "-q", "--orphan", "fresh")
    out = _invoke("tests", "loss", "--base", "main")
    assert out.exit_code == 1 and out.output.count("\n") == 1
    assert "has no commit" in out.output


def test_a_shallow_clone_with_no_merge_base_names_fetch_unshallow(repo, tmp_path, monkeypatch):
    commit_file = b"def test_more():\n    assert 1\n"
    write(repo, "tests/test_more.py", commit_file)
    commit(repo, "work commit")
    write(repo, "tests/test_more.py", commit_file + commit_file.replace(b"more", b"most"))
    commit(repo, "second work commit, so the fork point is not in a clone of depth 1")
    git(repo, "checkout", "-q", "main")  # the remote HEAD of the clone is main
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-single-branch", repo.as_uri(), str(clone)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    git(clone, "checkout", "-q", "work")
    monkeypatch.chdir(clone)
    out = _invoke("tests", "loss")
    assert out.exit_code == 1 and out.output.count("\n") == 1
    assert "git fetch --unshallow" in out.output


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX modes and a user that is not root")
def test_a_work_tree_that_git_cannot_read_names_the_exit_code_and_the_first_error_line(repo):
    locked = repo / "locked.txt"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        out = _invoke("tests", "loss")
    finally:
        locked.chmod(0o600)
    assert out.exit_code == 1 and out.output.count("\n") == 1
    assert "git add exited with" in out.output and "locked.txt" in out.output


def test_only_a_test_with_two_sides_can_be_not_read(repo):
    computed = b"import pytest\n\n@pytest.mark.parametrize('n', VALUES)\ndef test_values(n):\n    assert n\n"
    write(repo, "tests/test_values.py", computed)
    added = loss.loss_report(repo)
    assert added.added == ("tests/test_values.py::test_values",)
    assert added.not_read == ()
    assert not [line for line in _invoke("tests", "loss").output.splitlines() if line.startswith("not read ")]
    commit(repo, "values")
    write(repo, "tests/test_values.py", computed + b"    assert n > 0\n")
    both = loss.loss_report(repo, "HEAD")
    assert both.not_read == (("tests/test_values.py::test_values", "computed cases"),)
    assert "not read tests/test_values.py::test_values (computed cases)" in _invoke(
        "tests", "loss", "--base", "HEAD").output.splitlines()



def test_rust_attributes_with_brackets_inside_strings_keep_the_test():
    body = ('#[test]\n#[ignore = "see [1]"]\nfn later() {}\n\n'
            '#[test]\n#[should_panic(expected = "v[0]")]\nfn panics() {\n    assert!(false);\n}\n')
    found = by_name("src/lib.rs", body)
    assert (found["later"].skipped, found["panics"].skipped, found["panics"].asserts) == (True, False, 1)


GO_TABLE = """package calc

import "testing"

func TestTable(t *testing.T) {
\tcases := []struct {
\t\tname string
\t\tin   int
\t}{
\t\t{"one", 1},
\t\t{"two", 2},
\t\t{"three", 3},
\t}
\tfor _, tc := range cases {
\t\tt.Run(tc.name, func(t *testing.T) {
\t\t\tif tc.in == 0 {
\t\t\t\tt.Fatal("zero")
\t\t\t}
\t\t})
\t}
}
"""


def test_a_go_table_row_that_is_cut_prints_fewer_cases(repo):
    assert by_name("calc_test.go", GO_TABLE)["TestTable"].cases == 3
    write(repo, "calc/calc_test.go", GO_TABLE.encode())
    commit(repo, "go table")
    write(repo, "calc/calc_test.go", GO_TABLE.replace('\t\t{"three", 3},\n', "").encode())
    report = loss.loss_report(repo, "HEAD")
    assert report.fewer_cases == (("calc/calc_test.go::TestTable", 3, 2),)


def test_a_go_table_built_by_a_call_prints_not_read_on_a_body_change(repo):
    built = GO_TABLE.split("\tcases := ")[0] + "\tcases := makeCases()\n\tfor _, tc := range cases {" + \
        GO_TABLE.split("for _, tc := range cases {")[1]
    assert (by_name("calc_test.go", built)["TestTable"].cases,
            by_name("calc_test.go", built)["TestTable"].unread) == (None, (loss_scan.COMPUTED_CASES,))
    write(repo, "calc/calc_test.go", built.encode())
    commit(repo, "go built table")
    write(repo, "calc/calc_test.go", built.replace('t.Fatal("zero")', 't.Fatalf("zero %d", tc.in)').encode())
    report = loss.loss_report(repo, "HEAD")
    assert report.not_read == (("calc/calc_test.go::TestTable", "computed cases"),)


GO_INLINE = """package calc

import "testing"

func TestInline(t *testing.T) {
\tfor _, s := range []string{"a", "b", "c"} {
\t\tt.Run(s, func(t *testing.T) {
\t\t\tif s == "" {
\t\t\t\tt.Fatal("empty")
\t\t\t}
\t\t})
\t}
}
"""


def test_a_go_inline_table_row_that_is_cut_prints_fewer_cases(repo):
    assert by_name("calc_test.go", GO_INLINE)["TestInline"].cases == 3
    write(repo, "calc/inline_test.go", GO_INLINE.encode())
    commit(repo, "inline table")
    write(repo, "calc/inline_test.go", GO_INLINE.replace('"a", "b", "c"', '"a", "b"').encode())
    assert loss.loss_report(repo, "HEAD").fewer_cases == (("calc/inline_test.go::TestInline", 3, 2),)


def test_a_go_table_that_is_appended_to_gives_no_case_count(repo):
    appended = GO_TABLE.replace("\tfor _, tc := range cases {",
                                '\tcases = append(cases, struct {\n\t\tname string\n\t\tin   int\n\t}{"four", 4})\n'
                                "\tfor _, tc := range cases {")
    found = by_name("calc_test.go", appended)["TestTable"]
    assert (found.cases, found.unread) == (None, (loss_scan.COMPUTED_CASES,))
    write(repo, "calc/calc_test.go", appended.encode())
    commit(repo, "appended table")
    write(repo, "calc/calc_test.go", appended.replace('t.Fatal("zero")', 't.Fatalf("zero %d", tc.in)').encode())
    assert loss.loss_report(repo, "HEAD").not_read == (("calc/calc_test.go::TestTable", "computed cases"),)


def test_rust_skips_a_block_comment_between_the_test_attribute_and_fn():
    body = "#[test]\n/* a note\n   on two lines */\n#[ignore]\nfn later() {\n    assert!(true);\n}\n"
    found = by_name("src/lib.rs", body)
    assert (found["later"].skipped, found["later"].asserts) == (True, 1)


GO_MULTILINE = """package calc

import "testing"

func TestMultiline(t *testing.T) {
\tfor _, s := range []string{
\t\t"a",
\t\t"b",
\t\t"c",
\t} {
\t\tt.Run(s, func(t *testing.T) {
\t\t\tif s == "" {
\t\t\t\tt.Fatal("empty")
\t\t\t}
\t\t})
\t}
}
"""


def test_a_go_multi_line_inline_table_row_that_is_cut_prints_fewer_cases(repo):
    found = by_name("calc_test.go", GO_MULTILINE)["TestMultiline"]
    assert (found.cases, found.unread) == (3, ())
    write(repo, "calc/multi_test.go", GO_MULTILINE.encode())
    commit(repo, "multi-line inline table")
    write(repo, "calc/multi_test.go", GO_MULTILINE.replace('\t\t"c",\n', "").encode())
    assert loss.loss_report(repo, "HEAD").fewer_cases == (("calc/multi_test.go::TestMultiline", 3, 2),)


def test_a_go_for_header_with_no_body_brace_gives_no_case_count():
    body = ('package calc\n\nfunc TestNoBody(t *testing.T) {\n\tfor _, s := range names\n'
            '\t\tt.Run(s, func(t *testing.T) {})\n}\n')
    found = by_name("calc_test.go", body)["TestNoBody"]
    assert (found.cases, found.unread) == (None, (loss_scan.COMPUTED_CASES,))


def test_a_go_loop_over_a_struct_table_in_the_header_counts_its_rows():
    body = ('package calc\n\nfunc TestStruct(t *testing.T) {\n\tfor _, tc := range []struct{ n int }{\n'
            '\t\t{1},\n\t\t{2},\n\t} {\n\t\tt.Run("x", func(t *testing.T) {})\n\t}\n'
            '\tfor i := 0; i < 3; i++ {\n\t\t_ = i\n\t}\n\tt.Run("after", func(t *testing.T) {})\n}\n')
    assert by_name("calc_test.go", body)["TestStruct"].cases == 3


def test_a_go_function_literal_in_a_for_header_is_not_the_loop_body():
    from jacked.testdiet import loss_braces
    body = ('package calc\n\nfunc TestFuncRange(t *testing.T) {\n'
            '\tfor _, n := range func() []int { return []int{1, 2} }() {\n'
            '\t\tt.Run("n", func(t *testing.T) { _ = n })\n\t}\n}\n')
    found = by_name("calc_test.go", body)["TestFuncRange"]
    assert (found.cases, found.unread) == (None, (loss_scan.COMPUTED_CASES,))
    index = loss_braces._Index(body, "\"`", "`", chars=True)
    brace = body.index("{", body.index("TestFuncRange"))
    (loop,) = loss_braces._go_loops(body, index, brace, index.body(brace))
    assert loop[1] == body.index("{\n\t\tt.Run")


# --------------------------------------------------------------------------- regex literals and damaged files

_GLOB_TESTS = (b'import { test, expect } from "vitest";\n'
               b'test("glob", () => {\n  expect(p).toMatch(/^src\\/*.ts$/);\n});\n'
               b'test("trim", () => {\n  expect(s.replace(/\\/*$/, "")).toBe("a");\n});\n'
               b'test("guard", () => {\n  expect(1).toBe(1);\n});\n')


def test_a_regex_literal_with_a_comment_opener_does_not_hide_the_next_tests():
    names = [d.name for d in loss_scan.scan("src/a.test.ts", _GLOB_TESTS.decode())]
    assert names == ["glob", "trim", "guard"]
    assert loss_scan.scan_file("src/a.test.ts", _GLOB_TESTS.decode())[1] is None


def test_a_removed_test_after_a_regex_literal_is_named(repo):
    write(repo, "src/a.test.ts", _GLOB_TESTS)
    commit(repo, "globs")
    write(repo, "src/a.test.ts", _GLOB_TESTS.split(b'test("guard"')[0])
    report = loss.loss_report(repo, "HEAD")
    assert report.removed == ("src/a.test.ts::guard",) and report.files_not_read == ()


def test_jsx_closing_tags_and_self_closing_tags_are_not_regex_literals():
    body = ('test("view", () => {\n  render(<div><Foo x={y} /><br/></div>);\n'
            '  const half = a / b / c;\n  expect(1).toBe(1);\n});\n'
            'test("after", () => {\n  expect(2).toBe(2);\n});\n')
    found, reason = loss_scan.scan_file("src/view.test.tsx", body)
    assert [(d.name, d.asserts, d.unread) for d in found] == [("view", 1, ()), ("after", 1, ())]
    assert reason is None


@pytest.mark.parametrize("path, text", [
    ("src/a.test.ts", 'test("one", () => {});\n/* a comment that does not close\ntest("two", () => {});\n'),
    ("src/a.test.js", 'test("one", () => {});\nconst t = `a template that does not close\n'),
    ("pkg/a_test.go", "func TestOne(t *testing.T) {}\nvar s = `raw text that does not close\n"),
    ("src/lib.rs", "#[test]\nfn one() {}\n/* a comment that does not close\n#[test]\nfn two() {}\n"),
])
def test_a_file_with_a_comment_or_a_string_that_does_not_close_is_named(path, text):
    reason = loss_scan.scan_file(path, text)[1]
    assert reason is not None and "line" in reason and "does not close" in reason


def test_a_damaged_script_file_prints_a_not_read_file_line(repo):
    write(repo, "src/b.test.ts", b'test("one", () => {});\ntest("two", () => {});\n')
    commit(repo, "two")
    write(repo, "src/b.test.ts", b'test("one", () => {});\n/* open\ntest("two", () => {});\n')
    out = _invoke("tests", "loss", "--base", "HEAD").output.splitlines()
    assert any(line.startswith("not read file src/b.test.ts (") for line in out), out


def test_loss_does_not_run_a_filter_driver_of_the_repository(repo, tmp_path):
    marker = tmp_path / "filter-ran"
    script = tmp_path / "clean.sh"
    script.write_bytes(f"#!/bin/sh\ntouch '{marker}'\ncat\n".encode())
    script.chmod(0o755)
    if os.name == "nt":
        pytest.skip("a shell filter needs POSIX")
    git(repo, "config", "filter.x.clean", str(script))
    write(repo, ".gitattributes", b"*.py filter=x\n")
    write(repo, "tests/test_calc.py", CALC + b"\ndef test_more():\n    assert True\n")
    if marker.exists():
        marker.unlink()
    report = loss.loss_report(repo)
    assert "tests/test_calc.py::test_more" in report.added
    assert not marker.exists()
