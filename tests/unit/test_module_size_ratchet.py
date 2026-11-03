"""Module size ratchet: no module in ``jacked/`` grows past its line budget.

JACKED_GUARDRAILS.md sets a hard maximum of 500 lines for each file. That
rule was only advice: 39 modules were over it when this ratchet was added,
and the same change split one of them (#227i). This test stops the growth
while the splits land. BASELINE holds the line count of each module that is
over the limit. The test fails in four conditions:

- A module is over the limit and is not in BASELINE.
- A module is larger than its BASELINE entry.
- A BASELINE entry is larger than the real size of its module.
- A BASELINE entry names a file that does not exist.

The count is the number of newline characters, which is the count that
``wc -l`` gives.

To lower an entry after a split, run this test. It names each entry that is
above the real size and gives the new count. Set the entry to that count.
When the module is at or under 500 lines, remove the entry. Do not raise an
entry: split the module. Do not add a module only to make this test pass.
If a split cannot happen now, add the entry with a comment that gives the
reason and the issue that tracks the split.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "jacked"
LIMIT = 500

# The line count of each module over LIMIT, sorted by path. Lower an entry
# after each split, and remove it when the module is at or under LIMIT.
BASELINE: dict[str, int] = {
    "jacked/api/authority_guard.py": 1039,
    "jacked/api/claude_config_store.py": 554,
    "jacked/api/claude_identity.py": 601,
    "jacked/api/credential_helpers.py": 1370,
    "jacked/api/identity_watcher.py": 819,
    "jacked/api/main.py": 797,
    "jacked/api/routes/auth.py": 2421,
    "jacked/api/routes/features.py": 1466,
    "jacked/api/routes/system.py": 1285,
    "jacked/api/usage_monitor.py": 3441,
    "jacked/cli.py": 9333,
    "jacked/codex/installer.py": 570,
    "jacked/codex/usage.py": 516,
    "jacked/credentials/macos_store.py": 509,
    "jacked/credentials/runtime.py": 567,
    "jacked/credentials/transaction.py": 924,
    "jacked/dcr_settings.py": 755,
    "jacked/install_manifest.py": 602,
    "jacked/launch.py": 1586,
    "jacked/memory/capture.py": 786,
    "jacked/memory/migrate.py": 657,
    "jacked/memory/recall.py": 603,
    "jacked/memory/vault.py": 1266,
    "jacked/packs.py": 809,
    "jacked/recover.py": 622,
    "jacked/service/menubar_mac.py": 846,
    "jacked/service/platform.py": 525,
    "jacked/service/spec.py": 585,
    "jacked/service/tray.py": 2016,
    "jacked/service/update_status.py": 1034,
    "jacked/service/updater.py": 2200,
    "jacked/skill_listing.py": 1744,
    "jacked/transcript.py": 821,
    "jacked/web/analytics_db.py": 625,
    "jacked/web/auth.py": 3173,
    "jacked/web/browser_launch.py": 585,
    "jacked/web/database.py": 4105,
    "jacked/web/oauth.py": 1239,
}


def line_count(path: Path) -> int:
    """The number of newline bytes in *path*: the count that ``wc -l`` gives."""
    return path.read_bytes().count(b"\n")


def module_sizes(package: Path) -> dict[str, int]:
    """The line count of each ``*.py`` file under *package*.

    Each key is the path from the parent of *package*, with forward slashes
    on every platform (for example ``jacked/cli.py``).
    """
    return {
        path.relative_to(package.parent).as_posix(): line_count(path)
        for path in sorted(package.rglob("*.py"))
        if path.is_file()
    }


def ratchet_problems(
    sizes: dict[str, int], baseline: dict[str, int], limit: int = LIMIT
) -> list[str]:
    """Each way that the module *sizes* break the ratchet that *baseline* sets."""
    problems = []
    for path, lines in sorted(sizes.items()):
        if lines > limit and path not in baseline:
            problems.append(
                f"a new module crossed {limit} lines: split it, or add it with a reason "
                f"({path} has {lines} lines)"
            )
    for path, allowed in sorted(baseline.items()):
        lines = sizes.get(path)
        if lines is None:
            problems.append(
                f"{path} is in the baseline but does not exist; remove its baseline entry"
            )
        elif lines > limit and lines > allowed:
            problems.append(
                f"{path} grew from {allowed} to {lines} lines; "
                "the guardrail is a ratchet: split it instead"
            )
        elif lines <= limit:
            problems.append(
                f"{path} is now {lines} lines, at or under the {limit}-line limit; "
                "remove its baseline entry"
            )
        elif lines < allowed:
            problems.append(f"{path} is now {lines} lines; lower its baseline entry to {lines}")
    return problems


def test_every_module_fits_the_ratchet():
    problems = ratchet_problems(module_sizes(PACKAGE), BASELINE)
    assert not problems, "\n".join(problems)


def test_the_baseline_is_sorted_by_path():
    assert list(BASELINE) == sorted(BASELINE)


@pytest.mark.parametrize(
    ("sizes", "baseline", "problem"),
    [
        pytest.param(
            {"jacked/new.py": 501},
            {},
            "a new module crossed 500 lines: split it, or add it with a reason "
            "(jacked/new.py has 501 lines)",
            id="crossed",
        ),
        pytest.param(
            {"jacked/big.py": 601},
            {"jacked/big.py": 600},
            "jacked/big.py grew from 600 to 601 lines; "
            "the guardrail is a ratchet: split it instead",
            id="grew",
        ),
        pytest.param(
            {"jacked/big.py": 599},
            {"jacked/big.py": 600},
            "jacked/big.py is now 599 lines; lower its baseline entry to 599",
            id="lower",
        ),
        pytest.param(
            {"jacked/big.py": 500},
            {"jacked/big.py": 600},
            "jacked/big.py is now 500 lines, at or under the 500-line limit; "
            "remove its baseline entry",
            id="under-the-limit",
        ),
        pytest.param(
            {},
            {"jacked/gone.py": 600},
            "jacked/gone.py is in the baseline but does not exist; remove its baseline entry",
            id="missing",
        ),
    ],
)
def test_the_ratchet_names_each_kind_of_problem(sizes, baseline, problem):
    assert ratchet_problems(sizes, baseline) == [problem]


def test_the_ratchet_accepts_a_module_at_the_limit_and_one_at_its_entry():
    sizes = {"jacked/edge.py": 500, "jacked/big.py": 600}
    assert ratchet_problems(sizes, {"jacked/big.py": 600}) == []


def test_the_line_count_is_the_newline_count_that_wc_gives(tmp_path):
    # A last line with no newline does not count, and a CRLF checkout
    # (Windows) gives the same count as LF.
    cases = [(b"", 0), (b"one\n", 1), (b"one\ntwo", 1), (b"one\r\ntwo\r\n", 2)]
    for index, (data, expected) in enumerate(cases):
        path = tmp_path / f"case{index}.py"
        path.write_bytes(data)
        assert line_count(path) == expected, data


def test_module_sizes_keys_each_python_file_by_its_forward_slash_path(tmp_path):
    package = tmp_path / "jacked"
    (package / "sub").mkdir(parents=True)
    (package / "a.py").write_bytes(b"x\n" * 3)
    (package / "sub" / "b.py").write_bytes(b"y\n")
    (package / "notes.txt").write_bytes(b"z\n" * 900)
    assert module_sizes(package) == {"jacked/a.py": 3, "jacked/sub/b.py": 1}
