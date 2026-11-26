"""The loss report: the test declarations that a change removes or weakens.

``loss_report`` reads git only. It needs no history and no test run. It
compares each touched test file of the base commit with the work tree, so a
change that is not committed counts, and an untracked file counts too.
``jacked.testdiet.loss_scan`` reads the declarations of one file.

The names of all touched files are compared as one multiset. So a renamed
file, a test that moves to a different touched file and an edited parameter
value give no loss. A test that gets a new name inside one file shows as one
removed and one added name, with net 0.

``fewer_assertions`` and ``skips_added`` are report lines, not a verdict. A
count that the source cannot give is None, and then there is no comparison.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from jacked.testdiet import tree
from jacked.testdiet.loss_scan import Declaration, declarations, scan, scan_kind
from jacked.testdiet.model import one_line

__all__ = ["DEFAULT_NAME", "DEFAULT_REF", "Declaration", "LossError", "LossReport", "declarations",
           "loss_report", "render", "scan", "scan_kind"]

#: The ref of the default branch. There is no fetch and no network call.
DEFAULT_REF = "refs/remotes/origin/HEAD"
#: The name of the default base in the output.
DEFAULT_NAME = "origin/HEAD"
_ZERO = re.compile(r"0+")
_NOT_READ_MODES = ("160000", "120000")  # a submodule and a symbolic link


class LossError(RuntimeError):
    """An input of the loss report cannot be read (a ref, a tree or a file)."""


@dataclass(frozen=True)
class LossReport:
    """The loss report. Each name has the form ``path::name``.

    ``base`` is the base commit: the merge base of HEAD and ``base_ref``.
    ``base_ref`` is the ref that the user gave, or ``origin/HEAD``. ``net``
    is the number of declarations now,
    minus the number at the base, over the touched files.
    """

    base: str
    base_ref: str
    added: tuple[str, ...]
    removed: tuple[str, ...]
    net: int
    fewer_assertions: tuple[str, ...]
    skips_added: tuple[str, ...]



# Git --------------------------------------------------------------------

def _rev(top: Path, spec: str) -> str | None:
    code, out = tree._git(top, "rev-parse", "--verify", "-q", "--end-of-options", spec)
    lines = tree._lines(out)
    return lines[0] if code == 0 and len(lines) == 1 else None


def _base_commit(top: Path, base: str | None) -> str:
    """Return the merge base of HEAD and ``base`` (the default branch when ``base`` is None).

    A ref that is an ancestor of HEAD is its own merge base. A branch that
    moved ahead after the fork gives the fork point, so its new tests are
    not read as removed.
    """
    if base is not None:
        tip = _rev(top, f"{base}^{{commit}}")
        if tip is None:
            raise LossError(f"the base {base!r} is not a commit of this repository")
    else:
        tip = _rev(top, f"{DEFAULT_REF}^{{commit}}")
        if tip is None:
            raise LossError(f"{DEFAULT_REF} does not exist. Give --base <branch>, "
                            "or run: git remote set-head origin --auto")
    code, out = tree._git(top, "merge-base", "--end-of-options", "HEAD", tip)
    lines = tree._lines(out)
    if code != 0 or len(lines) != 1:
        name = DEFAULT_NAME if base is None else repr(base)
        raise LossError(f"HEAD and {name} have no merge base. Give a --base that shares history with HEAD.")
    return lines[0]


def _touched(top: Path, base: str, head_tree: str, env: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return (path, base blob, head blob) for each touched test file. An empty blob is ``""``."""
    code, out = tree._git(top, "diff-tree", "-r", "-z", "--no-renames", "--end-of-options",
                          base, head_tree, env=env)
    if code != 0:
        raise LossError(f"git diff-tree exited with {code}")
    fields = out.split(b"\0")
    touched = []
    for i in range(0, len(fields) - 1, 2):
        meta = os.fsdecode(fields[i]).lstrip(":").split()
        path = os.fsdecode(fields[i + 1])
        if len(meta) < 4 or not scan_kind(path):
            continue
        blobs = [sha if mode not in _NOT_READ_MODES and not _ZERO.fullmatch(sha) else ""
                 for mode, sha in ((meta[0], meta[2]), (meta[1], meta[3]))]
        touched.append((path, blobs[0], blobs[1]))
    return touched



def _read(top: Path, blob: str, env: dict[str, str]) -> bytes | None:
    if not blob:
        return None
    code, out = tree._git(top, "cat-file", "blob", blob, env=env)
    if code != 0:
        raise LossError(f"git cannot read the object {blob}")
    return out


def loss_report(cwd: Path, base: str | None = None) -> LossReport:
    """Compare the test declarations of the touched files at ``base`` and in the work tree.

    ``base`` is a ref or a commit. The compared commit is the merge base of
    HEAD and ``base``. With no ``base``, it is the merge base of HEAD and the
    default branch (``refs/remotes/origin/HEAD``). An
    input that cannot be read raises ``LossError`` (or ``tree.TreeError``
    outside a git work tree).
    """
    from jacked.testdiet.record import work_tree_top

    top = work_tree_top(Path(cwd))
    commit = _base_commit(top, base)
    objects = Path(tempfile.mkdtemp(prefix="jacked-loss-"))
    try:
        head = tree.tree_id(top, objects=objects)
        if head is None:
            raise LossError("git cannot give the tree of the work tree")
        code, out = tree._git(top, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        if code != 0:
            raise LossError("git did not give the object path")
        env = {"GIT_OBJECT_DIRECTORY": str(objects),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": tree._alternates(tree._lines(out)[0])}
        sides: tuple[dict[str, list[Declaration]], dict[str, list[Declaration]]] = ({}, {})
        for path, *blobs in _touched(top, commit, head.tree, env):
            for side, blob in zip(sides, blobs):
                data = _read(top, blob, env)
                if data is not None:
                    side[path] = scan(path, data.decode("utf-8", "replace"))
    finally:
        tree.remove_tree(objects)
    return _compare(commit, *sides, base_ref=DEFAULT_NAME if base is None else base)


def _compare(base: str, before: dict[str, list[Declaration]],
             after: dict[str, list[Declaration]], *, base_ref: str = DEFAULT_NAME) -> LossReport:
    old = Counter(d.name for found in before.values() for d in found)
    new = Counter(d.name for found in after.values() for d in found)
    removed = _surplus(before, after, old - new)
    added = _surplus(after, before, new - old)
    weaker, skips = [], []
    for path, now, then in _pairs(before, after):
        if now.asserts is not None and then.asserts is not None and now.asserts < then.asserts:
            weaker.append(f"{path}::{now.name}")
        if now.skipped is True and then.skipped is False:
            skips.append(f"{path}::{now.name}")
    return LossReport(base=base, base_ref=base_ref, added=added, removed=removed, net=sum(new.values()) - sum(old.values()),
                      fewer_assertions=tuple(sorted(weaker)), skips_added=tuple(sorted(skips)))


def _surplus(side: dict[str, list[Declaration]], other: dict[str, list[Declaration]],
             extra: Counter) -> tuple[str, ...]:
    """Name each extra declaration of ``side``, first in the files where its count went down."""
    here = {path: Counter(d.name for d in found) for path, found in side.items()}
    there = {path: Counter(d.name for d in found) for path, found in other.items()}
    names = []
    for name, number in extra.items():
        places = []
        for path in sorted(p for p in here if here[p][name]):
            count = here[path][name]
            places += [(count <= there.get(path, Counter())[name], path)] * count
        names += [f"{path}::{name}" for _, path in sorted(places)[:number]]
    return tuple(sorted(names))


def _pairs(before: dict[str, list[Declaration]], after: dict[str, list[Declaration]]):
    """Pair each test of the work tree with the same test at the base: same file first, then a move."""
    old: dict[tuple[str, str], list[Declaration]] = defaultdict(list)
    for path, found in before.items():
        for d in found:
            old[(path, d.name)].append(d)
    moved: list[tuple[str, Declaration]] = []
    for path in sorted(after):
        for d in after[path]:
            if old[(path, d.name)]:
                yield path, d, old[(path, d.name)].pop(0)
            else:
                moved.append((path, d))
    spare: dict[str, list[Declaration]] = defaultdict(list)
    for (path, name), rest in sorted(old.items()):
        spare[name] += rest
    for path, d in moved:
        if spare[d.name]:
            yield path, d, spare[d.name].pop(0)


def render(report: LossReport) -> str:
    """Return the report as text lines. Each name is on one line, with no control character."""
    from jacked.testdiet.record import clean_text

    lines = [f"base {report.base[:12]} ({one_line(report.base_ref)})", f"net {report.net:+d}" if report.net else "net 0",
             f"added {len(report.added)}, removed {len(report.removed)}"]
    for title, names in (("removed", report.removed), ("added", report.added),
                         ("fewer assertions", report.fewer_assertions),
                         ("skip mark added", report.skips_added)):
        lines += [f"{title} {one_line(name)}" for name in names]
    return clean_text("\n".join(lines))
