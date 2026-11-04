"""Sign in to Grok in an isolated home, then store that login for one account.

``grok login`` writes ``$GROK_HOME/auth.json``. jacked runs it with
``GROK_HOME`` set to a private directory under ``jacked-accounts``, so a
sign-in never replaces the live login in ``<grok home>/auth.json``. When
the CLI exits with success, ``finish_login`` copies the new login into the
account slot. It writes the live file only when no usable login is live, or
when the live login is the same account (a re-auth of the live account).
Then it removes the private directory, which holds a refresh token.

The builder verified on grok 1.0.50 that ``grok login`` honors
``GROK_HOME``. jacked also passes ``--leader-socket`` inside the private
directory, so the sign-in never talks to a leader of the real home.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

from jacked.grok.accounts import (
    GROK_EXPIRES_SENTINEL,
    GrokAccountError,
    _MANAGED_TOKEN,
    _atomic_write,
    _read_json,
    _switch_lock,
    _user_id,
    find_grok_account_id,
    grok_home,
    identity_email,
    read_live_login,
    slot_auth_path,
    usable_login,
)
from jacked.public_errors import describe

logger = logging.getLogger(__name__)

# Grok settings that a sign-in can need: an enterprise OIDC issuer, a fleet
# default, an admin requirement. jacked copies them into the private home.
_CONFIG_FILES = ("config.toml", "managed_config.toml", "requirements.toml")


# The name prefixes of the sign-in homes. Each sign-in gets its own home
# (``new_login_name``), so the cleanup of one sign-in never removes the
# login of another one: a dashboard sign-in, a `jacked grok add`, or a
# second jacked process.
DASHBOARD = ".sign-in"
CLI = ".sign-in-cli"
# A sign-in home this old was left by a process that stopped before its
# cleanup. A device code lives 15 minutes, so no sign-in runs this long.
_ABANDONED_SECONDS = 24 * 3600
_STORE_FAILED = (
    "jacked could not store the Grok sign-in. Check the jacked log, then try "
    "the sign-in again."
)


def new_login_name(kind: str = DASHBOARD) -> str:
    """A new, unique name for the private home of one sign-in.

    >>> new_login_name().startswith(".sign-in-"), new_login_name(CLI).startswith(".sign-in-cli-")
    (True, True)
    >>> new_login_name() != new_login_name()
    True
    """
    return f"{kind}-{secrets.token_hex(8)}"


def login_dir(home: Optional[Path] = None, name: str = DASHBOARD) -> Path:
    """The private ``GROK_HOME`` of a sign-in that jacked runs.

    >>> login_dir(Path("/h/.grok")).as_posix()
    '/h/.grok/jacked-accounts/.sign-in'
    >>> login_dir(Path("/h/.grok"), CLI).name
    '.sign-in-cli'
    """
    root = home if home is not None else grok_home()
    return root / "jacked-accounts" / name


def discard_login_dir(home: Optional[Path] = None, name: str = DASHBOARD) -> None:
    """Remove the private sign-in home. It can hold a refresh token."""
    target = login_dir(home, name)
    if target.is_symlink():
        target.unlink()
        return
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def _discard_abandoned(root: Path, keep: str) -> None:
    """Remove the sign-in homes that a stopped process left. They can hold a
    refresh token."""
    parent = root / "jacked-accounts"
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    cutoff = time.time() - _ABANDONED_SECONDS
    for entry in entries:
        if not entry.name.startswith(DASHBOARD) or entry.name == keep:
            continue
        try:
            if entry.lstat().st_mtime < cutoff:
                discard_login_dir(root, entry.name)
        except OSError:
            continue


def prepare_login_env(
    home: Optional[Path] = None, name: str = DASHBOARD
) -> tuple[dict, tuple[str, ...]]:
    """Make an empty private sign-in home. Return the env and the CLI arguments.

    Give each sign-in its own ``name`` (``new_login_name``): this removes an
    earlier directory with the same name. It also removes the sign-in homes
    that are older than one day.
    """
    root = home if home is not None else grok_home()
    target = login_dir(root, name)
    discard_login_dir(root, name)
    _discard_abandoned(root, name)
    target.mkdir(parents=True, exist_ok=False)
    for directory in (target.parent, target):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    for name in _CONFIG_FILES:
        source = root / name
        if source.is_file() and not source.is_symlink():
            try:
                shutil.copyfile(source, target / name)
            except OSError:
                logger.debug("Could not copy a Grok setting file into the sign-in home")
    env = {"GROK_HOME": str(target)}
    return env, ("--leader-socket", str(target / "leader.sock"))


def _same_identity(account: dict, data: dict) -> bool:
    """True when the login ``data`` belongs to the stored ``account``.

    The user id decides when both sides have one. Else the email decides.

    >>> row = {"email": "A@x.com", "organization_uuid": "u1"}
    >>> _same_identity(row, {"s": {"email": "a@x.com", "user_id": "u1"}})
    True
    >>> _same_identity(row, {"s": {"email": "a@x.com", "user_id": "u2"}})
    False
    >>> _same_identity({"email": "a@x.com"}, {"s": {"email": "A@X.com"}})
    True
    """
    stored_id = (account.get("organization_uuid") or "").strip()
    login_id = _user_id(data)
    if stored_id and login_id:
        return stored_id == login_id
    stored_email = (account.get("email") or "").strip().lower()
    return bool(stored_email) and stored_email == (identity_email(data) or "").lower()


def store_signed_in_login(
    db, *, target_id: Optional[int] = None, home: Optional[Path] = None,
    name: str = DASHBOARD,
) -> dict:
    """Store the login in the private sign-in home for one Grok account.

    ``target_id`` is the account of a re-auth. The login must belong to it.
    Without ``target_id`` (Add Account), the login goes to the stored account
    with the same identity, or to a new row.

    Returns ``{"account", "created", "live"}``. ``live`` is True when the
    login is now the live Grok login and the account is the active one.
    Raises ``GrokAccountError`` when the login is not usable or belongs to a
    different account.
    """
    root = home if home is not None else grok_home()
    data = _read_json(login_dir(root, name) / "auth.json")
    if not usable_login(data):
        raise GrokAccountError("The Grok sign-in wrote no usable login. Try the sign-in again.")
    if target_id is not None:
        target = db.get_account(target_id)
        if not target or (target.get("provider") or "") != "grok":
            raise GrokAccountError("This Grok account is not in jacked any more.")
        if not _same_identity(target, data):
            raise GrokAccountError(
                f"You signed in as {identity_email(data)}, not as {target.get('email')}. "
                "jacked did not change the account. Re-auth again and sign in as "
                f"{target.get('email')}."
            )
        account_id: Optional[int] = target_id
    else:
        account_id = find_grok_account_id(db, data)

    created = False
    with _switch_lock(root):
        live = read_live_login(root / "auth.json", "sign-in")
        live_owner = find_grok_account_id(db, live) if usable_login(live) else None
        if account_id is None:
            row = db.create_account(
                email=identity_email(data),
                access_token=_MANAGED_TOKEN,
                expires_at=GROK_EXPIRES_SENTINEL,
                refresh_token=None,
                organization_uuid=_user_id(data),
                provider="grok",
            )
            account_id = row["id"]
            created = True
        _atomic_write(slot_auth_path(account_id, root), data)
        # Never replace a usable live login of a different account: its
        # rotated refresh token exists only in that file.
        make_live = not usable_login(live) or live_owner == account_id
        if make_live:
            _atomic_write(root / "auth.json", data)
    # Add Account puts the account back in the pool: a disabled row of the
    # same login is enabled again, as the import does. A re-auth keeps it.
    enable = {"is_active": 1} if target_id is None else {}
    db.update_account(account_id, validation_status="valid", **enable)
    if make_live:
        db.set_active_account_id(account_id, provider="grok")
    account = db.get_account(account_id) or {"id": account_id, "email": identity_email(data)}
    logger.info(
        "Stored a Grok sign-in for account %s (created=%s, live=%s)",
        account_id, created, make_live,
    )
    return {"account": account, "created": created, "live": make_live}


_OUTCOME_MESSAGES = {
    "failed": "The Grok sign-in did not finish. jacked did not change any account.",
    "timed_out": (
        "The Grok sign-in code expired before the sign-in was done. "
        "Start the sign-in again."
    ),
    "no_browser": (
        "jacked could not open a browser for the Grok sign-in. Run `grok login` "
        "on the computer that runs jacked, then add the Grok account again."
    ),
}


def finish_login(
    db, outcome: str, *, target_id: Optional[int] = None, home: Optional[Path] = None,
    name: str = DASHBOARD,
) -> dict:
    """Store a finished sign-in and return the message for the dashboard.

    ``outcome`` is the result of ``jacked.web.external_login``. The private
    sign-in home is removed in all cases.
    """
    payload: dict = {"provider": "grok", "ok": False, "account_id": target_id}
    try:
        if outcome != "signed_in":
            payload["message"] = _OUTCOME_MESSAGES.get(outcome, _OUTCOME_MESSAGES["failed"])
            return payload
        try:
            stored = store_signed_in_login(db, target_id=target_id, home=home, name=name)
        except GrokAccountError as exc:
            payload["message"] = exc.public_message
            return payload
        except Exception as exc:  # noqa: BLE001 - the dashboard must get a result
            logger.warning("Storing a Grok sign-in failed: %s", describe(exc))
            payload["message"] = _STORE_FAILED
            return payload
        account = stored["account"]
        email = account.get("email") or ""
        if target_id is None:
            from jacked.web.pending_profile import adopt_pending_profile

            adopt_pending_profile("grok", email)
        payload.update(
            ok=True, account_id=account.get("id"), email=email,
            created=stored["created"], live=stored["live"],
        )
        if stored["live"]:
            payload["message"] = f"Signed in to Grok as {email}. It is the active Grok account."
        else:
            payload["message"] = (
                f"Signed in to Grok as {email}. The active Grok account did not change. "
                "Use this account to switch to it."
            )
        return payload
    finally:
        discard_login_dir(home, name)
