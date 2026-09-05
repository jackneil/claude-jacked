from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tests._platform import posix_file_modes_enforced

from jacked.credentials.file_store import FileCredentialStore
from jacked.credentials.models import (
    SessionActivationState,
    CredentialCapability,
    ExecutableIdentity,
    StoreDeclaration,
    StoreRole,
    SwitchContext,
    SwitchOutcome,
)
from jacked.credentials.runtime import (
    GLOBAL_FILE_LOCATOR,
    INSPECTED_CLAUDE_BUILD,
    KEYCHAIN_LOCATOR,
    SHIPPED_REGISTRY,
    activate_account,
    build_stores,
    detect_claude_identity,
    resolve_active_identity,
    scoped_launch_needs_global_activation,
)


def _identity(**changes) -> ExecutableIdentity:
    values = {
        "resolved_path": "/different/install/location/claude",
        "sha256": "0" * 64,
        "build_version": INSPECTED_CLAUDE_BUILD,
        "config_mode": "global",
        "platform_system": "darwin",
        "platform_machine": "arm64",
    }
    values.update(changes)
    return ExecutableIdentity(**values)


def _account() -> dict:
    return {
        "id": 7,
        "email": "seven@example.com",
        "display_name": "Seven Example",
        "organization_uuid": "org-seven",
        "organization_name": "Seven Org",
        "cc_access_token": "secret",
        "cc_refresh_token": "refresh",
        "cc_expires_at": 2_000_000_000,
    }


def test_shipped_registry_resolves_any_build_hash_on_each_platform() -> None:
    darwin = SHIPPED_REGISTRY.resolve(_identity(sha256="f" * 64))
    assert darwin.can_mutate is True
    assert darwin.capability.authority.locator == KEYCHAIN_LOCATOR
    assert [m.locator for m in darwin.capability.required_mirrors] == [GLOBAL_FILE_LOCATOR]

    for system in ("linux", "windows"):
        resolution = SHIPPED_REGISTRY.resolve(
            _identity(platform_system=system, platform_machine="x86_64")
        )
        assert resolution.can_mutate is True
        assert resolution.capability.authority.locator == GLOBAL_FILE_LOCATOR
        assert resolution.capability.required_mirrors == ()


def test_shipped_registry_flags_uninspected_newer_builds_and_rejects_old_ones() -> None:
    newer = SHIPPED_REGISTRY.resolve(_identity(build_version="2.1.999"))
    assert newer.can_mutate is True
    assert "build-newer-than-inspected" in newer.evidence

    assert SHIPPED_REGISTRY.resolve(_identity(build_version="2.0.9")).can_mutate is False
    assert SHIPPED_REGISTRY.resolve(_identity(config_mode="scoped")).can_mutate is False

    floor = SHIPPED_REGISTRY.resolve(_identity(build_version="2.1.0"))
    assert floor.can_mutate is True
    assert "build-newer-than-inspected" not in floor.evidence

    inspected = SHIPPED_REGISTRY.resolve(_identity(build_version=INSPECTED_CLAUDE_BUILD))
    assert inspected.can_mutate is True
    assert "build-newer-than-inspected" not in inspected.evidence


def test_build_stores_keys_adapters_by_declaration_locator(tmp_path: Path) -> None:
    linux = SHIPPED_REGISTRY.resolve(_identity(platform_system="linux")).capability
    stores = build_stores(linux, tmp_path)
    assert set(stores) == {GLOBAL_FILE_LOCATOR}
    assert isinstance(stores[GLOBAL_FILE_LOCATOR], FileCredentialStore)
    assert stores[GLOBAL_FILE_LOCATOR].path == tmp_path / ".claude" / ".credentials.json"

    darwin = SHIPPED_REGISTRY.resolve(_identity()).capability
    with mock.patch("jacked.credentials.runtime.MacOSCredentialStore") as keychain:
        stores = build_stores(darwin, tmp_path)
    assert set(stores) == {KEYCHAIN_LOCATOR, GLOBAL_FILE_LOCATOR}
    assert stores[KEYCHAIN_LOCATOR] is keychain.return_value


def test_build_stores_rejects_unknown_locator(tmp_path: Path) -> None:
    capability = SHIPPED_REGISTRY.resolve(_identity(platform_system="linux")).capability
    unknown = CredentialCapability(
        **{
            **capability.__dict__,
            "authority": StoreDeclaration("x", "nowhere", StoreRole.AUTHORITY),
        }
    )
    with pytest.raises(ValueError):
        build_stores(unknown, tmp_path)


def test_linux_activation_writes_file_authority_end_to_end(tmp_path: Path) -> None:
    from jacked.credentials.repository import InMemoryCredentialSwitchRepository

    home = tmp_path
    (home / ".claude").mkdir()
    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(platform_system="linux", platform_machine="x86_64"),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
        mock.patch(
            "jacked.credentials.runtime.DatabaseCredentialSwitchRepository",
            lambda _db: InMemoryCredentialSwitchRepository(),
        ),
    ):
        result = activate_account(object(), _account(), SwitchContext.MANUAL, "op-linux")

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    written = home / ".claude" / ".credentials.json"
    assert written.exists()
    if posix_file_modes_enforced():
        # Windows reports 0o666 regardless of chmod; there the file's privacy
        # is the profile directory's ACL, which this assertion cannot see.
        assert (written.stat().st_mode & 0o777) == 0o600
    assert '"_jackedAccountId":7' in written.read_text(encoding="utf-8").replace(" ", "")
    # Running Claude Code sessions follow a switch only through this identity
    # (regression 2026-09-04: the engine skipped it and no session switched).
    import json

    config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert config["oauthAccount"]["emailAddress"] == "seven@example.com"
    assert config["oauthAccount"]["displayName"] == "Seven Example"
    assert config["oauthAccount"]["organizationUuid"] == "org-seven"
    assert config["oauthAccount"]["organizationName"] == "Seven Org"
    assert result.existing_session_activation is SessionActivationState.PENDING_NEXT_ACTIVITY


def test_resolve_active_identity_on_linux_reads_credential_file(tmp_path: Path) -> None:
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(
        '{"_jackedAccountId": 4, "claudeAiOauth": {"accessToken": "a"}}', encoding="utf-8"
    )
    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(platform_system="linux", platform_machine="x86_64"),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
    ):
        observation = resolve_active_identity()

    assert observation.state.value == "resolved"
    assert observation.identity.account_id == 4
    assert f"build:{INSPECTED_CLAUDE_BUILD}" in observation.evidence


def test_resolve_active_identity_reports_a_divergent_darwin_mirror_as_evidence(tmp_path: Path) -> None:
    """Keychain says account 4, the file mirror says 5: the runtime uses the Keychain."""
    from jacked.credentials.canonical import CredentialPayload
    from jacked.credentials.store import MemoryCredentialStore

    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(
        '{"_jackedAccountId": 5, "claudeAiOauth": {"accessToken": "five"}}', encoding="utf-8"
    )
    keychain = MemoryCredentialStore(
        "keychain",
        CredentialPayload.from_mapping({"_jackedAccountId": 4, "claudeAiOauth": {"accessToken": "four"}}),
    )
    with (
        mock.patch("jacked.credentials.runtime.detect_claude_identity", return_value=_identity()),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
        mock.patch("jacked.credentials.runtime.MacOSCredentialStore", return_value=keychain),
    ):
        observation = resolve_active_identity()

    assert observation.state.value == "resolved"
    assert observation.identity.account_id == 4
    assert "required_mirror:global credential file:divergent" in observation.evidence


def test_unstamped_credential_file_is_unusable_with_named_evidence(tmp_path: Path) -> None:
    """A first-run Linux install has a Claude-written file with no jacked stamp."""
    home = tmp_path
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}', encoding="utf-8"
    )
    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(platform_system="linux", platform_machine="x86_64"),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
    ):
        observation = resolve_active_identity()

    assert observation.state.value == "unusable"
    assert "identity:stamp-absent" in observation.evidence


def test_scoped_launch_needs_global_activation_only_for_keychain_authority() -> None:
    with mock.patch(
        "jacked.credentials.runtime.detect_claude_identity",
        return_value=_identity(platform_system="linux", platform_machine="x86_64"),
    ):
        assert scoped_launch_needs_global_activation() is False
    with mock.patch(
        "jacked.credentials.runtime.detect_claude_identity", return_value=_identity()
    ):
        assert scoped_launch_needs_global_activation() is True
    with mock.patch(
        "jacked.credentials.runtime.detect_claude_identity",
        side_effect=OSError("no claude"),
    ):
        assert scoped_launch_needs_global_activation() is True  # fail closed


def test_detection_caches_identity_until_the_binary_changes(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"build-one")
    executable.chmod(0o755)
    completed = SimpleNamespace(returncode=0, stdout="2.1.260 (Claude Code)\n")
    run = mock.MagicMock(return_value=completed)

    with (
        mock.patch("jacked.credentials.runtime.find_bin", return_value=str(executable)),
        mock.patch("jacked.credentials.runtime.subprocess.run", run),
    ):
        first = detect_claude_identity(tmp_path)
        second = detect_claude_identity(tmp_path)
        executable.write_bytes(b"build-two")  # same size, new mtime
        os.utime(executable, ns=(1, 1))
        third = detect_claude_identity(tmp_path)

    assert first == second
    assert third.sha256 != first.sha256
    assert run.call_count == 2


def test_detection_turns_version_probe_timeout_into_oserror(tmp_path: Path) -> None:
    import subprocess

    executable = tmp_path / "claude"
    executable.write_bytes(b"x")
    executable.chmod(0o755)
    with (
        mock.patch("jacked.credentials.runtime.find_bin", return_value=str(executable)),
        mock.patch(
            "jacked.credentials.runtime.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["claude"], 5),
        ),
        pytest.raises(OSError),
    ):
        detect_claude_identity(tmp_path)


def test_detection_uses_known_install_locations_when_path_is_sanitized(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"certified-candidate")
    executable.chmod(0o755)
    completed = SimpleNamespace(returncode=0, stdout="2.1.259 (Claude Code)\n")

    with (
        mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False),
        mock.patch("jacked.credentials.runtime.find_bin", return_value=str(executable)),
        mock.patch("jacked.credentials.runtime.subprocess.run", return_value=completed),
    ):
        identity = detect_claude_identity(tmp_path)

    assert identity.resolved_path == str(executable.resolve())
    assert identity.build_version == "2.1.259"


def test_activation_maps_unknown_build_to_unsupported_without_mutation() -> None:
    with mock.patch(
        "jacked.credentials.runtime.detect_claude_identity",
        return_value=_identity(build_version="2.0.0"),
    ):
        result = activate_account(
            object(), _account(), SwitchContext.MANUAL, "operation-unknown"
        )

    assert result.outcome is SwitchOutcome.UNSUPPORTED
    assert result.committed_authority.account_id is None
    assert result.storage.state == "unchanged"


def test_active_identity_callers_do_not_reintroduce_file_precedence() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "jacked/api/routes/auth.py",
        "jacked/api/usage_monitor.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert 'Path.home() / ".claude" / ".credentials.json"' not in source
        assert "resolve_active_identity" in source

    helpers = (root / "jacked/api/credential_helpers.py").read_text(
        encoding="utf-8"
    )
    active_helper = helpers.split("def read_active_account_id()", 1)[1].split(
        "# ---------------------------------------------------------------------------",
        1,
    )[0]
    assert "resolve_active_identity" in active_helper
    assert "read_platform_credentials" not in active_helper
    assert '".credentials.json"' not in active_helper

    launch = (root / "jacked/launch.py").read_text(encoding="utf-8")
    resolve_default = launch.split("def resolve_account(", 1)[1].split(
        "def _sync_tokens_from_file", 1
    )[0]
    assert "read_active_account_id" in resolve_default
    assert "read_platform_credentials" not in resolve_default
    assert 'Path.home() / ".claude" / ".credentials.json"' not in resolve_default

    usage_monitor = (root / "jacked/api/usage_monitor.py").read_text(
        encoding="utf-8"
    )
    assert "_write_swap_credentials" not in usage_monitor
    assert "sync_credential_to_all_stores" not in usage_monitor


def _activate_on_linux_home(home: Path):
    """Run the real activation path, real identity publisher included."""
    from jacked.credentials.repository import InMemoryCredentialSwitchRepository

    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(platform_system="linux", platform_machine="x86_64"),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
        mock.patch(
            "jacked.credentials.runtime.DatabaseCredentialSwitchRepository",
            lambda _db: InMemoryCredentialSwitchRepository(),
        ),
    ):
        return activate_account(object(), _account(), SwitchContext.MANUAL, "op-linux")


def _assert_switch_reports_that_sessions_must_restart(result, home: Path) -> None:
    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert result.existing_session_activation is SessionActivationState.RESTART_REQUIRED
    assert "claude config identity not updated" in result.message
    # The credentials themselves still landed; only the mirror that running
    # sessions watch did not.
    authority = (home / ".claude" / ".credentials.json").read_text(encoding="utf-8")
    assert '"_jackedAccountId":7' in authority.replace(" ", "")


def test_activation_reports_restart_required_when_the_config_is_a_symlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (home / ".claude.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks require privileges on this platform")

    result = _activate_on_linux_home(home)

    _assert_switch_reports_that_sessions_must_restart(result, home)
    # A symlinked config is never written through, not even to say so.
    assert json.loads(target.read_text(encoding="utf-8")) == {}


def test_activation_reports_restart_required_when_the_config_cannot_be_written(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # A directory where the config file belongs: every write attempt fails.
    (home / ".claude.json").mkdir()

    result = _activate_on_linux_home(home)

    _assert_switch_reports_that_sessions_must_restart(result, home)
    assert (home / ".claude.json").is_dir()


# ------------------------------------------------------------------
# Cross-process switch lease
# ------------------------------------------------------------------


def test_two_leases_on_one_path_exclude_each_other(tmp_path: Path) -> None:
    from jacked.credentials.lease import FileSwitchLease

    first = FileSwitchLease(tmp_path / "locks" / "credential-switch.lock")
    second = FileSwitchLease(tmp_path / "locks" / "credential-switch.lock")

    with first.acquire() as held:
        assert held is True
        with second.acquire() as blocked:
            assert blocked is False

    with second.acquire() as free_now:
        assert free_now is True


def test_the_lease_file_and_its_directory_are_private(tmp_path: Path) -> None:
    from jacked.credentials.lease import FileSwitchLease

    path = tmp_path / "state" / "credential-switch.lock"
    with FileSwitchLease(path).acquire() as held:
        assert held is True

    assert path.exists()
    if posix_file_modes_enforced():
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_an_unusable_lease_path_refuses_the_switch(tmp_path: Path) -> None:
    """Fail closed: a lock that cannot be taken never grants the switch."""
    from jacked.credentials.lease import FileSwitchLease

    directory = tmp_path / "not-a-file"
    directory.mkdir()

    with FileSwitchLease(directory).acquire() as held:
        assert held is False


def test_a_lock_held_by_another_process_blocks_this_one(tmp_path: Path) -> None:
    import subprocess
    import sys

    lock_path = tmp_path / "credential-switch.lock"
    child_source = (
        "import sys\n"
        "from jacked.credentials.lease import FileSwitchLease\n"
        "with FileSwitchLease(sys.argv[1]).acquire() as held:\n"
        "    print('ACQUIRED' if held else 'REFUSED', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_source, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    try:
        assert child.stdout.readline().strip() == "ACQUIRED"
        from jacked.credentials.lease import FileSwitchLease

        with FileSwitchLease(lock_path).acquire() as held:
            assert held is False
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.wait(timeout=30) == 0
        with FileSwitchLease(lock_path).acquire() as held:
            assert held is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


def test_the_runtime_lease_covers_threads_and_processes(tmp_path: Path) -> None:
    from jacked.credentials.lease import FileSwitchLease
    from jacked.credentials.runtime import SWITCH_LEASE_RELATIVE_PATH, switch_lease_for

    expected = tmp_path / ".claude" / Path(*SWITCH_LEASE_RELATIVE_PATH)
    lease = switch_lease_for(tmp_path)

    with lease.acquire() as held:
        assert held is True
        # The same file blocks another process...
        with FileSwitchLease(expected).acquire() as other_process:
            assert other_process is False
        # ...and the in-process lock blocks another thread of this one, which
        # a file lock shared by the process cannot see.
        with switch_lease_for(tmp_path).acquire() as same_process:
            assert same_process is False

    assert expected.exists()
