"""Per-repo post-merge git hook install/remove for the memory vault.

The ``post-merge-memory.sh`` template (one language-independent script) is
copied into a repo's ``.git/hooks/post-merge`` so a landed merge on main/master
triggers ``jacked memory capture-merge``. Install/remove mirror the guardrails
lint-hook contract (guardrails.install_hook): ``.git`` required, foreign hooks
refused unless ``force``, hook-framework conflicts (husky/pre-commit/lefthook/
core.hooksPath) refused, executable bit set, and a distinct marker so we never
touch a hook we did not write.

Unlike the lint hook there is one template for every language (the script only
shells out to ``jacked``), so there is no language detection here.
"""
from __future__ import annotations

import logging
import stat
from pathlib import Path

from jacked import guardrails
from jacked.findbin import find_bin
from jacked.memory import vault as vault_mod

logger = logging.getLogger(__name__)

# Placeholder in the template that install substitutes with the absolute jacked
# binary path (so the hook fires even under a stripped PATH).
_JACKED_BIN_PLACEHOLDER = "@JACKED_BIN@"

# Bundled template dir (jacked/data/git-hooks), with the same global-copy
# fallback guardrails uses (deploy_templates globs git-hooks/*.sh into it).
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_GIT_HOOKS_DIR = _DATA_DIR / "git-hooks"
_TEMPLATE_NAME = "post-merge-memory.sh"

# Distinct from guardrails' "# jacked-lint-hook" so pre-push and post-merge
# are never confused, and a foreign post-merge is never clobbered.
HOOK_MARKER = "# jacked-memory-hook"

_HOOK_NAME = "post-merge"


def _resolve_template() -> Path | None:
    """The post-merge template: bundled first, then the deployed global copy."""
    src = _GIT_HOOKS_DIR / _TEMPLATE_NAME
    if src.exists():
        return src
    fallback = guardrails.HOOKS_GLOBAL / _TEMPLATE_NAME
    if fallback.exists():
        return fallback
    return None


def install_post_merge(repo_path: str | Path, force: bool = False) -> dict:
    """Install the post-merge memory hook into ``repo_path``/.git/hooks/.

    Returns a result dict:
      - ``{"installed": True, "path": ...}``            wrote (new or refreshed)
      - ``{"installed": False, "path": ..., "reason": "already installed"}``
                                                        our hook, already current
      - ``{"installed": False, "reason": ...}``         refused / unavailable

    A pre-existing hook that is OURS (carries ``HOOK_MARKER``) is refreshed in
    place only when its content differs from the template. A foreign post-merge
    hook is refused unless ``force``; a detected hook framework is refused unless
    ``force``.
    """
    repo = Path(repo_path)
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return {"installed": False, "reason": "no .git directory found"}

    src = _resolve_template()
    if src is None:
        return {"installed": False, "reason": "post-merge memory hook template not found"}
    # Substitute the absolute jacked path at install time so the already-installed
    # content comparison below compares the SUBSTITUTED text (not the raw template).
    jacked_bin = find_bin("jacked") or ""
    new_content = src.read_text(encoding="utf-8").replace(_JACKED_BIN_PLACEHOLDER, jacked_bin)

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    target = hooks_dir / _HOOK_NAME

    existing_is_ours = False
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if HOOK_MARKER in existing:
            existing_is_ours = True
            if existing == new_content:
                return {
                    "installed": False,
                    "path": str(target),
                    "reason": "already installed",
                }
        elif not force:
            return {
                "installed": False,
                "path": str(target),
                "reason": "post-merge hook already exists (use force to overwrite)",
            }

    # Framework conflict only gates NEW installs or foreign overwrites; refreshing
    # our own hook (a template migration) is always allowed.
    if not existing_is_ours:
        framework = guardrails._detect_hook_framework(repo)
        if framework and not force:
            return {
                "installed": False,
                "reason": (
                    f"existing hook framework detected: {framework}. "
                    "Integrate manually or use force."
                ),
            }

    target.write_text(new_content, encoding="utf-8")
    try:
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    return {"installed": True, "path": str(target)}


def remove_post_merge(repo_path: str | Path) -> bool:
    """Delete ``.git/hooks/post-merge`` ONLY if it is ours. Returns True if removed.

    A foreign post-merge hook (no ``HOOK_MARKER``) is left untouched.
    """
    target = Path(repo_path) / ".git" / "hooks" / _HOOK_NAME
    if not target.exists():
        return False
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return False
    if HOOK_MARKER not in content:
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Bulk install/remove across the vault's mapped repos (M7's toggle calls these)
# --------------------------------------------------------------------------- #

def _mapped_repos(vault: Path, roots) -> list[tuple[Path, str]]:
    """Discovered repos under ``roots`` that are mapped in the vault's vault.json."""
    cfg = vault_mod.load_vault_config(vault)
    repo_map = cfg.get("repo_map", {}) or {}
    discovered = vault_mod.scan_roots([Path(r) for r in roots])
    return [(path, ident) for path, ident in discovered if ident in repo_map]


def _install_summary(res: dict) -> dict:
    """Normalize install_post_merge output for the per-repo bulk result.

    "already installed" is a HEALTHY state (idempotent re-run), reported as
    ``current`` -- not as a skip, so re-enabling never shows warnings for hooks
    that are exactly where they should be. Only genuine refusals (foreign hook,
    framework conflict, no .git) carry ``skipped``.
    """
    if res.get("installed"):
        return {"installed": True, "path": res.get("path")}
    if res.get("reason") == "already installed":
        return {"installed": False, "current": True, "path": res.get("path")}
    return {
        "installed": False,
        "path": res.get("path"),
        "skipped": True,
        "reason": res.get("reason", ""),
    }


def install_for_mapped_repos(vault: Path, roots, *, force: bool = False) -> dict:
    """Install the post-merge hook into every mapped repo under ``roots``.

    Returns ``{identity: {installed/skipped, reason, path}}``. Refusals (foreign
    hook, framework conflict, no .git) are surfaced in the result, never forced.
    """
    results: dict = {}
    for path, identity in _mapped_repos(vault, roots):
        results[identity] = _install_summary(install_post_merge(path, force=force))
    return results


def remove_for_mapped_repos(vault: Path, roots) -> dict:
    """Remove OUR post-merge hook from every mapped repo under ``roots``.

    Returns ``{identity: {"removed": bool, "path": str}}``. Foreign hooks are
    never touched (``removed`` is False for them).
    """
    results: dict = {}
    for path, identity in _mapped_repos(vault, roots):
        results[identity] = {
            "removed": remove_post_merge(path),
            "path": str(Path(path) / ".git" / "hooks" / _HOOK_NAME),
        }
    return results
