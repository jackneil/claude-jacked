"""Tests for jacked.guardrails module."""

import os
from pathlib import Path


from jacked import guardrails


class TestDetectLanguage:
    """Tests for detect_language() — auto-detect repo language from config files."""

    def test_python_pyproject(self, tmp_path):
        """Detects Python from pyproject.toml.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/pyproject.toml', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'python'
        """
        (tmp_path / "pyproject.toml").touch()
        assert guardrails.detect_language(tmp_path) == "python"

    def test_python_setup_py(self, tmp_path):
        """Detects Python from setup.py.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/setup.py', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'python'
        """
        (tmp_path / "setup.py").touch()
        assert guardrails.detect_language(tmp_path) == "python"

    def test_python_requirements(self, tmp_path):
        """Detects Python from requirements.txt.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/requirements.txt', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'python'
        """
        (tmp_path / "requirements.txt").touch()
        assert guardrails.detect_language(tmp_path) == "python"

    def test_node(self, tmp_path):
        """Detects Node from package.json.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/package.json', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'node'
        """
        (tmp_path / "package.json").touch()
        assert guardrails.detect_language(tmp_path) == "node"

    def test_rust(self, tmp_path):
        """Detects Rust from Cargo.toml.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/Cargo.toml', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'rust'
        """
        (tmp_path / "Cargo.toml").touch()
        assert guardrails.detect_language(tmp_path) == "rust"

    def test_go(self, tmp_path):
        """Detects Go from go.mod.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/go.mod', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'go'
        """
        (tmp_path / "go.mod").touch()
        assert guardrails.detect_language(tmp_path) == "go"

    def test_no_language(self, tmp_path):
        """Returns None for empty directory.

        >>> import tempfile
        >>> from jacked.guardrails import detect_language
        >>> detect_language(tempfile.mkdtemp()) is None
        True
        """
        assert guardrails.detect_language(tmp_path) is None

    def test_nonexistent_path(self):
        """Returns None for path that doesn't exist.

        >>> from jacked.guardrails import detect_language
        >>> detect_language('/nonexistent/path') is None
        True
        """
        assert guardrails.detect_language("/nonexistent/path") is None

    def test_priority_python_over_node(self, tmp_path):
        """Python config files take priority over Node in monorepos.

        >>> import tempfile; d = tempfile.mkdtemp()
        >>> open(d + '/pyproject.toml', 'w').close()
        >>> open(d + '/package.json', 'w').close()
        >>> from jacked.guardrails import detect_language
        >>> detect_language(d)
        'python'
        """
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "package.json").touch()
        assert guardrails.detect_language(tmp_path) == "python"


class TestCreateGuardrails:
    """Tests for create_guardrails() — writes JACKED_GUARDRAILS.md to project."""

    def test_creates_file(self, tmp_path):
        """Creates JACKED_GUARDRAILS.md from templates.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> result = create_guardrails(d, language='python')
        >>> result['created']
        True
        """
        result = guardrails.create_guardrails(tmp_path, language="python")
        assert result["created"] is True
        target = tmp_path / "JACKED_GUARDRAILS.md"
        assert target.exists()

    def test_contains_base_content(self, tmp_path):
        """Output contains base guardrails content.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> result = create_guardrails(d, language='python')
        >>> 'Design Guardrails' in open(result['path']).read()
        True
        """
        result = guardrails.create_guardrails(tmp_path, language="python")
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "# Design Guardrails" in content
        assert "Run /dc before any commit" in content
        assert "Run the project linter before pushing" in content

    def test_contains_python_content(self, tmp_path):
        """Output includes Python-specific rules when language=python.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> result = create_guardrails(d, language='python')
        >>> 'ruff' in open(result['path']).read()
        True
        """
        result = guardrails.create_guardrails(tmp_path, language="python")
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "Python-Specific" in content
        assert "ruff" in content

    def test_contains_node_content(self, tmp_path):
        """Output includes Node-specific rules when language=node.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> result = create_guardrails(d, language='node')
        >>> 'eslint' in open(result['path']).read()
        True
        """
        result = guardrails.create_guardrails(tmp_path, language="node")
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "Node/JavaScript/TypeScript-Specific" in content
        assert "eslint" in content

    def test_auto_detects_language(self, tmp_path):
        """Auto-detects language when not specified.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'Cargo.toml'), 'w').close()
        >>> from jacked.guardrails import create_guardrails
        >>> result = create_guardrails(d)
        >>> result['language']
        'rust'
        """
        (tmp_path / "Cargo.toml").touch()
        result = guardrails.create_guardrails(tmp_path)
        assert result["created"] is True
        assert result["language"] == "rust"

    def test_no_overwrite_without_force(self, tmp_path):
        """Won't overwrite existing file without --force.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> create_guardrails(d, language='python')['created']
        True
        >>> create_guardrails(d, language='python')['created']
        False
        """
        guardrails.create_guardrails(tmp_path, language="python")
        result = guardrails.create_guardrails(tmp_path, language="python")
        assert result["created"] is False
        assert "already exists" in result["reason"]

    def test_no_overwrite_old_variant_without_force(self, tmp_path):
        """Won't overwrite old DESIGN_GUARDRAILS.md without --force.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> import os; open(os.path.join(d, 'DESIGN_GUARDRAILS.md'), 'w').close()
        >>> create_guardrails(d, language='python')['created']
        False
        """
        (tmp_path / "DESIGN_GUARDRAILS.md").write_text("old guardrails")
        result = guardrails.create_guardrails(tmp_path, language="python")
        assert result["created"] is False
        assert "already exists" in result["reason"]

    def test_overwrite_with_force(self, tmp_path):
        """Overwrites existing file with --force.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> create_guardrails(d, language='python')['created']
        True
        >>> create_guardrails(d, language='node', force=True)['created']
        True
        """
        guardrails.create_guardrails(tmp_path, language="python")
        result = guardrails.create_guardrails(tmp_path, language="node", force=True)
        assert result["created"] is True
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "Node/JavaScript/TypeScript-Specific" in content

    def test_force_cleans_old_variants(self, tmp_path):
        """--force deletes old variant files to prevent duplicates.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'DESIGN_GUARDRAILS.md'), 'w').close()
        >>> from jacked.guardrails import create_guardrails
        >>> result = create_guardrails(d, language='python', force=True)
        >>> result['created']
        True
        >>> os.path.exists(os.path.join(d, 'DESIGN_GUARDRAILS.md'))
        False
        """
        (tmp_path / "DESIGN_GUARDRAILS.md").write_text("old guardrails")
        (tmp_path / "guardrails.md").write_text("another old one")
        result = guardrails.create_guardrails(tmp_path, language="python", force=True)
        assert result["created"] is True
        assert (tmp_path / "JACKED_GUARDRAILS.md").exists()
        assert not (tmp_path / "DESIGN_GUARDRAILS.md").exists()
        assert not (tmp_path / "guardrails.md").exists()

    def test_base_only_when_no_language(self, tmp_path):
        """Creates base-only guardrails when language is unknown.

        >>> import tempfile
        >>> from jacked.guardrails import create_guardrails
        >>> d = tempfile.mkdtemp()
        >>> result = create_guardrails(d)
        >>> result['created']
        True
        """
        result = guardrails.create_guardrails(tmp_path)
        assert result["created"] is True
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "# Design Guardrails" in content


class TestInstallHook:
    """Tests for install_hook() — installs pre-push hook to .git/hooks/."""

    def _make_git_repo(self, tmp_path):
        """Helper: create minimal .git structure."""
        git_dir = tmp_path / ".git" / "hooks"
        git_dir.mkdir(parents=True)
        return tmp_path

    def test_installs_python_hook(self, tmp_path):
        """Installs Python pre-push hook.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d, language='python')
        >>> result['installed']
        True
        """
        self._make_git_repo(tmp_path)
        result = guardrails.install_hook(tmp_path, language="python")
        assert result["installed"] is True
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        assert hook.exists()
        content = hook.read_text(encoding="utf-8")
        assert "# jacked-lint-hook" in content
        assert "ruff" in content

    def test_hook_extensionless(self, tmp_path):
        """Hook file has no extension (git requirement).

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d, language='python')
        >>> os.path.basename(result['path'])
        'pre-push'
        """
        self._make_git_repo(tmp_path)
        result = guardrails.install_hook(tmp_path, language="python")
        assert os.path.basename(result["path"]) == "pre-push"

    def test_no_git_dir(self, tmp_path):
        """Fails gracefully when no .git directory.

        >>> import tempfile
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(tempfile.mkdtemp(), language='python')
        >>> result['installed']
        False
        """
        result = guardrails.install_hook(tmp_path, language="python")
        assert result["installed"] is False
        assert "no .git" in result["reason"]

    def test_no_overwrite_existing_hook(self, tmp_path):
        """Won't overwrite non-jacked hook without --force.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> open(os.path.join(d, '.git', 'hooks', 'pre-push'), 'w').write('other hook')
        10
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d, language='python')
        >>> result['installed']
        False
        """
        self._make_git_repo(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho other hook\n")
        result = guardrails.install_hook(tmp_path, language="python")
        assert result["installed"] is False
        assert "already exists" in result["reason"]

    def test_no_overwrite_jacked_hook(self, tmp_path):
        """Won't reinstall jacked hook without --force.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> from jacked.guardrails import install_hook
        >>> install_hook(d, language='python')['installed']
        True
        >>> install_hook(d, language='python')['installed']
        False
        """
        self._make_git_repo(tmp_path)
        guardrails.install_hook(tmp_path, language="python")
        result = guardrails.install_hook(tmp_path, language="python")
        assert result["installed"] is False
        assert "already installed" in result["reason"]

    def test_force_overwrite(self, tmp_path):
        """Overwrites with --force.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> from jacked.guardrails import install_hook
        >>> install_hook(d, language='python')['installed']
        True
        >>> install_hook(d, language='node', force=True)['installed']
        True
        """
        self._make_git_repo(tmp_path)
        guardrails.install_hook(tmp_path, language="python")
        result = guardrails.install_hook(tmp_path, language="node", force=True)
        assert result["installed"] is True
        content = (tmp_path / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
        assert "eslint" in content

    def test_detects_husky(self, tmp_path):
        """Detects husky framework and refuses without --force.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> os.makedirs(os.path.join(d, '.husky'))
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d, language='python')
        >>> 'husky' in result.get('reason', '')
        True
        """
        self._make_git_repo(tmp_path)
        (tmp_path / ".husky").mkdir()
        result = guardrails.install_hook(tmp_path, language="python")
        assert result["installed"] is False
        assert ".husky" in result["reason"]

    def test_auto_detects_language(self, tmp_path):
        """Auto-detects language for hook installation.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> open(os.path.join(d, 'go.mod'), 'w').close()
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d)
        >>> result['language']
        'go'
        """
        self._make_git_repo(tmp_path)
        (tmp_path / "go.mod").touch()
        result = guardrails.install_hook(tmp_path)
        assert result["installed"] is True
        assert result["language"] == "go"

    def test_unknown_language_fails(self, tmp_path):
        """Fails when language can't be detected and not specified.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> from jacked.guardrails import install_hook
        >>> result = install_hook(d)
        >>> result['installed']
        False
        """
        self._make_git_repo(tmp_path)
        result = guardrails.install_hook(tmp_path)
        assert result["installed"] is False
        assert "could not detect" in result["reason"]


class TestCheckProjectSetup:
    """Tests for check_project_setup() — checks guardrails + hook status."""

    def test_empty_project(self, tmp_path):
        """Empty project has nothing configured.

        >>> import tempfile
        >>> from jacked.guardrails import check_project_setup
        >>> result = check_project_setup(tempfile.mkdtemp())
        >>> result['has_guardrails']
        False
        """
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is False
        assert result["has_lint_hook"] is False
        assert result["detected_language"] is None

    def test_with_jacked_guardrails(self, tmp_path):
        """Detects JACKED_GUARDRAILS.md (canonical name).

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'JACKED_GUARDRAILS.md'), 'w').close()
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['has_guardrails']
        True
        """
        (tmp_path / "JACKED_GUARDRAILS.md").write_text("# Guardrails\n")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is True
        assert result["guardrails_file"] == "JACKED_GUARDRAILS.md"

    def test_fallback_design_guardrails(self, tmp_path):
        """Old DESIGN_GUARDRAILS.md still detected as having guardrails.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'DESIGN_GUARDRAILS.md'), 'w').close()
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['guardrails_file']
        'DESIGN_GUARDRAILS.md'
        """
        (tmp_path / "DESIGN_GUARDRAILS.md").write_text("old style guardrails")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is True
        assert result["guardrails_file"] == "DESIGN_GUARDRAILS.md"

    def test_jacked_guardrails_preferred(self, tmp_path):
        """Both exist — JACKED_GUARDRAILS.md wins (first in list).

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'JACKED_GUARDRAILS.md'), 'w').close()
        >>> open(os.path.join(d, 'DESIGN_GUARDRAILS.md'), 'w').close()
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['guardrails_file']
        'JACKED_GUARDRAILS.md'
        """
        (tmp_path / "JACKED_GUARDRAILS.md").write_text("new")
        (tmp_path / "DESIGN_GUARDRAILS.md").write_text("old")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is True
        assert result["guardrails_file"] == "JACKED_GUARDRAILS.md"

    def test_empty_guardrails_file(self, tmp_path):
        """Empty (0-byte) guardrails file still counts.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'JACKED_GUARDRAILS.md'), 'w').close()
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['has_guardrails']
        True
        """
        (tmp_path / "JACKED_GUARDRAILS.md").touch()
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is True
        assert result["guardrails_file"] == "JACKED_GUARDRAILS.md"

    def test_with_jacked_hook(self, tmp_path):
        """Detects jacked lint hook by marker.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> with open(os.path.join(d, '.git', 'hooks', 'pre-push'), 'w') as f:
        ...     _ = f.write('#!/bin/sh\\n# jacked-lint-hook\\nruff check .')
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['has_lint_hook']
        True
        """
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        (git_hooks / "pre-push").write_text("#!/bin/sh\n# jacked-lint-hook\nruff check .\n")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_lint_hook"] is True

    def test_non_jacked_hook_not_detected(self, tmp_path):
        """Non-jacked pre-push hook doesn't count.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> with open(os.path.join(d, '.git', 'hooks', 'pre-push'), 'w') as f:
        ...     _ = f.write('#!/bin/sh\\necho custom hook')
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['has_lint_hook']
        False
        """
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        (git_hooks / "pre-push").write_text("#!/bin/sh\necho custom hook\n")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_lint_hook"] is False

    def test_detects_language(self, tmp_path):
        """Includes detected language in result.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'package.json'), 'w').close()
        >>> from jacked.guardrails import check_project_setup
        >>> check_project_setup(d)['detected_language']
        'node'
        """
        (tmp_path / "package.json").touch()
        result = guardrails.check_project_setup(tmp_path)
        assert result["detected_language"] == "node"

    def test_nonexistent_path(self):
        """Returns safe defaults for nonexistent path.

        >>> from jacked.guardrails import check_project_setup
        >>> result = check_project_setup('/nonexistent')
        >>> result['has_guardrails']
        False
        """
        result = guardrails.check_project_setup("/nonexistent/path")
        assert result["has_guardrails"] is False
        assert result["has_lint_hook"] is False
        assert result["detected_language"] is None

    def test_fully_configured(self, tmp_path):
        """Fully configured project returns all True.

        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> open(os.path.join(d, 'JACKED_GUARDRAILS.md'), 'w').close()
        >>> open(os.path.join(d, 'pyproject.toml'), 'w').close()
        >>> os.makedirs(os.path.join(d, '.git', 'hooks'))
        >>> with open(os.path.join(d, '.git', 'hooks', 'pre-push'), 'w') as f:
        ...     _ = f.write('# jacked-lint-hook')
        >>> from jacked.guardrails import check_project_setup
        >>> r = check_project_setup(d)
        >>> r['has_guardrails'] and r['has_lint_hook']
        True
        """
        (tmp_path / "JACKED_GUARDRAILS.md").write_text("# Guardrails\n")
        (tmp_path / "pyproject.toml").touch()
        git_hooks = tmp_path / ".git" / "hooks"
        git_hooks.mkdir(parents=True)
        (git_hooks / "pre-push").write_text("#!/bin/sh\n# jacked-lint-hook\nruff check .\n")
        result = guardrails.check_project_setup(tmp_path)
        assert result["has_guardrails"] is True
        assert result["guardrails_file"] == "JACKED_GUARDRAILS.md"
        assert result["has_lint_hook"] is True
        assert result["detected_language"] == "python"


class TestDetectHookFramework:
    """Tests for _detect_hook_framework() — detects existing hook managers."""

    def test_no_framework(self, tmp_path):
        """No hook framework detected in clean repo.

        >>> import tempfile
        >>> from jacked.guardrails import _detect_hook_framework
        >>> from pathlib import Path
        >>> _detect_hook_framework(Path(tempfile.mkdtemp())) is None
        True
        """
        assert guardrails._detect_hook_framework(tmp_path) is None

    def test_detects_husky(self, tmp_path):
        """Detects .husky directory.

        >>> import tempfile, os
        >>> from pathlib import Path
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.husky'))
        >>> from jacked.guardrails import _detect_hook_framework
        >>> _detect_hook_framework(Path(d))
        '.husky'
        """
        (tmp_path / ".husky").mkdir()
        assert guardrails._detect_hook_framework(tmp_path) == ".husky"

    def test_detects_pre_commit(self, tmp_path):
        """Detects .pre-commit-config.yaml.

        >>> import tempfile
        >>> from pathlib import Path
        >>> d = tempfile.mkdtemp()
        >>> open(d + '/.pre-commit-config.yaml', 'w').close()
        >>> from jacked.guardrails import _detect_hook_framework
        >>> _detect_hook_framework(Path(d))
        '.pre-commit-config.yaml'
        """
        (tmp_path / ".pre-commit-config.yaml").touch()
        assert guardrails._detect_hook_framework(tmp_path) == ".pre-commit-config.yaml"

    def test_detects_core_hookspath(self, tmp_path):
        """Detects core.hooksPath in git config.

        >>> import tempfile, os
        >>> from pathlib import Path
        >>> d = tempfile.mkdtemp()
        >>> os.makedirs(os.path.join(d, '.git'))
        >>> with open(os.path.join(d, '.git', 'config'), 'w') as f:
        ...     _ = f.write('[core]\\n\\thooksPath = .githooks')
        >>> from jacked.guardrails import _detect_hook_framework
        >>> _detect_hook_framework(Path(d))
        'core.hooksPath'
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n\thooksPath = .githooks\n")
        assert guardrails._detect_hook_framework(tmp_path) == "core.hooksPath"


class TestTemplateContent:
    """Tests that bundled templates exist and have expected content."""

    def test_base_template_exists(self):
        """Base guardrails template exists in package data.

        >>> from jacked.guardrails import GUARDRAILS_TEMPLATES
        >>> (GUARDRAILS_TEMPLATES / 'base.md').exists()
        True
        """
        assert (guardrails.GUARDRAILS_TEMPLATES / "base.md").exists()

    def test_all_language_templates_exist(self):
        """All 4 language templates exist.

        >>> from jacked.guardrails import GUARDRAILS_TEMPLATES
        >>> all((GUARDRAILS_TEMPLATES / f'{l}.md').exists() for l in ['python', 'node', 'rust', 'go'])
        True
        """
        for lang in ["python", "node", "rust", "go"]:
            assert (guardrails.GUARDRAILS_TEMPLATES / f"{lang}.md").exists()

    def test_all_hook_templates_exist(self):
        """All 4 hook templates exist.

        >>> from jacked.guardrails import HOOK_TEMPLATES
        >>> all((HOOK_TEMPLATES / f'pre-push-{l}.sh').exists() for l in ['python', 'node', 'rust', 'go'])
        True
        """
        for lang in ["python", "node", "rust", "go"]:
            assert (guardrails.HOOK_TEMPLATES / f"pre-push-{lang}.sh").exists()

    def test_hook_templates_have_marker(self):
        """All hook templates contain the jacked marker.

        >>> from jacked.guardrails import HOOK_TEMPLATES, HOOK_MARKER
        >>> all(HOOK_MARKER in (HOOK_TEMPLATES / f'pre-push-{l}.sh').read_text() for l in ['python', 'node', 'rust', 'go'])
        True
        """
        for lang in ["python", "node", "rust", "go"]:
            content = (guardrails.HOOK_TEMPLATES / f"pre-push-{lang}.sh").read_text(encoding="utf-8")
            assert guardrails.HOOK_MARKER in content

    def test_hook_templates_have_shebang(self):
        """All hook templates start with #!/bin/sh.

        >>> from jacked.guardrails import HOOK_TEMPLATES
        >>> all((HOOK_TEMPLATES / f'pre-push-{l}.sh').read_text().startswith('#!/bin/sh') for l in ['python', 'node', 'rust', 'go'])
        True
        """
        for lang in ["python", "node", "rust", "go"]:
            content = (guardrails.HOOK_TEMPLATES / f"pre-push-{lang}.sh").read_text(encoding="utf-8")
            assert content.startswith("#!/bin/sh")

    def test_base_template_has_quality_gates(self):
        """Base template includes /dc and linter enforcement rules.

        >>> from jacked.guardrails import GUARDRAILS_TEMPLATES
        >>> content = (GUARDRAILS_TEMPLATES / 'base.md').read_text()
        >>> '/dc' in content and 'linter' in content
        True
        """
        content = (guardrails.GUARDRAILS_TEMPLATES / "base.md").read_text(encoding="utf-8")
        assert "/dc" in content
        assert "linter" in content
