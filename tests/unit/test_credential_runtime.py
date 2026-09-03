from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jacked.credentials.models import ExecutableIdentity, SwitchContext, SwitchOutcome
from jacked.credentials.runtime import (
    SHIPPED_REGISTRY,
    activate_account,
    detect_claude_identity,
)


SHIPPED_DIGEST = "884baa38fe1a624be25c4a91568bf5a08b5cf4e7d7acf29b7760e3525d964898"


def _identity(**changes) -> ExecutableIdentity:
    values = {
        "resolved_path": "/different/install/location/claude",
        "sha256": SHIPPED_DIGEST,
        "build_version": "2.1.259",
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
        "cc_access_token": "secret",
        "cc_refresh_token": "refresh",
        "cc_expires_at": 2_000_000_000,
    }


def test_shipped_registry_matches_exact_artifact_at_any_resolved_path() -> None:
    resolution = SHIPPED_REGISTRY.resolve(_identity())

    assert resolution.can_mutate is True
    assert resolution.capability.executable.resolved_path.endswith("/claude")


def test_shipped_registry_rejects_unknown_digest_or_build() -> None:
    assert SHIPPED_REGISTRY.resolve(_identity(sha256="0" * 64)).can_mutate is False
    assert SHIPPED_REGISTRY.resolve(_identity(build_version="2.1.260")).can_mutate is False


def test_shipped_registry_rejects_wrong_platform_or_architecture() -> None:
    assert SHIPPED_REGISTRY.resolve(_identity(platform_system="linux")).can_mutate is False
    assert SHIPPED_REGISTRY.resolve(_identity(platform_machine="x86_64")).can_mutate is False


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
        return_value=_identity(sha256="0" * 64),
    ):
        result = activate_account(
            object(), _account(), SwitchContext.MANUAL, "operation-unknown"
        )

    assert result.outcome is SwitchOutcome.UNSUPPORTED
    assert result.committed_authority.account_id is None
    assert result.storage.state == "unchanged"


def test_activation_fails_closed_when_platform_adapter_is_unavailable() -> None:
    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(),
        ),
        mock.patch("jacked.credentials.runtime.sys.platform", "linux"),
    ):
        result = activate_account(
            object(), _account(), SwitchContext.MANUAL, "operation-linux"
        )

    assert result.outcome is SwitchOutcome.UNSUPPORTED
    assert "platform adapter" in result.message


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
