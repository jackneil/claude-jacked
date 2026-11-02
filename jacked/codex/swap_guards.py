"""Refusal rules for the Codex account swap (``switching.swap_codex_account``).

Each rule only reads. The swap runs every rule before its first write, so a
refused swap leaves ``config.toml`` and every ``auth.json`` unchanged. A
refusal raises ``CodexSwapError``: ``str(exc)`` is the message for the user,
and ``exc.reason`` is a short code for the log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .credentials import detect_codex_account, extract_identity


class CodexSwapError(Exception):
    """A Codex account swap was refused or failed (guardrail tripped).

    ``str(exc)`` is the message for the user. ``reason`` is a short code for
    the log, and it is values-free: never a token, an email or a timestamp.

    >>> exc = CodexSwapError("stored Codex auth.json has no credential", reason="slot_no_credential")
    >>> str(exc), exc.reason
    ('stored Codex auth.json has no credential', 'slot_no_credential')
    """

    def __init__(self, message: str, *, reason: str = "refused") -> None:
        super().__init__(message)
        self.reason = reason


_KEYRING_REFUSAL = (
    "Codex keeps its credentials in the OS keyring; jacked can switch only "
    'file-stored credentials. Set cli_auth_credentials_store = "file" in '
    "config.toml and sign in again."
)
_AUTO_REFUSAL = (
    'Codex has no file-stored credentials to switch (store mode "auto" with no '
    'usable auth.json). Set cli_auth_credentials_store = "file" in config.toml '
    "and sign in again."
)
UNREADABLE_ROOT_REFUSAL = (
    "Codex auth.json is not readable, so jacked cannot save its tokens before "
    "the switch. Wait a moment and try again. If the problem continues, run "
    "`codex login`."
)
_OLDER_REFUSAL = (
    "refusing to replay a stale Codex snapshot: the live credential "
    "for this account is newer; replaying the old one would invalidate "
    "the rotated refresh token (run `codex login` to re-capture)"
)
_NOT_NEWER_REFUSAL = (
    "refusing to replay a Codex snapshot that is not provably newer than the "
    "live credential for this account. Its refresh token is different, and "
    "the replay can replace the live token with one that no longer works. "
    "Run `codex login` to capture the account again."
)


def refuse_unswitchable_store(base: Path, env: Optional[Mapping[str, str]]) -> None:
    """Refuse a swap when Codex can keep the live credential outside auth.json.

    jacked switches an account by replacing ``auth.json``. Under "keyring", and
    under "auto" with no credential in ``auth.json``, the live credential can
    be in the OS keyring. To force "file" storage then changes the store that
    the user chose, so the swap stops before it writes anything.
    """
    status = detect_codex_account(base, env)
    if status.store_mode == "keyring":
        raise CodexSwapError(_KEYRING_REFUSAL, reason="store_mode=keyring")
    if status.store_mode == "auto" and not status.present:
        raise CodexSwapError(
            _AUTO_REFUSAL, reason="store_mode=auto auth_json=no_credential"
        )


def validate_field_complete(data: Optional[dict]) -> None:
    """A slot we're about to make active must be a complete auth.json.

    Writing one missing ``auth_mode`` (or with no credential) breaks Codex and
    browser-use auth (cockpit-tools#666).
    """
    if not data:
        raise CodexSwapError(
            "stored Codex credentials are missing or unreadable",
            reason="slot_unreadable",
        )
    if not data.get("auth_mode"):
        raise CodexSwapError(
            "stored Codex auth.json is missing 'auth_mode' (would break Codex)",
            reason="slot_no_auth_mode",
        )
    if not (data.get("tokens") or data.get("OPENAI_API_KEY")):
        raise CodexSwapError(
            "stored Codex auth.json has no credential", reason="slot_no_credential"
        )


def parse_refresh(ts: Optional[str]) -> Optional[datetime]:
    """Parse ``last_refresh``. A time with no zone is UTC. None when unusable.

    >>> parse_refresh("2026-06-28T12:00:00.651297123Z").isoformat()
    '2026-06-28T12:00:00.651297+00:00'
    >>> parse_refresh("2026-06-28T12:00:00") == parse_refresh("2026-06-28T12:00:00Z")
    True
    >>> parse_refresh("not a time") is None, parse_refresh(None) is None
    (True, True)
    """
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    # Codex writes UTC. A naive time must compare, not raise a TypeError.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def same_codex_identity(a, b) -> bool:
    """Same account across two auth blobs — by account_id when present, else email."""
    ia, ib = extract_identity(a), extract_identity(b)
    if ia.account_id and ib.account_id:
        return ia.account_id == ib.account_id
    if ia.email and ib.email:
        return ia.email.lower() == ib.email.lower()
    return False


def _refresh_token(auth: Mapping) -> Optional[str]:
    tokens = auth.get("tokens")
    return tokens.get("refresh_token") if isinstance(tokens, dict) else None


def _unproven_age(target: Optional[datetime], live: Optional[datetime]) -> str:
    """Values-free word for two refresh times that prove no order.

    >>> _unproven_age(None, None), _unproven_age(None, 1), _unproven_age(1, None)
    ('both_missing', 'target_missing', 'live_missing')
    >>> _unproven_age(1, 1)
    'equal'
    """
    if target is None:
        return "both_missing" if live is None else "target_missing"
    return "live_missing" if live is None else "equal"


def refuse_stale_replay(target: dict, live: Optional[dict]) -> None:
    """Refuse writing ``target`` over ``live`` when that can lose the rotated
    refresh token of the SAME account (Codex rejects a used one).

    "Same account" is matched by account_id when present, else by email, so
    personal accounts (account_id == "") are still protected. API-key creds
    (no email/id_token, no token rotation) have no stale-replay hazard.

    A strictly older snapshot is refused. A snapshot with a different refresh
    token is refused too, unless its ``last_refresh`` is strictly newer: an
    equal, a missing or an unusable time does not prove which token Codex
    rotated last. The same refresh token at an equal or a newer time is not a
    replay, because the live token stays valid."""
    if not live or not same_codex_identity(target, live):
        return  # nothing live, or a different account: a normal switch
    t_ref = parse_refresh(target.get("last_refresh"))
    l_ref = parse_refresh(live.get("last_refresh"))
    if t_ref is not None and l_ref is not None and t_ref < l_ref:
        raise CodexSwapError(_OLDER_REFUSAL, reason="snapshot_older")
    if _refresh_token(target) == _refresh_token(live):
        return
    if t_ref is None or l_ref is None or t_ref == l_ref:
        raise CodexSwapError(
            _NOT_NEWER_REFUSAL,
            reason="snapshot_not_provably_newer last_refresh="
            + _unproven_age(t_ref, l_ref),
        )
