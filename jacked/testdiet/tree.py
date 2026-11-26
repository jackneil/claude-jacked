"""The identity of a git tree, the fingerprint of input files, and the identity of a machine.

``tree_id`` gives the hash of the tree that a commit of the work tree would
have. Changes that are not committed are part of it. It never changes the
real index or the real object store: it adds the work tree to a copy of the
index, and git writes the new objects into a temporary folder.

Limits of the identity: it does not hold a file that git ignores, and it does
not hold a change inside the work tree of a submodule. ``inputs_fingerprint``
adds named files to a record. The clean and smudge filters stay on, so the
hash is equal to the tree of a commit. With git-lfs, the clean filter can
write a copy of a new large file into the lfs folder of the clone. In a
sparse checkout, ``git add -A --sparse`` adds a file outside the sparse cone
too, and a tracked file that the cone leaves out of the work tree stays as
HEAD has it, so the hash is the tree of a full commit of the work tree.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from jacked import git_guard
from jacked.atomic_io import atomic_write_bytes
from jacked.findbin import find_bin
from jacked.packs import jacked_home
from jacked.regular_file import open_regular_fd
from jacked.testdiet.model import TreeId

logger = logging.getLogger(__name__)

#: The name of the store folder in the common git directory of a clone.
STORE_NAME = "jacked-tests"
#: The file of the machine identity, in the ``.claude`` folder of the jacked home.
MACHINE_ID_NAME = "jacked-tests-machine-id"

# The time that one git call can take. ``git add -A`` hashes each changed file
# of the work tree, so a large checkout needs minutes, not seconds. The limit
# only stops a git that hangs; no data is dropped.
_GIT_TIMEOUT_S = 600.0
# The configuration of the calls that write into the copy of the index. A
# split index keeps part of the index in a second file of the git directory;
# with this value git writes the copy as one file and never touches that one.
_INDEX_COPY_ARGS = ("-c", "core.splitIndex=false")
# A failure of the work-tree read is logged at WARNING one time in each process.
_warned_add = False
# The size of the machine identity, in random bytes (32 hex characters).
_MACHINE_ID_BYTES = 16


class TreeError(RuntimeError):
    """Git cannot give the answer (for example, a tree object is not in the repository)."""


class _All:
    """A git_guard consumer that keeps the whole output."""

    def __init__(self) -> None:
        self.data = bytearray()

    def feed(self, chunk: bytes) -> bool:
        self.data += chunk
        return True

    def close(self) -> None:
        return None


def _git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> tuple[int, bytes]:
    """Run git in ``cwd`` and return the exit code and the output.

    Each call has ``GIT_OPTIONAL_LOCKS=0``, so a read never writes the real
    index. A git that cannot start raises ``TreeError``.
    """
    binary = find_bin("git")
    if binary is None:
        raise TreeError("git is not installed")
    full_env = dict(os.environ)
    full_env.update(env or {})
    full_env["GIT_OPTIONAL_LOCKS"] = "0"
    out = _All()
    argv = [binary, "-C", str(cwd), *git_guard.GIT_CONFIG_OVERRIDE_ARGS, *args]
    try:
        code = git_guard.run_git(argv, name=args[0] if args[0] != "-c" else args[2],
                                 env=full_env, timeout=_GIT_TIMEOUT_S, consumer=out)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TreeError(f"git did not run: {type(exc).__name__}") from exc
    return code, bytes(out.data)


def _lines(data: bytes) -> list[str]:
    return [os.fsdecode(line) for line in data.splitlines() if line]


def tree_id(cwd: Path, *, objects: Path | None = None) -> TreeId | None:
    """Return the identity of the work tree that holds ``cwd``.

    The result is None for a folder that is not in a git repository, for a
    bare repository, for a repository with no commit, and when git fails.
    ``objects`` is a folder that the caller owns: when it is given, the
    objects of a tree with changes stay there, so ``changed_paths`` can read
    them later. When it is not given, they go into a temporary folder that
    this function removes.
    """
    try:
        return _tree_id(Path(cwd), objects)
    except _AddError as exc:
        global _warned_add
        level = logging.DEBUG if _warned_add else logging.WARNING
        _warned_add = True
        logger.log(level, "tree_id: no tree identity, the work tree read failed (%s)", exc)
        return None
    except (TreeError, OSError) as exc:
        logger.debug("tree_id: no tree identity (%s)", type(exc).__name__ if isinstance(exc, OSError)
                     else exc)
        return None


class _AddError(TreeError):
    """``git add`` or ``git write-tree`` on the copy of the index failed."""


def _tree_id(cwd: Path, objects: Path | None) -> TreeId | None:
    code, out = _git(cwd, "rev-parse", "--is-bare-repository")
    if code != 0 or out.strip() != b"false":
        return None
    heads = []
    for spec in ("HEAD^{commit}", "HEAD^{tree}"):
        code, out = _git(cwd, "rev-parse", "--verify", "-q", spec)
        if code != 0 or len(_lines(out)) != 1:
            return None
        heads += _lines(out)
    commit, head_tree = heads
    code, out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "index",
                     "--git-path", "objects")
    paths = _lines(out)
    if code != 0 or len(paths) != 2:
        raise TreeError("git did not give the index and object paths")
    real_index, real_objects = Path(paths[0]), paths[1]

    work = Path(tempfile.mkdtemp(prefix="jacked-tree-"))
    try:
        index_copy = work / "index"
        if real_index.is_file():
            shutil.copyfile(real_index, index_copy)
        object_dir = Path(objects) if objects is not None else work / "objects"
        object_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "GIT_INDEX_FILE": str(index_copy),
            "GIT_OBJECT_DIRECTORY": str(object_dir),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": _alternates(real_objects),
        }
        code, _ = _git(cwd, *_INDEX_COPY_ARGS, "add", "-A", "--sparse", env=env)
        if code != 0:
            raise _AddError(f"git add exited with {code}")
        code, out = _git(cwd, *_INDEX_COPY_ARGS, "write-tree", env=env)
        lines = _lines(out)
        if code != 0 or len(lines) != 1:
            raise _AddError(f"git write-tree exited with {code}")
    finally:
        remove_tree(work)
    return TreeId(commit=commit, tree=lines[0], clean=lines[0] == head_tree)


def remove_tree(path: Path) -> None:
    """Remove the folder ``path`` and all that it holds.

    Git makes its loose objects read-only, and Windows does not delete a
    read-only file. So a path that cannot be removed gets write permission
    (and its folder too), and the removal tries again. A path that still
    cannot be removed is logged at WARNING. A missing folder is not an error.
    """

    root = os.path.abspath(path)

    def retry(func, failed, exc) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            parent = os.path.dirname(os.path.abspath(failed))
            # Only a folder inside ``path`` gets a new mode, never the folder that holds it.
            if parent == root or parent.startswith(root + os.sep):
                os.chmod(parent, stat.S_IRWXU)
            if os.path.lexists(failed) and not os.path.isdir(failed):
                os.chmod(failed, stat.S_IREAD | stat.S_IWRITE)
            elif os.path.isdir(failed):
                os.chmod(failed, stat.S_IRWXU)
            func(failed)
        except FileNotFoundError:
            return
        except OSError as again:
            logger.warning("remove_tree: cannot remove %s (%s)", failed, type(again).__name__)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=lambda func, failed, info: retry(func, failed, info[1]))


def _alternates(real_objects: str) -> str:
    """The real object folder, plus the alternates that the environment already names."""
    present = os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
    return os.pathsep.join([real_objects, present] if present else [real_objects])


def changed_paths(cwd: Path, a: TreeId, b: TreeId, *, objects: Path | None = None) -> list[str]:
    """Return the sorted paths whose content or mode differs between the trees ``a`` and ``b``.

    The tree of a work tree with changes is only in the folder ``objects``
    that ``tree_id`` got. A tree object that git cannot find raises
    ``TreeError``: an empty list always means that no path differs.
    """
    cwd = Path(cwd)
    env: dict[str, str] = {}
    if objects is not None:
        code, out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        if code != 0:
            raise TreeError("git did not give the object path")
        env = {"GIT_OBJECT_DIRECTORY": str(objects),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": _alternates(_lines(out)[0])}
    code, out = _git(cwd, "diff-tree", "-r", "-z", "--name-only", "--no-renames", "--end-of-options",
                     a.tree, b.tree, env=env)
    if code != 0:
        raise TreeError(f"git diff-tree exited with {code}; a tree object can be missing")
    return sorted(os.fsdecode(name) for name in out.split(b"\0") if name)


def inputs_fingerprint(cwd: Path, globs: Sequence[str]) -> str | None:
    """Return a SHA-256 of the files that ``globs`` match, or None when no glob is given.

    A relative glob starts at ``cwd``. ``**`` matches folders at all depths,
    and a hidden file matches. Each file gives its path, its size and its
    modification time in nanoseconds, not its content. A glob that matches
    no file is in the hash too.
    """
    if not globs:
        return None
    cwd = Path(cwd)
    entries: list[list] = []
    for pattern in globs:
        found: list[list] = []
        for name in glob.glob(pattern, root_dir=cwd, recursive=True, include_hidden=True):
            path = Path(name) if os.path.isabs(name) else cwd / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file():
                found.append([Path(name).as_posix(), stat.st_size, stat.st_mtime_ns])
        entries.append([pattern, sorted(found)])
    text = json.dumps(entries, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def common_dir(cwd: Path) -> Path:
    """Return the common git directory of the clone that holds ``cwd``.

    All worktrees of one clone have the same common directory. A folder that
    is not in a git repository raises ``TreeError``.
    """
    code, out = _git(Path(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir")
    lines = _lines(out)
    if code != 0 or len(lines) != 1:
        raise TreeError("the folder is not in a git repository")
    return Path(lines[0])


def store_dir(cwd: Path) -> Path:
    """Return the history store of the clone that holds ``cwd``: ``<common dir>/jacked-tests``.

    Git never commits a file in its own directory.
    """
    return common_dir(cwd) / STORE_NAME


def on_default_branch(cwd: Path, tree: TreeId) -> bool | None:
    """Return True when ``tree`` is clean and its commit is an ancestor of the remote default branch.

    The default branch is the commit that ``refs/remotes/origin/HEAD``
    names. There is no fetch and no network call. The result is None when
    that ref does not exist or when git fails, and False for a tree with
    changes.
    """
    cwd = Path(cwd)
    try:
        code, out = _git(cwd, "rev-parse", "--verify", "-q", "refs/remotes/origin/HEAD^{commit}")
        lines = _lines(out)
        if code != 0 or len(lines) != 1:
            return None
        if not tree.clean:
            return False
        code, _ = _git(cwd, "merge-base", "--is-ancestor", "--end-of-options", tree.commit,
                       lines[0])
    except TreeError as exc:
        logger.debug("on_default_branch: no answer (%s)", exc)
        return None
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def command_sha(argv: Sequence[str]) -> str:
    """Return the SHA-256 of the arguments as the caller typed them, joined by a NUL.

    The join is not ambiguous, because an argument of a real command line
    cannot hold a NUL. The text is UTF-8 with "surrogatepass", so a lone
    surrogate (from a Windows command line) gives a hash and never an error.

    >>> command_sha(["a b"]) == command_sha(["a", "b"])
    False
    """
    text = "\0".join(argv)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def machine_id(home: Path | None = None) -> str | None:
    """Return the private random identity of this machine, and make it on first use.

    The identity is 32 random hex characters in
    ``<home>/.claude/jacked-tests-machine-id``. ``home`` is the jacked home
    when it is not given. The file has mode 0600 where the system has
    modes. The identity holds no part of the host name. Two processes that
    make it at the same time get the same identity. The result is None when
    the file cannot be read or made, or holds no valid identity.
    """
    path = Path(home if home is not None else jacked_home()) / ".claude" / MACHINE_ID_NAME
    try:
        if not os.path.lexists(path):
            _make_machine_id(path)
        return _read_machine_id(path)
    except (OSError, ValueError) as exc:
        logger.debug("machine_id: no machine identity (%s)", type(exc).__name__)
        return None


def _make_machine_id(path: Path) -> None:
    """Make the file with a complete content, or keep the file that another process made.

    The value goes into a private temporary file first. A hard link then puts
    it at ``path``; the link fails when ``path`` exists, so the first process
    wins and no reader sees a partial file.
    """
    value = secrets.token_hex(_MACHINE_ID_BYTES)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    atomic_write_bytes(temp, (value + "\n").encode("ascii"), mode=0o600)
    try:
        os.link(temp, path)
    except FileExistsError:
        pass
    finally:
        temp.unlink(missing_ok=True)


def _read_machine_id(path: Path) -> str | None:
    fd = open_regular_fd(path)
    try:
        data = os.read(fd, 4096)
    finally:
        os.close(fd)
    value = data.decode("ascii").strip()
    if len(value) == 2 * _MACHINE_ID_BYTES and all(c in "0123456789abcdef" for c in value):
        return value
    return None
