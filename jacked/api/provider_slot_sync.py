"""The 30-minute copy of a rotated Codex or Grok login into its account slot.

Called by the token refresh loop in ``jacked.api.main`` (kept out of that
module by the module size ratchet).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def sync_provider_slots(db=None) -> None:
    """Copy a rotated Codex or Grok login into the active account's slot.

    Both CLIs rotate their own refresh tokens and write the new pair into
    their auth file. jacked copies that file and never calls their token
    endpoints. Each copy runs in a worker thread, because the Grok switch
    lock can wait seconds. A failure in one provider does not stop the other.
    """
    from jacked.codex.switching import sync_active_slot_from_live as sync_codex
    from jacked.grok.accounts import sync_active_slot_from_live as sync_grok

    if db is None:
        from jacked.web.database import Database

        try:
            db = await asyncio.to_thread(Database)
        except Exception as exc:
            logger.debug("Provider slot copy skipped: %s", type(exc).__name__)
            return
    for name, sync in (("codex", sync_codex), ("grok", sync_grok)):
        try:
            await asyncio.to_thread(sync, db)
        except Exception as exc:
            # The type only: an error text can name a home directory.
            logger.debug("%s slot copy failed: %s", name, type(exc).__name__)
