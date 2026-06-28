"""Switch the active OpenAI Codex account — guardrailed.

Codex assumes a single account: the live credential is ``$CODEX_HOME/auth.json``
and Codex rotates its single-use refresh token in place. So switching is NOT a
clean clone of jacked's Claude swap. Two mechanisms, both file-based:

1. Per-account ``CODEX_HOME`` isolation — each account gets its own home dir
   (``$CODEX_HOME/accounts/<id>``) holding its auth.json + a file-store config,
   so ``jacked codex launch <id>`` runs Codex on that account without ever
   touching the shared root (no refresh-token race).
2. In-place active swap — point the shared root ``$CODEX_HOME/auth.json`` at a
   chosen account for the stock Codex CLI / IDE / app, with hard guardrails:
     - force file storage (``cli_auth_credentials_store="file"``)
     - CAPTURE the live root into the outgoing account's slot FIRST, so Codex's
       rotated tokens aren't lost (the #15502 hazard)
     - write the incoming slot field-complete (keep ``auth_mode`` +
       ``last_refresh`` — a partial auth.json breaks Codex, cockpit-tools#666)
     - REFUSE replaying a stale snapshot over a newer live one for the same
       account (would invalidate the rotated refresh token)
     - single-writer lock; a restart is required (stock Codex caches auth at
       startup)

Per-account slots double as the per-account homes: ``accounts/<id>/auth.json``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping, Optional

from .credentials import codex_home, ensure_file_storage, extract_identity

logger = logging.getLogger(__name__)


class CodexSwapError(Exception):
    """A Codex account swap was refused or failed (guardrail tripped)."""


@dataclass
class CodexSwapResult:
    ok: bool
    target_id: int
    outgoing_id: Optional[int]
    restart_required: bool
    captured_outgoing: bool


# ---------------------------------------------------------------------------
# Per-account home / slot layout
# ---------------------------------------------------------------------------

def codex_account_home(
    account_id: int, base: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """Per-account CODEX_HOME: ``$CODEX_HOME/accounts/<id>``."""
    base = base or codex_home(env)
    return base / "accounts" / str(account_id)


def codex_slot_auth_path(
    account_id: int, base: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """The account's stored auth.json (its slot == its per-account home)."""
    return codex_account_home(account_id, base, env) / "auth.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def seed_codex_slot(
    account_id: int, base: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> bool:
    """Capture the live root auth.json into ``account_id``'s slot.

    Called when an account is added/active so its slot holds the latest tokens.
    Returns ``True`` if it wrote a slot, ``False`` if there's nothing to capture.
    """
    base = base or codex_home(env)
    root = _read_json(base / "auth.json")
    if not root:
        return False
    _atomic_write_json(codex_slot_auth_path(account_id, base), root)
    return True


# ---------------------------------------------------------------------------
# Single-writer lock (mirrors the Claude proper-lockfile approach, codex-scoped)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _codex_swap_lock(base: Path, retries: int = 50) -> Iterator[bool]:
    lock_dir = base / ".jacked-codex-swap.lock"
    pid_file = lock_dir / "pid"
    acquired = False
    for _ in range(retries):
        try:
            base.mkdir(parents=True, exist_ok=True)
            os.mkdir(lock_dir)
            try:
                pid_file.write_text(str(os.getpid()))
            except OSError:
                pass
            acquired = True
            break
        except FileExistsError:
            holder = None
            try:
                holder = int(pid_file.read_text().strip())
            except (FileNotFoundError, ValueError):
                pass
            stale = holder is None
            if holder is not None:
                try:
                    os.kill(holder, 0)
                except ProcessLookupError:
                    stale = True
                except PermissionError:
                    stale = False
                except OSError:
                    stale = True
            if stale:
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            time.sleep(0.05)
    try:
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _validate_field_complete(data: Optional[dict]) -> None:
    """A slot we're about to make active must be a complete auth.json.

    Writing one missing ``auth_mode`` (or with no credential) breaks Codex and
    browser-use auth (cockpit-tools#666).
    """
    if not data:
        raise CodexSwapError("stored Codex credentials are missing or unreadable")
    if not data.get("auth_mode"):
        raise CodexSwapError("stored Codex auth.json is missing 'auth_mode' (would break Codex)")
    if not (data.get("tokens") or data.get("OPENAI_API_KEY")):
        raise CodexSwapError("stored Codex auth.json has no credential")


def _parse_refresh(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _refuse_stale_replay(target: dict, live: Optional[dict]) -> None:
    """Refuse writing ``target`` over ``live`` when it's an OLDER snapshot of the
    SAME account — Codex would reject the already-rotated refresh token."""
    if not live:
        return
    t_id = extract_identity(target).account_id
    l_id = extract_identity(live).account_id
    if not t_id or t_id != l_id:
        return  # different account → a normal switch, not a replay
    t_ref = _parse_refresh(target.get("last_refresh"))
    l_ref = _parse_refresh(live.get("last_refresh"))
    if t_ref is not None and l_ref is not None and t_ref < l_ref:
        raise CodexSwapError(
            "refusing to replay a stale Codex snapshot — the live credential "
            "for this account is newer; replaying the old one would invalidate "
            "the rotated refresh token (run `codex login` to re-capture)"
        )


# ---------------------------------------------------------------------------
# The swap
# ---------------------------------------------------------------------------

def swap_codex_account(
    db,
    target_id: int,
    base: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    lock: bool = True,
) -> CodexSwapResult:
    """Make ``target_id`` the active Codex account in the shared root.

    Captures the outgoing account's live tokens first, refuses a stale replay,
    writes the target field-complete, updates the per-provider active setting,
    and reports that a Codex restart is required.
    """
    base = base or codex_home(env)
    ensure_file_storage(base, env)

    root_path = base / "auth.json"
    target_slot = codex_slot_auth_path(target_id, base)
    target_data = _read_json(target_slot)
    _validate_field_complete(target_data)

    live = _read_json(root_path)
    outgoing_id = db.get_active_account_id("codex")

    # Stale-replay guard BEFORE any write.
    _refuse_stale_replay(target_data, live)

    lock_cm = _codex_swap_lock(base) if lock else contextlib.nullcontext(True)
    with lock_cm as locked:
        if lock and not locked:
            raise CodexSwapError("could not acquire the Codex swap lock (another swap in progress)")

        # 1. CAPTURE the live root into the outgoing account's slot FIRST, so the
        #    tokens Codex rotated while it was active are not lost.
        captured = False
        if live and outgoing_id and outgoing_id != target_id:
            _atomic_write_json(codex_slot_auth_path(outgoing_id, base), live)
            captured = True

        # 2. Write the target field-complete into the shared root.
        _atomic_write_json(root_path, target_data)

    db.set_active_account_id(target_id, provider="codex")
    logger.info(
        "Swapped active Codex account -> %s (outgoing=%s, captured=%s)",
        target_id, outgoing_id, captured,
    )
    return CodexSwapResult(
        ok=True,
        target_id=target_id,
        outgoing_id=outgoing_id,
        restart_required=True,
        captured_outgoing=captured,
    )


# ---------------------------------------------------------------------------
# Per-account CODEX_HOME launch
# ---------------------------------------------------------------------------

def prepare_codex_account_home(
    account_id: int, base: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> Path:
    """Ensure the per-account home has the account's auth.json + file-store config.

    Returns the home path to export as ``CODEX_HOME``.
    """
    base = base or codex_home(env)
    home = codex_account_home(account_id, base, env)
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    slot = codex_slot_auth_path(account_id, base)
    if not slot.exists():
        # Seed it from the shared root if this account is the one currently live.
        if not seed_codex_slot(account_id, base, env) or not slot.exists():
            raise CodexSwapError(
                f"no stored credentials for Codex account {account_id} — "
                "add it (`jacked codex add`) while it's logged in first"
            )
    ensure_file_storage(home, env)
    return home


def build_codex_launch_env(
    account_id: int,
    base: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Environment for launching Codex isolated on ``account_id`` (CODEX_HOME set)."""
    home = prepare_codex_account_home(account_id, base, env)
    out = dict(env if env is not None else os.environ)
    out["CODEX_HOME"] = str(home)
    return out


def launch_codex(
    account_id: int,
    codex_args=(),
    base: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    codex_bin: Optional[str] = None,
) -> None:
    """Replace this process with ``codex`` running isolated on ``account_id``
    (its own ``CODEX_HOME``), so the shared root account is never touched."""
    codex = codex_bin or shutil.which("codex")
    if not codex:
        raise CodexSwapError("the `codex` CLI is not installed or not on PATH")
    run_env = build_codex_launch_env(account_id, base, env)
    os.execvpe(codex, [codex, *codex_args], run_env)
