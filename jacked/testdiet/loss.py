"""The loss report: the test declarations that a change removes or weakens.

``loss_report`` reads git only. It needs no history and no test run. It
compares each touched test file of the base commit with the work tree, so a
change that is not committed counts, and an untracked file counts too.
``jacked.testdiet.loss_scan`` reads the declarations of one file, and says
what it counts as an assertion, a case and a skip mark.

Each test of the work tree is paired with a test of the base: first the
same file and name, then the same name in a different touched file, then
the same last name part (``Class::name`` gives ``name``) with the same body
(``Declaration.body``). A pair that is not the same file and name prints a
``moved`` line. So a renamed file, a moved test, a renamed class and an
edited parameter value give no loss. A test that gets a new name inside one
file shows as one removed and one added name. A Python file is read only
when pytest collects it by default (``test_*.py``, ``*_test.py``).

``fewer_assertions``, ``fewer_cases`` and ``skips_added`` are report lines,
not a verdict. ``not_read`` names each paired test (a test on both sides,
or a moved pair) that the report cannot compare: a body that the scanner
cannot read, or a computed case list. An added or a removed test has
nothing to compare, so it is never in ``not_read``. ``files_not_read``
names each touched file that is not read: a file that does not parse, a
symbolic link, a submodule and a type change.
A symbolic link, a submodule and a type change are never read as absent, so
their tests are never reported as removed.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from jacked import git_guard
from jacked.findbin import find_bin
from jacked.testdiet import tree
from jacked.testdiet.cleanup import remove_tree_quietly
from jacked.testdiet.loss_scan import NOT_PARSED, Declaration, declarations, scan, scan_file, scan_kind
from jacked.testdiet.model import DEFAULT_BRANCH_REF, clean_text, one_line

__all__ = ["DEFAULT_NAME", "DEFAULT_REF", "Declaration", "LossError", "LossReport",
           "declarations", "loss_report", "render", "scan", "scan_kind"]

#: The ref of the default branch. There is no fetch and no network call.
DEFAULT_REF = DEFAULT_BRANCH_REF
#: The name of the default base in the output.
DEFAULT_NAME = "origin/HEAD"
_ZERO = re.compile(r"0+")
_FILE_KINDS = {"120000": "symbolic link", "160000": "submodule"}

Sides = dict[str, list[Declaration]]


class LossError(RuntimeError):
    """An input of the loss report cannot be read (a ref, a tree or a file)."""


@dataclass(frozen=True)
class LossReport:
    """The loss report. Each name has the form ``path::name``.

    ``base`` is the base commit: the merge base of HEAD and ``base_ref``.
    ``base_ref`` is the ref that the user gave, or ``origin/HEAD``. ``net``
    is the number of declarations now, minus the number at the base, over
    the touched files. ``moved`` holds (old name, new name) pairs.
    ``fewer_cases`` holds (name, cases at the base, cases now).
    ``not_read`` holds (name, reason) for each paired test that cannot be
    compared. ``files_not_read`` holds (path, kind) for each touched file
    that is not read: a symbolic link, a submodule, a type change, or a
    file that does not parse.
    """

    base: str
    base_ref: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    net: int
    fewer_assertions: tuple[str, ...]
    skips_added: tuple[str, ...]
    moved: tuple[tuple[str, str], ...] = ()
    fewer_cases: tuple[tuple[str, int, int], ...] = ()
    not_read: tuple[tuple[str, str], ...] = ()
    files_not_read: tuple[tuple[str, str], ...] = ()


# Git --------------------------------------------------------------------

def _base_commit(top: Path, base: str | None) -> str:
    """Return the merge base of HEAD and ``base`` (the default branch when ``base`` is None).

    A ref that is an ancestor of HEAD is its own merge base. A branch that
    moved ahead after the fork gives the fork point, so its new tests are
    not read as removed.
    """
    if tree.rev(top, "HEAD^{commit}") is None:
        raise LossError("the current branch has no commit. Make a commit, then run loss again.")
    if base is not None:
        tip = tree.rev(top, f"{base}^{{commit}}")
        if tip is None:
            raise LossError(f"the base {base!r} is not a commit of this repository")
    else:
        tip = tree.rev(top, f"{DEFAULT_REF}^{{commit}}")
        if tip is None:
            raise LossError(f"{DEFAULT_REF} does not exist. Give --base <branch>, "
                            "or run: git remote set-head origin --auto")
    code, out = tree.git(top, "merge-base", "--end-of-options", "HEAD", tip)
    lines = tree.output_lines(out)
    if code != 0 or len(lines) != 1:
        name = DEFAULT_NAME if base is None else repr(base)
        _, shallow = tree.git(top, "rev-parse", "--is-shallow-repository")
        if shallow.strip() == b"true":
            raise LossError(f"HEAD and {name} have no merge base in this shallow clone. "
                            "Run: git fetch --unshallow")
        raise LossError(f"HEAD and {name} have no merge base. Give a --base that shares history with HEAD.")
    return lines[0]


def _touched(top: Path, base: str, head_tree: str, env: dict[str, str]):
    """Return the touched test files as (path, base blob, head blob), and the files that are not read.

    An absent side has the blob ``""``. A symbolic link, a submodule and a
    type change go to the second list with their kind.
    """
    code, out = tree.git(top, "diff-tree", "-r", "-z", "--no-renames", "--end-of-options",
                         base, head_tree, env=env)
    if code != 0:
        raise LossError(f"git diff-tree exited with {code}")
    fields = out.split(b"\0")
    touched, skipped = [], []
    for i in range(0, len(fields) - 1, 2):
        meta = os.fsdecode(fields[i]).lstrip(":").split()
        path = os.fsdecode(fields[i + 1])
        if len(meta) < 4 or not scan_kind(path):
            continue
        sides = [(meta[0], meta[2]), (meta[1], meta[3])]
        present = [mode for mode, sha in sides if not _ZERO.fullmatch(sha)]
        kinds = {_FILE_KINDS.get(mode, "regular file") for mode in present}
        if kinds - {"regular file"}:
            kind = "type change" if len(kinds) > 1 else kinds.pop()
            skipped.append((path, kind))
            continue
        touched.append((path, *("" if _ZERO.fullmatch(sha) else sha for _, sha in sides)))
    return touched, skipped


def _read_blobs(top: Path, blobs: list[str], env: dict[str, str]) -> dict[str, memoryview]:
    """Read each blob with one ``git cat-file --batch`` process. An empty name is an absent side.

    Each value is a ``memoryview`` slice of the one output buffer, so the
    blobs are in memory one time, not two. There is no size limit.
    """
    wanted = sorted({blob for blob in blobs if blob})
    if not wanted:
        return {}
    with tempfile.TemporaryFile() as names:
        names.write("".join(f"{blob}\n" for blob in wanted).encode("ascii"))
        names.seek(0)
        code, out = tree.git(top, "cat-file", "--batch", env=env, stdin=names)
    if code != 0:
        raise LossError(f"git cat-file --batch exited with {code}")
    found: dict[str, memoryview] = {}
    view = memoryview(out)
    pos = 0
    for blob in wanted:
        newline = out.find(b"\n", pos)
        header = out[pos:newline].split() if newline >= 0 else []
        if len(header) != 3 or header[0] != blob.encode("ascii") or header[1] != b"blob":
            raise LossError(f"git cannot read the object {blob}")
        start = newline + 1
        found[blob] = view[start:start + int(header[2])]
        pos = start + int(header[2]) + 1  # the content ends with one newline
    return found


def loss_report(cwd: Path, base: str | None = None) -> LossReport:
    """Compare the test declarations of the touched files at ``base`` and in the work tree.

    ``base`` is a ref or a commit. The compared commit is the merge base of
    HEAD and ``base``. With no ``base``, it is the merge base of HEAD and the
    default branch (``refs/remotes/origin/HEAD``). Outside a work tree this
    raises ``tree.NotAWorkTree``, and git errors raise the other
    ``tree.TreeError`` classes. An input that cannot be read raises
    ``LossError``.
    """
    top = tree.work_tree_top(cwd)
    commit = _base_commit(top, base)
    guarded = git_guard.guarded_git_env(find_bin("git") or "git", str(top))
    if guarded is None:
        raise LossError("git cannot list the filter drivers of this repository, so loss does not "
                        "read the work tree")
    objects = Path(tempfile.mkdtemp(prefix="jacked-loss-"))
    try:
        try:
            # A read-only report: no filter driver of the repository config runs.
            head = tree.read_tree_id(top, objects=objects, base_env=guarded)
        except tree.TreeError as exc:
            detail = getattr(exc, "detail", "")
            raise LossError(f"git cannot give the tree of the work tree ({exc}"
                            + (f": {one_line(detail[:tree.DETAIL_CHARS])})" if detail else ")")) from exc
        if head is None:
            raise LossError("git cannot give the tree of the work tree")
        env = tree.object_env(top, objects)
        sides: tuple[Sides, Sides] = ({}, {})
        touched, skipped = _touched(top, commit, head.tree, env)
        contents = _read_blobs(top, [blob for _, *blobs in touched for blob in blobs], env)
        for path, *blobs in touched:
            for side, blob in zip(sides, blobs):
                data = contents.get(blob) if blob else None
                if data is not None:
                    side[path], reason = scan_file(path, str(data, "utf-8", "replace"))
                    if reason is not None and (path, reason) not in skipped:
                        skipped.append((path, reason))
    finally:
        remove_tree_quietly(objects)
    return _compare(commit, *sides, base_ref=DEFAULT_NAME if base is None else base, files_not_read=skipped)


# The comparison ---------------------------------------------------------

def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _match(before: Sides, after: Sides):
    """Pair the tests of the work tree with the tests of the base.

    The keys, in order: the same file and name, the same name, the same last
    name part with the same body. A test whose body is not read never pairs
    by the last key, so a removal and an unrelated addition with one last
    name never read as a move. Return the pairs (old path, old test, path,
    test), the removed tests and the added tests, each as (path, test).
    """
    pool = [(path, d) for path in sorted(before) for d in before[path]]
    used = [False] * len(pool)
    heads = [(path, d) for path in sorted(after) for d in after[path]]
    pairs, left = [], list(range(len(heads)))
    for key in (lambda p, d: (p, d.name), lambda p, d: d.name,
                lambda p, d: None if d.body is None else (_leaf(d.name), d.body)):
        free: dict[object, list[int]] = defaultdict(list)
        for k, (path, d) in enumerate(pool):
            if not used[k] and key(path, d) is not None:
                free[key(path, d)].append(k)
        rest = []
        for h in left:
            path, d = heads[h]
            spare = free.get(key(path, d)) if key(path, d) is not None else None
            if spare:
                k = spare.pop(0)
                used[k] = True
                pairs.append((pool[k][0], pool[k][1], path, d))
            else:
                rest.append(h)
        left = rest
    removed = [pool[k] for k in range(len(pool)) if not used[k]]
    return pairs, removed, [heads[h] for h in left]


def _compare(base: str, before: Sides, after: Sides, *, base_ref: str = DEFAULT_NAME,
             files_not_read=()) -> LossReport:
    pairs, removed, added = _match(before, after)
    weaker, fewer, skips, moved = [], [], [], []
    unread = set()
    for old_path, then, path, now in pairs:
        name = f"{path}::{now.name}"
        if (old_path, then.name) != (path, now.name):
            moved.append((f"{old_path}::{then.name}", name))
        if now.asserts is not None and then.asserts is not None and now.asserts < then.asserts:
            weaker.append(name)
        if now.cases is not None and then.cases is not None and now.cases < then.cases:
            fewer.append((name, then.cases, now.cases))
        if now.skipped is True and then.skipped is False:
            skips.append(name)
        unread.update((name, reason) for reason in then.unread + now.unread if reason != NOT_PARSED)
    net = sum(len(found) for found in after.values()) - sum(len(found) for found in before.values())
    return LossReport(base=base, base_ref=base_ref, added=tuple(sorted(f"{p}::{d.name}" for p, d in added)),
                      removed=tuple(sorted(f"{p}::{d.name}" for p, d in removed)), net=net,
                      fewer_assertions=tuple(sorted(weaker)), skips_added=tuple(sorted(skips)),
                      moved=tuple(sorted(moved)), fewer_cases=tuple(sorted(fewer)),
                      not_read=tuple(sorted(unread)), files_not_read=tuple(sorted(set(files_not_read))))


def render(report: LossReport) -> str:
    """Return the report as text lines. Each name is on one line, with no control character."""
    lines = [f"base {report.base[:12]} ({one_line(report.base_ref)})", f"net {report.net:+d}" if report.net else "net 0",
             f"added {len(report.added)}, removed {len(report.removed)}"]
    lines += [f"removed {one_line(name)}" for name in report.removed]
    lines += [f"added {one_line(name)}" for name in report.added]
    lines += [f"moved {one_line(old)} -> {one_line(new)}" for old, new in report.moved]
    lines += [f"fewer assertions {one_line(name)}" for name in report.fewer_assertions]
    lines += [f"fewer cases {one_line(name)} {then} -> {now}" for name, then, now in report.fewer_cases]
    lines += [f"skip mark added {one_line(name)}" for name in report.skips_added]
    lines += [f"not read {one_line(name)} ({reason})" for name, reason in report.not_read]
    lines += [f"not read file {one_line(path)} ({kind})" for path, kind in report.files_not_read]
    return clean_text("\n".join(lines))
