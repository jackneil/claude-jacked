"""Data-preserving migration of baked autostart ``--host`` values.

Pre-M5 autostart artifacts (the launchd plist on macOS, the Startup-folder VBS
on Windows) baked ``--host X`` into their command line. The bind host now lives
in the settings DB (see :mod:`jacked.service.bind`), and the artifacts are
generated host-free, so a user who once ran ``jacked service install --host
0.0.0.0`` must have that intent captured in the DB *before* the old artifact is
overwritten or its argv neutralized. This module owns that capture:

- :func:`extract_baked_host` parses the baked ``--host`` out of an artifact's
  text without ever raising on malformed input.
- :func:`strip_baked_host` rewrites artifact text host-free while preserving
  everything else (binary path, port, environment) byte-for-byte.
- :func:`map_host_to_setting` is the single host -> settings mapping shared by
  the migration and the ``service install/restart --host`` CLI parity path.
- :func:`migrate_baked_host_to_db` applies the mapping with the critical guard:
  it writes the DB ONLY when ``remote_access_enabled`` is absent. A stale
  artifact must never clobber a choice the user already made in the GUI.
"""

from __future__ import annotations

import logging
import re

from jacked.service.bind import _in_cgnat_range

logger = logging.getLogger(__name__)

# launchd plist: ProgramArguments carries the pair
#     <string>--host</string>
#     <string>X</string>
_PLIST_HOST_RE = re.compile(r"<string>\s*--host\s*</string>\s*<string>([^<]*)</string>")
# Same pair including surrounding whitespace/newlines, for host-free rewriting.
_PLIST_HOST_STRIP_RE = re.compile(
    r"[ \t]*<string>\s*--host\s*</string>\s*<string>[^<]*</string>[ \t]*\n?"
)

# Windows VBS: a single WshShell.Run command line carrying `--host X`.
# Hosts are IP literals or hostnames; stop before the VBS quote/comma tail.
_VBS_HOST_RE = re.compile(r"--host\s+([\w.:\-]+)")
_VBS_HOST_STRIP_RE = re.compile(r"\s--host\s+[\w.:\-]+")


def extract_baked_host(artifact_text: str, kind: str) -> str | None:
    """The ``--host`` value baked into an autostart artifact, or ``None``.

    ``kind`` is ``"plist"`` (macOS launchd) or ``"vbs"`` (Windows Startup
    folder). Malformed/garbage text returns ``None`` without raising.
    """
    try:
        if kind == "plist":
            match = _PLIST_HOST_RE.search(artifact_text)
        elif kind == "vbs":
            match = _VBS_HOST_RE.search(artifact_text)
        else:
            return None
    except TypeError:  # non-string input
        return None
    if match is None:
        return None
    host = match.group(1).strip()
    return host or None




def strip_baked_host(artifact_text: str, kind: str) -> str:
    """Return the artifact text with its ``--host X`` removed, all else intact.

    Surgical, not a regeneration: the boot-time migration may be running INSIDE
    the very process the artifact describes (the launchd job's own respawn), so
    the rewrite must preserve the binary path, port, log paths, and environment
    exactly. Text with no baked host is returned unchanged (idempotent).
    """
    if kind == "plist":
        return _PLIST_HOST_STRIP_RE.sub("", artifact_text)
    if kind == "vbs":
        return _VBS_HOST_STRIP_RE.sub("", artifact_text)
    return artifact_text


def map_host_to_setting(host: str) -> tuple[str, str | None] | None:
    """Map a ``--host`` value onto ``(remote_access_enabled, remote_access_scope)``.

    The single mapping shared by artifact migration and the ``service
    install/restart --host`` parity path (which differ only in overwrite
    semantics):

    - ``0.0.0.0`` -> ``('true', 'all')``
    - an IP inside Tailscale's CGNAT range 100.64.0.0/10 -> ``('true', 'tailscale')``
    - ``127.0.0.1`` / ``localhost`` -> ``('false', None)`` (loopback only)
    - anything else -> ``None``: a pinned specific IP is a one-shot CLI
      concept, not a persistent mode.
    """
    if host in ("127.0.0.1", "localhost"):
        return ("false", None)
    if host == "0.0.0.0":
        return ("true", "all")
    if _in_cgnat_range(host):
        return ("true", "tailscale")
    return None


def migrate_baked_host_to_db(host: str, db=None) -> str:
    """Copy a baked artifact ``--host`` into the settings DB, guarded.

    Returns a human-readable description of what happened (also logged).
    Never raises: migration is best-effort bookkeeping that must not break an
    install or a service boot.

    Rules (see module docstring):

    - loopback hosts write NOTHING: the absent-key default already means
      "remote access off", and writing would needlessly claim the keys.
    - unmapped specific IPs write NOTHING: a pinned specific IP is a one-shot
      CLI concept, not a persistent mode (info-logged).
    - mapped hosts write ONLY when ``remote_access_enabled`` is ABSENT. A
      stale artifact must never clobber a choice already made in the GUI or
      via ``service install/restart --host``.
    """
    mapping = map_host_to_setting(host)
    if mapping is None:
        msg = (
            f"baked host {host} not migrated: a pinned specific IP is a "
            "one-shot CLI concept, not a persistent mode"
        )
        logger.info(msg)
        return msg
    enabled, scope = mapping
    if enabled == "false":
        msg = f"baked host {host} is the loopback default; nothing to migrate"
        logger.info(msg)
        return msg
    try:
        if db is None:
            from jacked.web.database import Database

            db = Database()
        if db.get_setting("remote_access_enabled") is not None:
            msg = (
                "remote access setting already present; "
                f"baked host {host} left alone"
            )
            logger.info(msg)
            return msg
        # Write scope FIRST, then the guard key `remote_access_enabled` LAST.
        # If the second write fails mid-migration, `remote_access_enabled` stays
        # absent, so the guard above lets a later boot retry the whole migration
        # cleanly instead of the intent sticking half-applied (enabled set, scope
        # missing) and being permanently downgraded to the default scope.
        if scope is not None:
            db.set_setting("remote_access_scope", scope)
        db.set_setting("remote_access_enabled", enabled)
    except Exception as exc:  # sqlite errors, unreadable DB path ...
        msg = f"could not migrate baked host {host}: {exc}"
        logger.warning(msg)
        return msg
    msg = f"migrated baked host {host} to remote access: enabled, scope {scope}"
    logger.info(msg)
    return msg


def remote_access_configured(db=None) -> bool:
    """True if remote access has been configured via the GUI or CLI parity.

    ``remote_access_enabled`` is present in the settings DB exactly when a user
    has set remote access through the dashboard toggle or ``service
    install/restart --host`` (which persists it). When it is present the DB is
    the authoritative source for the service/autostart bind, and a ``--host``
    reaching ``service start`` from a stale launchd in-memory replay must be
    ignored. Absent means a legacy pure-CLI setup that never used the DB channel,
    where an explicit ``--host`` is still honored. Never raises.
    """
    try:
        if db is None:
            from jacked.web.database import Database

            db = Database()
        return db.get_setting("remote_access_enabled") is not None
    except Exception as exc:  # a broken DB must not crash boot resolution
        logger.warning("Could not read remote-access setting (%s); "
                       "treating as unconfigured", exc)
        return False
