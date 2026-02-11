"""
Guardrails template management and git hook installation.

Provides language detection, guardrails creation, and pre-push
hook installation for jacked-managed projects.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Optional


# Where bundled templates live inside the package
_DATA_DIR = Path(__file__).parent / "data"
GUARDRAILS_TEMPLATES = _DATA_DIR / "guardrails"
HOOK_TEMPLATES = _DATA_DIR / "git-hooks"

# Where global copies are deployed by `jacked install`
_CLAUDE_DIR = Path.home() / ".claude"
GUARDRAILS_GLOBAL = _CLAUDE_DIR / "jacked-guardrails"
HOOKS_GLOBAL = _CLAUDE_DIR / "jacked-hooks"

# Hook marker searched in .git/hooks/pre-push to identify our hooks
HOOK_MARKER = "# jacked-lint-hook"

# Filenames we recognize as guardrails (checked in order, first match wins)
GUARDRAILS_FILENAMES = [
    "JACKED_GUARDRAILS.md",
    "DESIGN_GUARDRAILS.md",
    "design_guardrails.md",
    "Design_Guardrails.md",
    "GUARDRAILS.md",
    "guardrails.md",
    "Guardrails.md",
]

# Language detection: config file -> language name
_LANGUAGE_INDICATORS = [
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
    ("package.json", "node"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
]

# Hook frameworks that might conflict
_HOOK_FRAMEWORKS = [
    ".husky",
    ".pre-commit-config.yaml",
    "lefthook.yml",
    "lefthook.yaml",
]


def detect_language(repo_path: str | Path) -> Optional[str]:
    """Detect the primary language of a repository.

    Checks for config files in priority order:
    pyproject.toml > package.json > Cargo.toml > go.mod

    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> open(os.path.join(d, 'pyproject.toml'), 'w').close()
    >>> detect_language(d)
    'python'

    >>> d2 = tempfile.mkdtemp()
    >>> detect_language(d2) is None
    True
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return None
    for config_file, language in _LANGUAGE_INDICATORS:
        if (repo / config_file).exists():
            return language
    return None


def create_guardrails(
    repo_path: str | Path,
    language: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Create JACKED_GUARDRAILS.md in a project from templates.

    Combines base.md + language-specific template into a single file.
    Returns dict with status info.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> result = create_guardrails(d, language='python')
    >>> result['created']
    True
    >>> 'Design Guardrails' in open(result['path']).read()
    True
    """
    repo = Path(repo_path)
    target = repo / "JACKED_GUARDRAILS.md"

    # Check for any existing variant before creating
    if not force:
        for gname in GUARDRAILS_FILENAMES:
            existing = repo / gname
            if existing.exists():
                return {
                    "created": False,
                    "path": str(existing),
                    "reason": f"already exists: {gname} (use --force to overwrite)",
                }
    else:
        # --force: clean up old variant files to prevent duplicates
        for gname in GUARDRAILS_FILENAMES:
            old = repo / gname
            if old.exists() and old.name != target.name:
                old.unlink()

    if language is None:
        language = detect_language(repo)

    # Read base template
    base_src = GUARDRAILS_TEMPLATES / "base.md"
    if not base_src.exists():
        # Fall back to global copy
        base_src = GUARDRAILS_GLOBAL / "base.md"
    if not base_src.exists():
        return {"created": False, "path": str(target), "reason": "base template not found"}

    content = base_src.read_text(encoding="utf-8")

    # Append language-specific template if available
    if language:
        lang_src = GUARDRAILS_TEMPLATES / f"{language}.md"
        if not lang_src.exists():
            lang_src = GUARDRAILS_GLOBAL / f"{language}.md"
        if lang_src.exists():
            content += "\n" + lang_src.read_text(encoding="utf-8")

    target.write_text(content, encoding="utf-8")
    return {
        "created": True,
        "path": str(target),
        "language": language,
    }


def _detect_hook_framework(repo_path: Path) -> Optional[str]:
    """Check if repo uses an existing hook framework.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> _detect_hook_framework(Path(d)) is None
    True
    """
    for framework_file in _HOOK_FRAMEWORKS:
        if (repo_path / framework_file).exists():
            return framework_file
    # Check core.hooksPath
    git_config = repo_path / ".git" / "config"
    if git_config.exists():
        try:
            text = git_config.read_text(encoding="utf-8")
            if "hooksPath" in text:
                return "core.hooksPath"
        except Exception:
            pass
    return None


def install_hook(
    repo_path: str | Path,
    language: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Install a pre-push lint hook in a project's .git/hooks/.

    Copies the language-appropriate hook template to .git/hooks/pre-push
    (extensionless, as git requires).

    >>> import tempfile, os
    >>> d = tempfile.mkdtemp()
    >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
    >>> result = install_hook(d, language='python')
    >>> result['installed']
    True
    >>> os.path.basename(result['path'])
    'pre-push'
    """
    repo = Path(repo_path)
    git_dir = repo / ".git"

    if not git_dir.is_dir():
        return {"installed": False, "reason": "no .git directory found"}

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    target = hooks_dir / "pre-push"

    # Check for existing hook (not ours)
    if target.exists() and not force:
        try:
            existing = target.read_text(encoding="utf-8")
            if HOOK_MARKER in existing:
                return {
                    "installed": False,
                    "path": str(target),
                    "reason": "jacked hook already installed (use --force to overwrite)",
                }
        except Exception:
            pass
        return {
            "installed": False,
            "path": str(target),
            "reason": "pre-push hook already exists (use --force to overwrite)",
        }

    # Check for hook frameworks
    framework = _detect_hook_framework(repo)
    if framework and not force:
        return {
            "installed": False,
            "reason": f"existing hook framework detected: {framework}. Integrate manually or use --force.",
        }

    if language is None:
        language = detect_language(repo)

    if not language:
        return {"installed": False, "reason": "could not detect language — specify with --language"}

    # Find the template
    template_name = f"pre-push-{language}.sh"
    src = HOOK_TEMPLATES / template_name
    if not src.exists():
        src = HOOKS_GLOBAL / template_name
    if not src.exists():
        return {"installed": False, "reason": f"no hook template for language: {language}"}

    # Copy template to .git/hooks/pre-push (extensionless)
    shutil.copy2(src, target)

    # Set executable bit (no-op on Windows/NTFS, but correct for Unix)
    try:
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass

    return {
        "installed": True,
        "path": str(target),
        "language": language,
    }


def check_project_setup(repo_path: str | Path) -> dict:
    """Check if a project has guardrails and lint hook configured.

    Returns:
        dict with has_guardrails, has_lint_hook, detected_language.
        Returns {False, False, None} if repo_path has no .git/ directory.

    >>> import tempfile
    >>> d = tempfile.mkdtemp()
    >>> result = check_project_setup(d)
    >>> result['has_guardrails']
    False
    >>> result['has_lint_hook']
    False
    """
    repo = Path(repo_path)
    result = {
        "has_guardrails": False,
        "guardrails_file": None,
        "has_lint_hook": False,
        "detected_language": None,
        "env_path": None,
        "has_lessons": False,
        "lessons_count": 0,
    }

    if not repo.is_dir():
        return result

    # Check for guardrails file (flexible naming — first match wins)
    for gname in GUARDRAILS_FILENAMES:
        gpath = repo / gname
        if gpath.exists():
            result["has_guardrails"] = True
            result["guardrails_file"] = gname
            break

    # Check .git/hooks/pre-push for our marker
    pre_push = repo / ".git" / "hooks" / "pre-push"
    if pre_push.exists():
        try:
            content = pre_push.read_text(encoding="utf-8")
            result["has_lint_hook"] = HOOK_MARKER in content
        except Exception:
            pass

    # Check for lessons.md
    lessons_file = repo / "lessons.md"
    if lessons_file.exists():
        result["has_lessons"] = True
        try:
            text = lessons_file.read_text(encoding="utf-8")
            result["lessons_count"] = sum(1 for line in text.splitlines() if line.strip().startswith("- "))
        except Exception:
            pass

    # Read .git/jacked/env if present
    env_file = repo / ".git" / "jacked" / "env"
    if env_file.exists():
        try:
            env_path = env_file.read_text(encoding="utf-8").strip()
            if env_path:
                result["env_path"] = env_path
        except Exception:
            pass

    # Detect language
    result["detected_language"] = detect_language(repo)

    return result


def deploy_templates(force: bool = False) -> dict:
    """Deploy guardrails and hook templates to global ~/.claude/ directories.

    Called by `jacked install`. Copies bundled templates to:
    - ~/.claude/jacked-guardrails/ (5 .md files)
    - ~/.claude/jacked-hooks/ (4 .sh files)

    >>> # This function writes to ~/.claude/ so we don't doctest it fully
    """
    results = {"guardrails": [], "hooks": []}

    # Deploy guardrails templates
    GUARDRAILS_GLOBAL.mkdir(parents=True, exist_ok=True)
    for src in GUARDRAILS_TEMPLATES.glob("*.md"):
        dst = GUARDRAILS_GLOBAL / src.name
        if dst.exists() and not force:
            results["guardrails"].append({"file": src.name, "skipped": True})
            continue
        shutil.copy2(src, dst)
        results["guardrails"].append({"file": src.name, "deployed": True})

    # Deploy hook templates
    HOOKS_GLOBAL.mkdir(parents=True, exist_ok=True)
    for src in HOOK_TEMPLATES.glob("*.sh"):
        dst = HOOKS_GLOBAL / src.name
        if dst.exists() and not force:
            results["hooks"].append({"file": src.name, "skipped": True})
            continue
        shutil.copy2(src, dst)
        results["hooks"].append({"file": src.name, "deployed": True})

    return results
