"""The ``jacked grok`` commands, and the database handle of the provider commands.

``jacked.cli`` registers ``grok_group``. The command bodies are here because
``jacked/cli.py`` is over its size budget (module size ratchet).

``jacked grok`` mirrors ``jacked codex``: add, list, switch, remove. Grok
keeps its login in ``<grok home>/auth.json`` and rotates its own refresh
token. jacked stores identity in its database and a copy of the login in
``<grok home>/jacked-accounts/<id>/auth.json``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape

console = Console()


def provider_db():
    """The jacked database (``~/.claude/jacked.db``)."""
    from jacked.web.database import Database

    return Database(str(Path.home() / ".claude" / "jacked.db"))


@click.group(name="grok")
def grok_group():
    """Manage xAI Grok accounts (add, list, switch, remove)."""


def _sign_in(db) -> dict:
    """Run ``grok login`` in a private home, then store the new login.

    The live login stays unless nobody is signed in. Returns the payload of
    ``finish_login``.
    """
    from jacked.findbin import find_bin
    from jacked.grok.login_import import CLI, finish_login, prepare_login_env

    grok = find_bin("grok")
    if not grok:
        raise click.ClickException("The grok CLI is not installed. Install it, then try again.")
    extra_env, extra_args = prepare_login_env(name=CLI)
    console.print("Sign in to the Grok account to add. A browser opens for the sign-in.")
    try:
        code = subprocess.run(  # noqa: S603 - find_bin result plus fixed arguments
            [grok, "login", *extra_args], env={**os.environ, **extra_env}, check=False,
        ).returncode
    except OSError as exc:
        code = 1
        console.print(f"[red]Could not run grok login:[/red] {type(exc).__name__}")
    return finish_login(db, "signed_in" if code == 0 else "failed", name=CLI)


@grok_group.command(name="add")
@click.option("--make-active", is_flag=True,
              help="Make the added account the live Grok login.")
@click.option("--no-login", is_flag=True,
              help="Do not run `grok login`. Import only a login that is live now.")
def grok_add_cmd(make_active, no_login):
    """Add a Grok account to jacked.

    A live login that jacked does not know yet (from a `grok login` in a
    terminal) is imported and is the active Grok account. Else jacked runs
    `grok login` in a private home, so the live login stays.

    >>> # CLI command: jacked grok add [--make-active] [--no-login]
    """
    from jacked.grok.accounts import (
        GrokAccountError,
        import_grok_account,
        live_login_owner,
        swap_grok_account,
    )

    db = provider_db()
    try:
        signed_in, owner = live_login_owner(db)
        if signed_in and owner is None:
            acct = import_grok_account(db, make_active=True)
            console.print(
                f"Added Grok account [bold]{escape(acct['email'])}[/bold] (id {acct['id']}). "
                "It is the active Grok account."
            )
            return
        if no_login:
            raise click.ClickException(
                "No new Grok login to import. Run without --no-login to sign in."
            )
        result = _sign_in(db)
        if not result.get("ok"):
            raise click.ClickException(result.get("message") or "The Grok sign-in did not finish.")
        console.print(escape(result["message"]))
        if make_active and not result.get("live"):
            swap_grok_account(db, result["account_id"])
            console.print(f"Active Grok account: [bold]{result['account_id']}[/bold].")
    except GrokAccountError as exc:
        raise click.ClickException(str(exc))
    finally:
        db.close()


@grok_group.command(name="list")
def grok_list_cmd():
    """List the Grok accounts jacked knows about. An arrow marks the active one.

    >>> # CLI command: jacked grok list
    """
    db = provider_db()
    try:
        active = db.get_active_account_id("grok")
        rows = [a for a in db.list_accounts() if a.get("provider") == "grok"]
    finally:
        db.close()
    if not rows:
        console.print("No Grok accounts yet. Add one with [bold]jacked grok add[/bold].")
        return
    for a in rows:
        mark = "→ " if a["id"] == active else "  "
        console.print(f"{mark}[bold]{a['id']}[/bold]  {escape(a['email'])}", highlight=False)


@grok_group.command(name="switch")
@click.argument("account_id", type=int)
def grok_switch_cmd(account_id):
    """Make ACCOUNT_ID the live Grok login (swaps <grok home>/auth.json).

    jacked first copies the live login into the slot of its account. Grok
    reads the new login on its next request, with no restart.

    >>> # CLI command: jacked grok switch <id>
    """
    from jacked.grok.accounts import GrokAccountError, swap_grok_account

    db = provider_db()
    try:
        account = db.get_account(account_id)
        if not account or account.get("provider") != "grok":
            raise click.ClickException(f"No Grok account with id {account_id}.")
        result = swap_grok_account(db, account_id)
    except GrokAccountError as exc:
        raise click.ClickException(str(exc))
    finally:
        db.close()
    console.print(escape(result["message"]))


@grok_group.command(name="remove")
@click.argument("account_id", type=int)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def grok_remove_cmd(account_id, yes):
    """Remove ACCOUNT_ID from jacked and delete its stored login copy.

    The live Grok login stays. To sign out of Grok, run `grok logout`.

    >>> # CLI command: jacked grok remove <id> [--yes]
    """
    from jacked.api.provider_accounts import forget_slot

    db = provider_db()
    try:
        account = db.get_account(account_id)
        if not account or account.get("provider") != "grok":
            raise click.ClickException(f"No Grok account with id {account_id}.")
        if not yes:
            click.confirm(f"Remove Grok account {account['email']} from jacked?", abort=True)
        db.delete_account(account_id)
        forget_slot(db, account)
    finally:
        db.close()
    console.print(f"Removed Grok account [bold]{escape(account['email'])}[/bold] from jacked.")
