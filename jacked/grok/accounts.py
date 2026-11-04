"""Import and switch Grok accounts without replaying a refresh token.

The live credential is ``<grok home>/auth.json``. jacked copies it into
``<grok home>/jacked-accounts/<id>/auth.json`` when the account is imported
or when a switch captures the outgoing login. The database row stores the
email and a non-secret marker. The refresh token stays in those files, which
Grok itself rotates.

Decisions, with the evidence (grok 1.0.50, scratch GROK_HOME, 2026-11-04):

* The slots stay under ``<grok home>/jacked-accounts``. ``grok logout``
  removes only ``auth.json``, and ``grok du`` lists the folder and removes
  nothing (``--clean`` acts only on Grove redirect folders). The slots then
  follow ``GROK_HOME``, as the live file does.
* jacked does not take Grok's ``auth.json.lock``. Grok holds an OS lock
  (``flock``) on that file. Its pid text is only a note: Grok wrote over a
  live pid and did not wait. With ``flock`` held by another process,
  ``grok logout`` printed "Logged out" and left ``auth.json`` in place. A
  Grok refresh that met a lock held by jacked could skip its write in the
  same way, and lose a refresh token that the server already rotated. The
  switch lock below, the capture of the outgoing login, the atomic replace
  and the stale-replay refusal cover the switch instead.
* A switch needs no Grok restart. The Grok docs (Authentication > Hot
  Reload) say that Grok reads a changed ``auth.json`` on the next API call.
  Leader mode is off by default (``cli.use_leader``).
* On Windows the file is ``%USERPROFILE%\\.grok\\auth.json``: the Grok
  installer (``x.ai/cli/install.ps1``) sets ``$GrokDir`` from
  ``USERPROFILE``, and ``Path.home()`` reads ``USERPROFILE`` on Windows.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional

from jacked.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

# Grok rotates its own tokens. A far-future expiry keeps the row from looking
# permanently expired to the Claude refresh loop, which must not touch it.
GROK_EXPIRES_SENTINEL = 4102444800  # 2100-01-01T00:00:00Z
_MANAGED_TOKEN = "grok-managed"
# How long a switch waits for another one. A switch holds the lock for
# milliseconds.
_LOCK_WAIT_SECONDS = 30.0


class GrokAccountError(Exception):
    """A Grok account could not be imported or switched."""


def grok_home(env: Optional[Mapping[str, str]] = None) -> Path:
    """Directory that holds ``auth.json``. ``GROK_HOME`` overrides the default."""
    source = env if env is not None else os.environ
    override = source.get("GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def slot_dir(account_id: int, home: Path) -> Path:
    """Per-account directory that holds the stored copy of auth.json."""
    return home / "jacked-accounts" / str(int(account_id))


def slot_auth_path(account_id: int, home: Path) -> Path:
    """Per-account copy of auth.json. Grok does not read this directory."""
    return slot_dir(account_id, home) / "auth.json"


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _records(data: Optional[dict]) -> list[dict]:
    if not data:
        return []
    found: list[dict] = []
    for value in data.values():
        if isinstance(value, dict) and (
            value.get("email") or value.get("key") or value.get("refresh_token")
        ):
            found.append(value)
    return found


def usable_login(data: Optional[dict]) -> bool:
    """True when ``data`` names an identity and holds a key or a refresh token.

    >>> usable_login({"s": {"email": "a@x.com", "refresh_token": "r"}})
    True
    >>> usable_login({"s": {"email": "a@x.com"}}), usable_login(None)
    (False, False)
    """
    return bool(identity_email(data)) and any(
        record.get("key") or record.get("refresh_token") for record in _records(data)
    )


def identity_email(data: Optional[dict]) -> Optional[str]:
    for record in _records(data):
        email = (record.get("email") or "").strip()
        if email:
            return email
    return None


def _expires(data: Optional[dict]) -> Optional[datetime]:
    best: Optional[datetime] = None
    for record in _records(data):
        raw = record.get("expires_at")
        if not raw:
            continue
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if best is None or parsed > best:
            best = parsed
    return best


def _user_id(data: dict) -> str:
    for record in _records(data):
        user_id = (record.get("user_id") or "").strip()
        if user_id:
            return user_id
    return ""


def _atomic_write(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` as a private file (0600 in a 0700 directory).

    The shared writer retries the replace while Grok holds the file open on
    Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    atomic_write_text(path, json.dumps(data) + "\n", mode=0o600)


@contextlib.contextmanager
def _switch_lock(home: Path) -> Iterator[None]:
    """One switch or slot copy at a time, on Windows, macOS and Linux.

    ``mkdir`` is atomic on all three (``flock`` does not exist on Windows).
    The holder writes its pid into the lock. A lock whose pid file is missing,
    or whose process is dead, was left by a switch that died, and is broken.
    This is the rule of the Codex swap lock (``_codex_swap_lock``). This lock
    does not touch Grok's own ``auth.json.lock``.
    """
    parent = home / "jacked-accounts"
    parent.mkdir(parents=True, exist_ok=True)
    lock_dir = parent / ".switch.lockdir"
    pid_file = lock_dir / "pid"
    deadline = time.time() + _LOCK_WAIT_SECONDS
    while True:
        try:
            lock_dir.mkdir()
        except FileExistsError:
            if _lock_is_stale(pid_file):
                shutil.rmtree(lock_dir, ignore_errors=True)
                logger.warning("Removed a Grok switch lock that a dead process left")
                continue
            if time.time() > deadline:
                raise GrokAccountError(
                    "Another Grok account switch is still in progress."
                ) from None
            time.sleep(0.05)
            continue
        try:
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        break
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _lock_is_stale(pid_file: Path) -> bool:
    """True when the lock holder is gone: no pid file, or a dead process."""
    from jacked.service.process import process_liveness

    try:
        holder = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    try:
        return process_liveness(holder) is False
    except (OSError, SystemError):
        return False


def find_grok_account_id(db, data: Optional[dict]) -> Optional[int]:
    """Match a live auth file to a stored Grok row, by user id then email."""
    if not data:
        return None
    user_id = _user_id(data)
    email = (identity_email(data) or "").lower()
    by_email: Optional[int] = None
    for account in db.list_accounts():
        if (account.get("provider") or "") != "grok":
            continue
        if user_id and (account.get("organization_uuid") or "") == user_id:
            return account["id"]
        if email and (account.get("email") or "").lower() == email:
            by_email = account["id"]
    return by_email


def live_login_owner(db, home: Optional[Path] = None) -> tuple[bool, Optional[int]]:
    """Who the live Grok login belongs to: ``(signed_in, account_id)``.

    ``signed_in`` is False when no usable login is live. ``account_id`` is
    None for a usable login that matches no stored Grok account.
    """
    root = home if home is not None else grok_home()
    live = _read_json(root / "auth.json")
    if not usable_login(live):
        return False, None
    return True, find_grok_account_id(db, live)


def _refuse_stale(target: dict, live: Optional[dict]) -> None:
    """Do not write an older copy of the same login over a newer live file.

    Grok's refresh token rotates. Replaying the old file invalidates the
    login the CLI just refreshed.
    """
    if not live:
        return
    target_email = (identity_email(target) or "").lower()
    live_email = (identity_email(live) or "").lower()
    if not target_email or target_email != live_email:
        return
    target_exp = _expires(target)
    live_exp = _expires(live)
    target_refresh = _refresh_value(target)
    live_refresh = _refresh_value(live)
    slot_is_newer = (
        target_exp is not None and live_exp is not None and target_exp > live_exp
    )
    if target_refresh and live_refresh and target_refresh != live_refresh and not slot_is_newer:
        raise GrokAccountError(
            "Refusing to replay a Grok login whose refresh token is not the "
            "live one. Grok rotates that token. Re-auth so jacked copies the "
            "file Grok just wrote."
        )
    if target_exp is not None and live_exp is not None and target_exp < live_exp:
        raise GrokAccountError(
            "Refusing to replay an older Grok login over a newer one. "
            "Re-auth this account so jacked stores the current login."
        )


def import_grok_account(
    db,
    home: Optional[Path] = None,
    *,
    make_active: bool = False,
) -> dict:
    """Persist the live ``auth.json`` as a ``provider='grok'`` row.

    Raises ``GrokAccountError`` when no signed-in Grok identity is readable.
    The refresh token is not copied into the database.
    """
    root = home if home is not None else grok_home()
    live_path = root / "auth.json"
    data = _read_json(live_path)
    email = identity_email(data)
    if data is None or not email:
        raise GrokAccountError(
            "No Grok login found. Re-auth to sign in. jacked opens that login "
            "in this account's browser profile."
        )
    if not usable_login(data):
        raise GrokAccountError("Grok auth.json has no usable login. Re-auth this account.")

    account = db.create_account(
        email=email,
        access_token=_MANAGED_TOKEN,
        expires_at=GROK_EXPIRES_SENTINEL,
        refresh_token=None,
        organization_uuid=_user_id(data),
        provider="grok",
    )
    _atomic_write(slot_auth_path(account["id"], root), data)
    db.update_account(account["id"], validation_status="valid")
    account = db.get_account(account["id"]) or account
    if make_active:
        db.set_active_account_id(account["id"], provider="grok")
    logger.info("Imported Grok account id=%s", account.get("id"))
    return account


def _refresh_value(data: Optional[dict]) -> Optional[str]:
    for record in _records(data):
        token = record.get("refresh_token")
        if token:
            return token
    return None


def sync_active_slot_from_live(db, home: Optional[Path] = None) -> bool:
    """Copy the live auth.json into the active Grok slot when the CLI rotated it.

    Grok access tokens expire in well under an hour. The CLI refreshes them
    and writes a new refresh token into auth.json. jacked must not call that
    refresh endpoint. Copying the file the CLI already wrote keeps the slot
    from becoming the stale copy a later switch would replay. Returns True
    when the slot was updated.
    """
    root = home if home is not None else grok_home()
    active_id = db.get_active_account_id(provider="grok")
    if not active_id:
        return False
    # Under the switch lock: a copy read before a switch and written after it
    # would put an older file over the one the switch just captured.
    with _switch_lock(root):
        live = _read_json(root / "auth.json")
        if find_grok_account_id(db, live) != active_id:
            return False
        slot_path = slot_auth_path(active_id, root)
        slot = _read_json(slot_path)
        if (
            slot
            and _refresh_value(slot) == _refresh_value(live)
            and _expires(slot) == _expires(live)
        ):
            return False
        _atomic_write(slot_path, live)
    logger.info("Copied the live Grok login into account %s slot", active_id)
    return True


def swap_grok_account(db, target_id: int, home: Optional[Path] = None) -> dict:
    """Make ``target_id`` the live Grok login.

    Captures the current auth.json into the outgoing account's slot first.
    Refuses to overwrite a newer live login with an older stored copy of the
    same account. Grok re-reads auth.json on the next request.
    """
    root = home if home is not None else grok_home()
    live_path = root / "auth.json"
    with _switch_lock(root):
        # Read the slot under the lock too: the timer copy writes it.
        target = _read_json(slot_auth_path(target_id, root))
        if target is None or not identity_email(target):
            raise GrokAccountError(
                "No stored Grok login for this account. Re-auth it first."
            )
        live = _read_json(live_path)
        _refuse_stale(target, live)
        outgoing_id = find_grok_account_id(db, live) if live else None
        captured = False
        if live and outgoing_id is not None and outgoing_id != target_id:
            _atomic_write(slot_auth_path(outgoing_id, root), live)
            captured = True
        _atomic_write(live_path, target)

    db.set_active_account_id(target_id, provider="grok")
    logger.info(
        "Swapped active Grok account -> %s (outgoing=%s, captured=%s)",
        target_id,
        outgoing_id,
        captured,
    )
    return {
        "success": True,
        "provider": "grok",
        "account_id": target_id,
        "outgoing_id": outgoing_id,
        "captured_outgoing": captured,
        "restart_required": False,
        "message": (
            "Switched the active Grok account. Grok reads the new login on its "
            "next request. You do not need to restart it."
        ),
    }
