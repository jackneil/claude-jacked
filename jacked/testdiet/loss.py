"""The loss report: the test declarations that a change removes or weakens.

``loss_report`` reads git only. It needs no history and no test run. It
compares each touched test file of the base commit with the work tree, so a
change that is not committed counts, and an untracked file counts too.
``jacked.testdiet.loss_scan`` reads the declarations of one file, and says
what it counts as an assertion, a case and a skip mark.

Each test of the work tree is paired with a test of the base: first the
same file and name, then the same name in a different touched file, then
the same last name part (``Class::name`` gives ``name``). A pair that is not
the same file and name prints a ``moved`` line. So a renamed file, a moved
test, a renamed class and an edited parameter value give no loss. A test
that gets a new name inside one file shows as one removed and one added name.

``fewer_assertions``, ``fewer_cases`` and ``skips_added`` are report lines,
not a verdict. ``not_read`` names each paired test (a test on both sides,
or a moved pair) that the report cannot compare: a file that does not
parse, a body that the scanner cannot read, a computed case list. An added
or a removed test has nothing to compare, so it is never in ``not_read``.
It also names each file that is a symbolic link, a submodule or a type change.
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

from jacked.testdiet import tree
from jacked.testdiet.loss_scan import Declaration, declarations, scan, scan_kind
from jacked.testdiet.model import one_line

__all__ = ["DEFAULT_NAME", "DEFAULT_REF", "Declaration", "LossError", "LossReport", "NotInWorkTree",
           "declarations", "loss_report", "render", "scan", "scan_kind"]

#: The ref of the default branch. There is no fetch and no network call.
DEFAULT_REF = "refs/remotes/origin/HEAD"
#: The name of the default base in the output.
DEFAULT_NAME = "origin/HEAD"
_ZERO = re.compile(r"0+")
_FILE_KINDS = {"120000": "symbolic link", "160000": "submodule"}

Sides = dict[str, list[Declaration]]


class LossError(RuntimeError):
    """An input of the loss report cannot be read (a ref, a tree or a file)."""


class NotInWorkTree(LossError):
    """The folder is not in the work tree of a git repository."""


@dataclass(frozen=True)
class LossReport:
    """The loss report. Each name has the form ``path::name``.

    ``base`` is the base commit: the merge base of HEAD and ``base_ref``.
    ``base_ref`` is the ref that the user gave, or ``origin/HEAD``. ``net``
    is the number of declarations now, minus the number at the base, over
    the touched files. ``moved`` holds (old name, new name) pairs.
    ``fewer_cases`` holds (name, cases at the base, cases now).
    ``not_read`` holds (name or file, reason).
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


# Git --------------------------------------------------------------------

def _rev(top: Path, spec: str) -> str | None:
    code, out = tree.git(top, "rev-parse", "--verify", "-q", "--end-of-options", spec)
    lines = tree.output_lines(out)
    return lines[0] if code == 0 and len(lines) == 1 else None


def _top(cwd: Path) -> Path:
    code, out = tree.git(Path(cwd), "rev-parse", "--show-toplevel")
    lines = tree.output_lines(out)
    if code != 0 or len(lines) != 1:
        raise NotInWorkTree("this folder is not in the work tree of a git repository.")
    return Path(lines[0])


def _base_commit(top: Path, base: str | None) -> str:
    """Return the merge base of HEAD and ``base`` (the default branch when ``base`` is None).

    A ref that is an ancestor of HEAD is its own merge base. A branch that
    moved ahead after the fork gives the fork point, so its new tests are
    not read as removed.
    """
    if _rev(top, "HEAD^{commit}") is None:
        raise LossError("the current branch has no commit. Make a commit, then run loss again.")
    if base is not None:
        tip = _rev(top, f"{base}^{{commit}}")
        if tip is None:
            raise LossError(f"the base {base!r} is not a commit of this repository")
    else:
        tip = _rev(top, f"{DEFAULT_REF}^{{commit}}")
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
        sides = [(mode, sha) for mode, sha in ((meta[0], meta[2]), (meta[1], meta[3]))]
        present = [mode for mode, sha in sides if not _ZERO.fullmatch(sha)]
        kinds = {_FILE_KINDS.get(mode, "regular file") for mode in present}
        if kinds - {"regular file"}:
            kind = "type change" if len(kinds) > 1 else kinds.pop()
            skipped.append((path, kind))
            continue
        touched.append((path, *("" if _ZERO.fullmatch(sha) else sha for _, sha in sides)))
    return touched, skipped


def _read(top: Path, blob: str, env: dict[str, str]) -> bytes | None:
    if not blob:
        return None
    code, out = tree.git(top, "cat-file", "blob", blob, env=env)
    if code != 0:
        raise LossError(f"git cannot read the object {blob}")
    return out


def loss_report(cwd: Path, base: str | None = None) -> LossReport:
    """Compare the test declarations of the touched files at ``base`` and in the work tree.

    ``base`` is a ref or a commit. The compared commit is the merge base of
    HEAD and ``base``. With no ``base``, it is the merge base of HEAD and the
    default branch (``refs/remotes/origin/HEAD``). Outside a work tree this
    raises ``NotInWorkTree``. An input that cannot be read raises
    ``LossError``. A git that cannot start raises ``tree.TreeError``.
    """
    top = _top(cwd)
    commit = _base_commit(top, base)
    objects = Path(tempfile.mkdtemp(prefix="jacked-loss-"))
    try:
        try:
            head = tree.read_tree_id(top, objects=objects)
        except tree.TreeError as exc:
            detail = getattr(exc, "detail", "")
            raise LossError(f"git cannot give the tree of the work tree ({exc}"
                            + (f": {one_line(detail)})" if detail else ")")) from exc
        if head is None:
            raise LossError("git cannot give the tree of the work tree")
        env = tree.object_env(top, objects)
        sides: tuple[Sides, Sides] = ({}, {})
        touched, skipped = _touched(top, commit, head.tree, env)
        for path, *blobs in touched:
            for side, blob in zip(sides, blobs):
                data = _read(top, blob, env)
                if data is not None:
                    side[path] = scan(path, data.decode("utf-8", "replace"))
    finally:
        tree.remove_tree(objects)
    return _compare(commit, *sides, base_ref=DEFAULT_NAME if base is None else base, files_not_read=skipped)


# The comparison ---------------------------------------------------------

def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def _match(before: Sides, after: Sides):
    """Pair the tests of the work tree with the tests of the base.

    The keys, in order: the same file and name, the same name, the same last
    name part. Return the pairs (old path, old test, path, test), the
    removed tests and the added tests, each as (path, test).
    """
    pool = [(path, d) for path in sorted(before) for d in before[path]]
    used = [False] * len(pool)
    heads = [(path, d) for path in sorted(after) for d in after[path]]
    pairs, left = [], list(range(len(heads)))
    for key in (lambda p, d: (p, d.name), lambda p, d: d.name, lambda p, d: _leaf(d.name)):
        free: dict[object, list[int]] = defaultdict(list)
        for k, (path, d) in enumerate(pool):
            if not used[k]:
                free[key(path, d)].append(k)
        rest = []
        for h in left:
            path, d = heads[h]
            spare = free.get(key(path, d))
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
    unread = set(files_not_read)
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
        unread.update((name, reason) for reason in then.unread + now.unread)
    net = sum(len(found) for found in after.values()) - sum(len(found) for found in before.values())
    return LossReport(base=base, base_ref=base_ref, added=tuple(sorted(f"{p}::{d.name}" for p, d in added)),
                      removed=tuple(sorted(f"{p}::{d.name}" for p, d in removed)), net=net,
                      fewer_assertions=tuple(sorted(weaker)), skips_added=tuple(sorted(skips)),
                      moved=tuple(sorted(moved)), fewer_cases=tuple(sorted(fewer)),
                      not_read=tuple(sorted(unread)))


def render(report: LossReport) -> str:
    """Return the report as text lines. Each name is on one line, with no control character."""
    from jacked.testdiet.record import clean_text

    lines = [f"base {report.base[:12]} ({one_line(report.base_ref)})", f"net {report.net:+d}" if report.net else "net 0",
             f"added {len(report.added)}, removed {len(report.removed)}"]
    lines += [f"removed {one_line(name)}" for name in report.removed]
    lines += [f"added {one_line(name)}" for name in report.added]
    lines += [f"moved {one_line(old)} -> {one_line(new)}" for old, new in report.moved]
    lines += [f"fewer assertions {one_line(name)}" for name in report.fewer_assertions]
    lines += [f"fewer cases {one_line(name)} {then} -> {now}" for name, then, now in report.fewer_cases]
    lines += [f"skip mark added {one_line(name)}" for name in report.skips_added]
    lines += [f"not read {one_line(name)} ({reason})" for name, reason in report.not_read]
    return clean_text("\n".join(lines))
