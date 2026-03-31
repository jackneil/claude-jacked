# Credential Compatibility Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix jacked's keychain and OAuth handling to match Claude Code's actual implementation — correct keychain account name, hex encoding, proper scopes, and authorize URL.

**Architecture:** The Claude Code source reveals jacked has been writing to a different keychain entry (`-a "Claude Code"`) than CC reads from (`-a "$USER"`). CC also uses hex encoding (`-X` flag) for keychain writes and requests broader OAuth scopes. These mismatches mean jacked's keychain writes are invisible to CC. Fixing these makes credential switching work properly through the keychain (the primary store on macOS) rather than relying solely on the `.credentials.json` fallback.

**Tech Stack:** Python 3.12+ (subprocess for `security` CLI), macOS Keychain

### Reference

See `/Users/jack.neil/Github/claude-code/docs/auth-architecture.md` for the full CC auth architecture. Key source files:
- CC keychain: `claude-code/src/utils/secureStorage/macOsKeychainStorage.ts`
- CC service names: `claude-code/src/utils/secureStorage/macOsKeychainHelpers.ts`
- CC scopes: `claude-code/src/constants/oauth.ts`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/credential_helpers.py` | Keychain read/write | Fix account name to `$USER`, add hex encoding, update read to match |
| `jacked/web/oauth.py` | OAuth flow | Update scopes, authorize URL |
| `tests/unit/test_credential_sync.py` | Credential tests | Update tests for new keychain format |

---

### Task 1: Fix keychain account name and encoding format

**Files:**
- Modify: `jacked/api/credential_helpers.py:141-231`
- Modify: `tests/unit/test_credential_sync.py`

CC uses `process.env.USER` (the system username) as the `-a` account parameter and hex-encodes the JSON via `-X` flag. Jacked uses `-a "Claude Code"` and plaintext `-w`. This means jacked writes to a completely separate keychain entry that CC never reads.

- [ ] **Step 1: Write failing tests for the new keychain format**

Replace the existing keychain tests in `tests/unit/test_credential_sync.py` and add new ones. Find the `test_read_platform_credentials_macos` test (around line 282) and the `test_write_platform_credentials_macos` test (around line 363). Replace ALL keychain tests (from `test_read_platform_credentials_macos` through `test_write_platform_credentials_keychain_error`) with:

```python
# ------------------------------------------------------------------
# read_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_read_platform_credentials_uses_user_account():
    """Reads keychain with -a $USER to match Claude Code's entry.

    >>> test_read_platform_credentials_uses_user_account()
    """
    keychain_json = json.dumps({
        "claudeAiOauth": {
            "accessToken": "keychain_token",
            "refreshToken": "keychain_refresh",
        }
    })
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = keychain_json

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch("jacked.api.credential_helpers.subprocess.run", return_value=mock_result) as mock_run,
        mock.patch("jacked.api.credential_helpers.os.environ", {"USER": "testuser"}),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is not None
    assert result["claudeAiOauth"]["accessToken"] == "keychain_token"
    # Verify the command uses -a with $USER, not "Claude Code"
    call_args = mock_run.call_args[0][0]
    assert "-a" in call_args
    a_idx = call_args.index("-a")
    assert call_args[a_idx + 1] == "testuser"


def test_read_platform_credentials_linux():
    """Returns None immediately on Linux (no keychain support yet).

    >>> test_read_platform_credentials_linux()
    """
    with mock.patch("jacked.api.credential_helpers.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = read_platform_credentials()

    assert result is None


def test_read_platform_credentials_keychain_not_found():
    """Returns None when keychain entry doesn't exist.

    >>> test_read_platform_credentials_keychain_not_found()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 44
    mock_result.stdout = ""
    mock_result.stderr = "The specified item could not be found in the keychain."

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch("jacked.api.credential_helpers.subprocess.run", return_value=mock_result),
        mock.patch("jacked.api.credential_helpers.os.environ", {"USER": "testuser"}),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


# ------------------------------------------------------------------
# write_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_write_platform_credentials_uses_hex_and_user():
    """Writes keychain with -X hex encoding and -a $USER to match Claude Code.

    >>> test_write_platform_credentials_uses_hex_and_user()
    """
    cred_data = {
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "test_token"},
    }
    mock_result = mock.MagicMock()
    mock_result.returncode = 0

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_helpers.subprocess.run",
            return_value=mock_result,
        ) as mock_run,
        mock.patch("jacked.api.credential_helpers.os.environ", {"USER": "testuser"}),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials(cred_data)

    assert result is True
    # Should use -U (update) not delete-then-add
    assert mock_run.call_count == 1
    call_args = mock_run.call_args[0][0]
    assert "add-generic-password" in call_args
    assert "-U" in call_args
    # Verify -a uses $USER
    a_idx = call_args.index("-a")
    assert call_args[a_idx + 1] == "testuser"
    # Verify -X is used (hex encoding) not -w
    assert "-X" in call_args
    assert "-w" not in call_args
    # Verify the hex value decodes to the original JSON
    x_idx = call_args.index("-X")
    hex_value = call_args[x_idx + 1]
    decoded = bytes.fromhex(hex_value).decode("utf-8")
    decoded_data = json.loads(decoded)
    assert decoded_data["_jackedAccountId"] == 1


def test_write_platform_credentials_linux_noop():
    """Returns True (no-op) on Linux.

    >>> test_write_platform_credentials_linux_noop()
    """
    with mock.patch("jacked.api.credential_helpers.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is True


def test_write_platform_credentials_keychain_error():
    """Returns False when keychain add command fails.

    >>> test_write_platform_credentials_keychain_error()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "errSecAuthFailed"

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_helpers.subprocess.run",
            return_value=mock_result,
        ),
        mock.patch("jacked.api.credential_helpers.os.environ", {"USER": "testuser"}),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -k "platform_credentials" -v`
Expected: FAIL — old format doesn't use `-a $USER` or `-X` hex

- [ ] **Step 3: Implement the keychain fixes**

In `jacked/api/credential_helpers.py`, add a helper to get the system username (after the imports, around line 16):

```python
def _get_keychain_username() -> str:
    """Get the system username for keychain account name.

    Claude Code uses process.env.USER || userInfo().username as the
    keychain account name (-a parameter). We must match this exactly
    or we write to a different keychain entry.
    """
    return os.environ.get("USER") or os.environ.get("USERNAME") or "Claude Code"
```

Replace `read_platform_credentials()` (lines 141-162) with:

```python
def read_platform_credentials() -> dict | None:
    """Read credentials from the platform's native credential store.

    macOS: Keychain entry with service "Claude Code-credentials" and
    account name matching the system username (same as Claude Code).
    Linux/Windows: not yet needed (still use .credentials.json)

    Returns parsed dict (same shape as .credentials.json) or None.
    """
    if sys.platform != "darwin":
        return None
    try:
        username = _get_keychain_username()
        result = subprocess.run(
            ["security", "find-generic-password",
             "-a", username,
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        if result.returncode != 0:
            logger.debug("Keychain read failed: %s", result.stderr.strip())
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
        logger.debug("Keychain read error: %s", exc)
    return None
```

Replace `write_platform_credentials()` (lines 197-231) with:

```python
def write_platform_credentials(data: dict) -> bool:
    """Write credentials to the platform's native credential store.

    macOS: Keychain entry with service "Claude Code-credentials" and
    account name matching the system username.  Uses -X hex encoding
    to match Claude Code's format (avoids plaintext in process args,
    prevents CrowdStrike/process monitor exposure).

    Linux/Windows: no-op (they use .credentials.json)

    Returns True if written successfully, False otherwise.

    >>> write_platform_credentials({}) if sys.platform != "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return True  # no-op on non-macOS (file write is sufficient)
    try:
        username = _get_keychain_username()
        json_data = json.dumps(data, separators=(",", ":"))
        hex_value = json_data.encode("utf-8").hex()

        # Use -U (update-or-insert) with -X (hex value) to match CC's format.
        # CC uses stdin for payloads ≤4032 bytes to hide from process monitors,
        # but argv with -X is also hex-safe and simpler from Python.
        result = subprocess.run(
            ["security", "add-generic-password",
             "-U",
             "-a", username,
             "-s", "Claude Code-credentials",
             "-X", hex_value],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Keychain write failed: %s", result.stderr.strip())
            return False
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Keychain write error: %s", exc)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -k "platform_credentials" -v`
Expected: 6 passed

- [ ] **Step 5: Run all credential tests for regressions**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add jacked/api/credential_helpers.py tests/unit/test_credential_sync.py
git commit -m "fix: match Claude Code's keychain format — correct account name and hex encoding

Claude Code uses -a \$USER (system username) and -X (hex encoding)
for keychain operations. Jacked was using -a 'Claude Code' and -w
(plaintext), writing to a completely separate keychain entry that CC
never reads. Now uses the same account name and encoding format.

Also switches from delete-then-add to -U (update-or-insert) to match
CC's pattern and avoid brief windows with no keychain entry."
```

---

### Task 2: Clean up orphan keychain entry

**Files:**
- Modify: `jacked/api/credential_helpers.py`

The old `-a "Claude Code"` keychain entry should be cleaned up on first write so it doesn't cause confusion.

- [ ] **Step 1: Add cleanup to write_platform_credentials**

In `jacked/api/credential_helpers.py`, inside `write_platform_credentials()`, after the successful `add-generic-password` call (after `return True`), add cleanup of the old entry. Actually, do it BEFORE the main write to keep the function clean. Add this block at the start of the `try` block, before the main write:

```python
        # Clean up orphan keychain entry from old jacked versions that used
        # -a "Claude Code" instead of -a $USER.  Ignore errors (may not exist).
        if username != "Claude Code":
            subprocess.run(
                ["security", "delete-generic-password",
                 "-a", "Claude Code",
                 "-s", "Claude Code-credentials"],
                capture_output=True, timeout=5,
            )
```

- [ ] **Step 2: Verify tests still pass**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add jacked/api/credential_helpers.py
git commit -m "fix: clean up orphan keychain entry from old -a 'Claude Code' format

Old jacked versions wrote to -a 'Claude Code' which CC never reads.
Delete the orphan entry on next write so only the correct -a \$USER
entry exists."
```

---

### Task 3: Update OAuth scopes and authorize URL

**Files:**
- Modify: `jacked/web/oauth.py:40,45`

CC requests `user:mcp_servers` and `user:file_upload` scopes and uses `https://claude.com/cai/oauth/authorize` (attribution layer URL).

- [ ] **Step 1: Update constants**

In `jacked/web/oauth.py`, replace line 40:

```python
AUTH_URL = "https://claude.ai/oauth/authorize"
```

With:

```python
AUTH_URL = "https://claude.com/cai/oauth/authorize"
```

And replace line 45:

```python
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"
```

With:

```python
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
```

- [ ] **Step 2: Verify no tests break**

Run: `uv run python -m pytest tests/ --tb=short 2>&1 | tail -5`
Expected: All pass (scopes/URL are constants not directly tested)

- [ ] **Step 3: Commit**

```bash
git add jacked/web/oauth.py
git commit -m "fix: match Claude Code's OAuth scopes and authorize URL

Add user:mcp_servers and user:file_upload scopes (CC requests these).
Use https://claude.com/cai/oauth/authorize (CC's attribution URL)
instead of https://claude.ai/oauth/authorize."
```

---

### Task 4: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All pass, no regressions

- [ ] **Step 2: Manual keychain verification (macOS only)**

```bash
# Check that the old orphan entry is gone
security find-generic-password -a "Claude Code" -s "Claude Code-credentials" 2>&1
# Expected: "The specified item could not be found in the keychain."

# Check that the correct entry exists
security find-generic-password -a "$USER" -s "Claude Code-credentials" 2>&1
# Expected: shows the entry with correct service and account
```
