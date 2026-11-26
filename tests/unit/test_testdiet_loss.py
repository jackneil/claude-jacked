"""Tests for jacked.testdiet.loss and the command ``jacked tests loss``.

Each repository is a throwaway one under ``tmp_path`` with a hermetic git
config. The base is the commit that ``refs/remotes/origin/HEAD`` names, as
the default base of the command, or a ``--base`` value.
"""

from __future__ import annotations

import json
import shutil
import warnings
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked.cli import main
from jacked.testdiet import loss
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
    no_loss(loss.loss_report(repo))


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
    assert _invoke("tests", "loss", "--base", "main").exit_code == 1


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


def test_a_symbolic_link_is_not_read(repo):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("no symbolic links")
    try:
        (repo / "tests" / "test_link.py").symlink_to("test_calc.py")
    except OSError:
        pytest.skip("this system cannot make a symbolic link")
    no_loss(loss.loss_report(repo))


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
