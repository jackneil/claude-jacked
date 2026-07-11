"""Break-glass override machinery for the agent-reach integration.

Extracted from the runner so ``agent_reach.py`` stays under the repo's per-file
line guardrail and holds only install/status/remove orchestration. These are
free functions that take the runner's primitives explicitly (its settings
accessors, its ``run``/``require_bin`` subprocess helpers, its home dir), so this
module never imports the runner and there is no import cycle.

A break-glass override installs an UNVETTED upstream commit the user explicitly
acknowledged. The SHA + ack + timestamp persist in the DB ``settings`` table; the
compiled (still fully pinned, just unvetted) constraints live next to the state
file. ``resolve_override`` auto-clears the override once jacked's shipped pin
reaches the overridden SHA (the equality case; true ancestry needs git history we
deliberately do not fetch in the network-light status path).
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

from jacked.integrations._util import ReachUserError, now_iso, truthy

logger = logging.getLogger(__name__)

# DB settings keys owned by the injected persistence layer. Kept here (and
# re-exported by agent_reach) so system._PROTECTED_SETTING_KEYS and the runner
# share one definition.
SETTING_OVERRIDE_SHA = "reach_override_sha"
SETTING_OVERRIDE_ACK = "reach_override_ack"
SETTING_OVERRIDE_AT = "reach_override_at"

OVERRIDE_CONSTRAINTS_NAME = "jacked-reach-override-constraints.txt"

# Network subprocess budget for ls-remote / pip compile.
NETWORK_TIMEOUT = 120

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")

SettingGetter = Callable[[str], Optional[str]]
SettingSetter = Callable[[str, Optional[str]], None]
# run(cmd: list[str], *, timeout: int, check: bool = True) -> CompletedProcess
RunFn = Callable[..., object]
RequireBinFn = Callable[[str], str]


def override_constraints_path(home: Path) -> Path:
    return home / ".claude" / OVERRIDE_CONSTRAINTS_NAME


def resolve_override(
    get_setting: SettingGetter,
    set_setting: SettingSetter,
    home: Path,
    pin_sha: str,
    *,
    mutate: bool,
) -> tuple[str | None, bool]:
    """Return ``(override_sha, active)``.

    An override auto-clears once jacked's shipped pin advances TO the overridden
    SHA. Only the equality case is detectable offline (true ancestry needs git
    history). ``mutate`` gates the side effect: mutating ops (install/update) pass
    ``mutate=True`` so a satisfied override is cleaned up; ``status()`` passes
    ``mutate=False`` so a read never writes/unlinks (it just reports inactive).
    """
    sha = get_setting(SETTING_OVERRIDE_SHA)
    if not sha or not truthy(get_setting(SETTING_OVERRIDE_ACK)):
        return None, False
    if sha == pin_sha:
        if mutate:
            clear_override_state(set_setting, home)
        return None, False
    return sha, True


def clear_override_state(set_setting: SettingSetter, home: Path) -> None:
    """Delete the override DB keys and the compiled override constraints file."""
    set_setting(SETTING_OVERRIDE_SHA, None)
    set_setting(SETTING_OVERRIDE_ACK, None)
    set_setting(SETTING_OVERRIDE_AT, None)
    try:
        override_constraints_path(home).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("could not remove override constraints file: %s", e)


def resolve_ref(upstream: str, ref: str, run: RunFn, require_bin: RequireBinFn) -> str:
    """Resolve a user ref to a full commit SHA via ``git ls-remote``.

    A raw 40-hex SHA is accepted verbatim. For a symbolic ref, an ANNOTATED tag
    makes ls-remote emit two lines: the tag-object SHA, then the peeled commit as
    ``<sha>\\t<ref>^{}``. We prefer the peeled line so an annotated tag resolves to
    the commit uv can actually install (not the unusable tag-object SHA).
    """
    if _HEX40.fullmatch(ref):
        return ref.lower()
    # Defense-in-depth: a ref beginning with '-' would be parsed by git as an
    # OPTION (argument injection). Reject it before the argv, and end the option
    # list with '--' so no ref can be reinterpreted as a flag.
    if not ref or ref.startswith("-"):
        raise ReachUserError(f"invalid ref {ref!r}: a ref may not start with '-'")
    git = require_bin("git")
    proc = run([git, "ls-remote", upstream, "--", ref], timeout=NETWORK_TIMEOUT)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise ReachUserError(f"could not resolve ref {ref!r} against {upstream}")
    sha = _pick_sha(lines)
    if not _HEX40.fullmatch(sha):
        raise RuntimeError(f"git ls-remote returned an unexpected sha for {ref!r}: {sha!r}")
    return sha.lower()


def _pick_sha(lines: list[str]) -> str:
    """Prefer the peeled-commit line (ref ending ``^{}``) for annotated tags."""
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith("^{}"):
            return parts[0]
    return lines[0].split()[0]


def compile_constraints(
    upstream: str, sha: str, out_path: Path, run: RunFn, require_bin: RequireBinFn
) -> None:
    """Compile a fully-pinned constraints file for the override SHA via uv."""
    uv = require_bin("uv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req_line = f"agent-reach @ git+{upstream}@{sha}\n"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".in", delete=False, encoding="utf-8")
    try:
        tmp.write(req_line)
        tmp.close()
        # --generate-hashes so the override path also gets artifact-hash pinning
        # (the runner's require-hashes pre-flight enforces it). --no-emit-package
        # agent-reach drops the un-hashable VCS root line from the output (a git+
        # ref can't carry a --hash), leaving a deps-only, fully-hashed file that
        # `uv pip install --require-hashes` accepts; agent-reach itself is
        # integrity-pinned by its commit SHA in the tool-install URL.
        run([uv, "pip", "compile", "--generate-hashes", "--no-emit-package", "agent-reach",
             tmp.name, "-o", str(out_path)], timeout=NETWORK_TIMEOUT)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def set_override(
    ref: str,
    *,
    ack: bool,
    upstream: str,
    get_setting: SettingGetter,
    set_setting: SettingSetter,
    home: Path,
    run: RunFn,
    require_bin: RequireBinFn,
) -> dict:
    """Break-glass: resolve ref->SHA, compile fresh (unvetted) constraints, persist.

    Requires explicit ``ack=True``. A raw 40-hex SHA is accepted verbatim; any
    other ref is resolved (annotated-tag aware) via ``git ls-remote``.
    """
    if not ack:
        raise ReachUserError(
            "break-glass override requires explicit acknowledgement (ack=True): "
            "this installs UNVETTED upstream code"
        )
    sha = resolve_ref(upstream, ref, run, require_bin)
    constraints_path = override_constraints_path(home)
    compile_constraints(upstream, sha, constraints_path, run, require_bin)
    set_setting(SETTING_OVERRIDE_SHA, sha)
    set_setting(SETTING_OVERRIDE_ACK, "true")
    set_setting(SETTING_OVERRIDE_AT, now_iso())
    return {"override_sha": sha, "constraints": str(constraints_path)}
