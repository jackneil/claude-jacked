#!/usr/bin/env python3
"""Test fixture for security_gatekeeper.py.

Pipes mock hook input JSON to the gatekeeper script and checks output.
Tests local fast-path (permissions + allowlist/denylist) only — does NOT
test API/CLI tiers to keep it fast and offline.

Run: python tests/debugging/test_gatekeeper.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Dynamically resolve paths — no hardcoded user dirs
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = str(REPO_ROOT / "jacked" / "data" / "hooks" / "security_gatekeeper.py")
PYTHON = sys.executable
CWD = str(REPO_ROOT)

ALLOW_JSON = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
    }
}


def run_gatekeeper(command: str, cwd: str = CWD) -> tuple[str, float]:
    """Run gatekeeper with a mock hook input. Returns (stdout, elapsed_seconds)."""
    hook_input = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
        "session_id": "test",
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
    }
    env = {**os.environ, "JACKED_HOOK_DEBUG": "1"}
    # Strip API key so we only test local fast-path
    env.pop("ANTHROPIC_API_KEY", None)

    start = time.time()
    result = subprocess.run(
        [PYTHON, SCRIPT],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    elapsed = time.time() - start
    return result.stdout.strip(), elapsed


def test_case(name: str, command: str, expect_allow: bool, max_time: float = 1.0):
    """Run a single test case."""
    stdout, elapsed = run_gatekeeper(command)

    if expect_allow:
        try:
            output = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            output = None
        passed = output == ALLOW_JSON
        status = "ALLOW" if passed else f"FAIL (got: {stdout[:80]})"
    else:
        passed = stdout == ""
        status = "PASS (no output)" if passed else f"FAIL (got: {stdout[:80]})"

    fast = elapsed < max_time
    time_status = f"{elapsed:.3f}s" + ("" if fast else f" SLOW (>{max_time}s)")

    icon = "+" if passed else "FAIL"
    print(f"  [{icon}] {name}: {status} [{time_status}]")
    return passed


def main():
    results = []
    print("\n=== Permission-matched (from settings.json allow rules) ===")
    results.append(test_case("gh pr list --repo foo", "gh pr list --repo foo", expect_allow=True))
    results.append(test_case("cat somefile.txt", "cat somefile.txt", expect_allow=True))
    results.append(test_case("find . -name '*.py'", "find . -name '*.py'", expect_allow=True))
    results.append(test_case("grep -r TODO .", "grep -r TODO .", expect_allow=True))

    print("\n=== Local allowlist (hardcoded safe patterns) ===")
    results.append(test_case("git status", "git status", expect_allow=True))
    results.append(test_case("git log --oneline", "git log --oneline", expect_allow=True))
    results.append(test_case("echo hello", "echo hello", expect_allow=True))
    results.append(test_case("ls -la", "ls -la", expect_allow=True))
    results.append(test_case("pip list", "pip list", expect_allow=True))
    results.append(test_case("pip freeze", "pip freeze", expect_allow=True))
    results.append(test_case("pytest tests/", "pytest tests/", expect_allow=True))
    results.append(test_case("npm test", "npm test", expect_allow=True))
    results.append(test_case("ruff check .", "ruff check .", expect_allow=True))
    results.append(test_case("gh issue list", "gh issue list", expect_allow=True))
    results.append(test_case("docker ps", "docker ps", expect_allow=True))

    print("\n=== Path-stripped allowlist (full path to python/node) ===")
    results.append(test_case(
        "python.exe -c print(42)",
        f'{sys.executable} -c "print(42)"',
        expect_allow=True,
    ))
    results.append(test_case(
        "python.exe -m pytest",
        f"{sys.executable} -m pytest tests/ -v",
        expect_allow=True,
    ))
    results.append(test_case(
        "python.exe -m pip list",
        f"{sys.executable} -m pip list",
        expect_allow=True,
    ))

    print("\n=== Version/help flags (universal safe) ===")
    results.append(test_case("npm --version", "npm --version", expect_allow=True))
    results.append(test_case("node --version", "node --version", expect_allow=True))
    results.append(test_case("python --help", "python --help", expect_allow=True))

    print("\n=== Denylisted (should show dialog) ===")
    results.append(test_case("rm -rf /", "rm -rf /", expect_allow=False))
    results.append(test_case("rm -rf ~", "rm -rf ~", expect_allow=False))
    results.append(test_case("rm -rf /*", "rm -rf /*", expect_allow=False))
    results.append(test_case("sudo apt install foo", "sudo apt install foo", expect_allow=False))
    results.append(test_case("cat ~/.ssh/id_rsa", "cat ~/.ssh/id_rsa", expect_allow=False))
    results.append(test_case("dd if=/dev/zero", "dd if=/dev/zero of=/dev/sda", expect_allow=False))
    results.append(test_case("chmod 777 /etc", "chmod 777 /etc", expect_allow=False))
    results.append(test_case("schtasks /create", "schtasks /create /tn task", expect_allow=False))
    results.append(test_case("reg add HKLM", "reg add HKLM\\SOFTWARE\\foo", expect_allow=False))
    results.append(test_case(
        "powershell -EncodedCommand",
        "powershell -EncodedCommand ZWNobyAiaGFja2VkIg==",
        expect_allow=False,
    ))
    results.append(test_case(
        "base64 --decode pipe",
        "echo payload | base64 --decode | sh",
        expect_allow=False,
    ))

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{total} passed")
    if passed == total:
        print("All tests passed!")
    else:
        print(f"{total - passed} test(s) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
