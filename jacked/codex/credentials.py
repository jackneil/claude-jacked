"""Read & identify OpenAI Codex CLI credentials.

Codex's active credential lives in ``$CODEX_HOME/auth.json`` (plaintext, mode
0600) when ``cli_auth_credentials_store = "file"``. Under ``"keyring"`` — or
``"auto"`` when no auth.json exists — the real credential is in the OS keychain
(service ``"Codex Auth"``) and CANNOT be managed by swapping a file, so jacked
must say so plainly rather than silently find nothing.

Account identity (email, ChatGPT plan, account/workspace id) is carried in the
``tokens.id_token`` JWT; ``account_id`` is normalized to the same ``""`` org
sentinel jacked uses for Claude personal/legacy accounts.

Security: this module READS credentials for identification only. It never logs
or returns raw token values to callers — only identity + a swappable verdict.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

_AUTH_NS = "https://api.openai.com/auth"
_PROFILE_NS = "https://api.openai.com/profile"

# Valid values of the config key, lowest-friction default first.
_STORE_MODES = ("file", "keyring", "auto")
_DEFAULT_STORE_MODE = "auto"

_STORE_KEY_RE = re.compile(
    r'^\s*cli_auth_credentials_store\s*=\s*["\']?(file|keyring|auto)["\']?'
)

# A real TOML table / array-of-tables header line, e.g. [a] or [[a.b]] — used to
# place the key at top level. Deliberately strict (whole line is the header) so a
# nested-array continuation like `  [1, 2],` is NOT mistaken for a table header.
_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?[^\[\]]+\]\]?\s*(#.*)?$")


def codex_home(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve CODEX_HOME (``$CODEX_HOME`` or ``~/.codex``)."""
    env = env if env is not None else os.environ
    raw = env.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def credential_store_mode(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Return the credential store mode from ``config.toml``.

    One of ``"file" | "keyring" | "auto"`` (default ``"auto"`` when the key is
    absent). Prefers ``tomllib`` for correctness; falls back to a defensive
    line scan if the TOML is unparseable (Codex itself would also choke, but we
    degrade to the safe default rather than raising).
    """
    home = home or codex_home(env)
    cfg = home / "config.toml"
    if not cfg.exists():
        return _DEFAULT_STORE_MODE
    try:
        text = cfg.read_text(errors="replace")
    except OSError:
        return _DEFAULT_STORE_MODE

    try:
        import tomllib

        data = tomllib.loads(text)
        val = data.get("cli_auth_credentials_store")
        if isinstance(val, str) and val in _STORE_MODES:
            return val
        if val is None:
            return _DEFAULT_STORE_MODE
    except Exception:
        # Malformed TOML — fall through to the line scan.
        pass

    for line in text.splitlines():
        m = _STORE_KEY_RE.match(line)
        if m:
            return m.group(1)
    return _DEFAULT_STORE_MODE


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write ``content`` to ``path`` atomically, preserving the existing mode."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        # Windows-safe replace (retries if the config file is held open) — reuse
        # the platform util the Claude credential path already ships.
        from jacked.api.credential_helpers import _safe_replace

        _safe_replace(tmp, str(path))
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def ensure_file_storage(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> bool:
    """Force ``cli_auth_credentials_store = "file"`` in ``config.toml``.

    This is the precondition for jacked to manage a Codex account by swapping
    ``auth.json``. Idempotent: returns ``True`` if it changed the file, ``False``
    if storage was already ``"file"``. Preserves the rest of ``config.toml`` and
    keeps the key at top level (inserted before the first ``[table]`` header).
    """
    home = home or codex_home(env)
    if credential_store_mode(home, env) == "file":
        return False
    cfg = home / "config.toml"
    existing = ""
    if cfg.exists():
        try:
            existing = cfg.read_text(errors="replace")
        except OSError:
            existing = ""
    # Drop any existing top-level key occurrences (commented lines are kept).
    kept = [ln for ln in existing.splitlines() if not _STORE_KEY_RE.match(ln)]
    insert_at = next(
        (i for i, ln in enumerate(kept) if _TABLE_HEADER_RE.match(ln)), len(kept)
    )
    kept.insert(insert_at, 'cli_auth_credentials_store = "file"')
    content = "\n".join(kept).rstrip("\n") + "\n"
    _atomic_write(cfg, content)
    return True


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload segment (no signature verification).

    This is a local identity read of a credential the user already holds — we
    only need the claims, not authenticity. Raises ``ValueError`` if the input
    is not a three-segment JWT or the payload is not valid base64url JSON.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT (expected 3 dot-separated segments)")
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)  # restore base64 padding
    try:
        raw = base64.urlsafe_b64decode(seg)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"JWT payload is not valid base64url: {exc}") from exc
    try:
        claims = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JWT payload is not valid JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not a JSON object")
    return claims


def read_auth_json(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[dict]:
    """Parse ``$CODEX_HOME/auth.json``; ``None`` if missing or unreadable."""
    home = home or codex_home(env)
    p = home / "auth.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        logger.debug("codex auth.json present but unreadable at %s", p, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


@dataclass
class CodexIdentity:
    """Who a Codex account is — derived from auth.json + the id_token JWT."""

    email: Optional[str]
    plan: Optional[str]
    account_id: str  # org/workspace sentinel: "" when absent (mirrors Claude)
    user_id: Optional[str]
    auth_mode: Optional[str]
    expires_at: Optional[int]


def extract_identity(auth: Mapping) -> CodexIdentity:
    """Extract identity from a parsed ``auth.json`` dict.

    ChatGPT mode: identity comes from the ``tokens.id_token`` JWT. API-key mode
    (no id_token): email/plan are unavailable, so they are ``None``.
    """
    auth_mode = auth.get("auth_mode")
    tokens = auth.get("tokens") or {}
    id_token = tokens.get("id_token")

    email: Optional[str] = None
    plan: Optional[str] = None
    user_id: Optional[str] = None
    expires_at: Optional[int] = None
    account_id = tokens.get("account_id") or ""

    if id_token:
        try:
            claims = decode_jwt_claims(id_token)
        except ValueError:
            logger.debug("codex id_token failed to decode", exc_info=True)
            claims = {}
        ns = claims.get(_AUTH_NS) or {}
        profile = claims.get(_PROFILE_NS) or {}
        email = claims.get("email") or profile.get("email")
        plan = ns.get("chatgpt_plan_type")
        user_id = ns.get("chatgpt_user_id")
        # Prefer the JWT's account id; fall back to the tokens block.
        account_id = ns.get("chatgpt_account_id") or account_id or ""
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            expires_at = int(exp)

    return CodexIdentity(
        email=email,
        plan=plan,
        account_id=account_id or "",
        user_id=user_id,
        auth_mode=auth_mode,
        expires_at=expires_at,
    )


@dataclass
class CodexCredentialStatus:
    """Result of probing a CODEX_HOME for a manageable Codex account."""

    present: bool  # auth.json exists, parsed, and carries a credential
    store_mode: str  # file | keyring | auto
    swappable: bool  # can jacked manage this account by swapping auth.json?
    reason: Optional[str]  # why not (set whenever swappable is False)
    identity: Optional[CodexIdentity]
    home: Path


def _has_credential(auth: Optional[dict]) -> bool:
    if not auth:
        return False
    return bool(auth.get("tokens") or auth.get("OPENAI_API_KEY"))


def detect_codex_account(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> CodexCredentialStatus:
    """Probe CODEX_HOME and report whether jacked can manage the account.

    Always returns a status object — never ``None`` and never silently empty.
    When the credential is in the keyring, or there is no readable auth.json,
    ``swappable`` is ``False`` and ``reason`` explains the next step.
    """
    home = home or codex_home(env)
    mode = credential_store_mode(home, env)
    auth = read_auth_json(home, env)
    present = _has_credential(auth)
    identity = extract_identity(auth) if present else None

    if mode == "keyring":
        return CodexCredentialStatus(
            present=present,
            store_mode=mode,
            swappable=False,
            reason=(
                "Codex credentials are stored in the OS keyring (service "
                "'Codex Auth'); set cli_auth_credentials_store = \"file\" in "
                "~/.codex/config.toml so jacked can manage them"
            ),
            identity=identity,
            home=home,
        )

    if not present:
        if mode == "file":
            reason = "no ~/.codex/auth.json — run `codex login` first"
        else:  # auto, no file -> creds may be in the keyring
            reason = (
                "no readable ~/.codex/auth.json (under 'auto' the credential "
                "may be in the OS keyring) — run `codex login`, or set "
                'cli_auth_credentials_store = "file"'
            )
        return CodexCredentialStatus(
            present=False,
            store_mode=mode,
            swappable=False,
            reason=reason,
            identity=None,
            home=home,
        )

    # file (or auto with a real auth.json) and a credential is present.
    return CodexCredentialStatus(
        present=True,
        store_mode=mode,
        swappable=True,
        reason=None,
        identity=identity,
        home=home,
    )
