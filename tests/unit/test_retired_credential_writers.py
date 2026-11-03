"""Tripwire: the retired credential writers never come back.

Account switches go through ``jacked.credentials.runtime.activate_account``
(the transaction engine) and the ``~/.claude.json`` identity is written only
by ``jacked.api.claude_identity``. A second write path beside them is how
split-store bugs happened before, so no jacked module may reference one of
the retired names again.
"""

from pathlib import Path

# The credential writers that the transaction engine (activate_account) and
# claude_identity.write_identity replaced. No jacked module may use them again.
_RETIRED_CREDENTIAL_WRITERS = (
    "sync_credential_to_all_stores",
    "acquire_claude_lock",
    "update_claude_config_email",
    "_update_claude_config_account",
    "_legacy_identity_block",
    "_ClaudeConfigAccount",
    "_RATE_LIMIT_TIER_UNSET",
    "_write_credential_file",
    "write_platform_credentials",
)


def test_no_jacked_module_references_the_retired_credential_writers() -> None:
    """A second credential write path beside the engine cannot come back."""
    from jacked.api import credential_helpers

    package = Path(__file__).resolve().parents[2] / "jacked"
    sources = sorted(package.rglob("*.py"))
    assert package / "api" / "credential_helpers.py" in sources

    offenders = [
        f"{path.relative_to(package.parent).as_posix()}: {name}"
        for path in sources
        for name in _RETIRED_CREDENTIAL_WRITERS
        if name in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
    for name in _RETIRED_CREDENTIAL_WRITERS:
        assert not hasattr(credential_helpers, name)
