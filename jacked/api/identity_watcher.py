"""Keep the Claude Code config identity true to the credential authority.

Claude Code merges a profile into ``~/.claude.json`` ``oauthAccount`` from
whatever token a process holds. After a switch, a stale process can write the
previous account back into the config, and running sessions then key their
usage cache, consent and gating on the wrong account.

The service observer submits each published snapshot to this watcher. The
watcher runs on one daemon worker thread, so a slow profile lookup or a held
config lock never delays the observer. A submit while a run is in flight is
skipped. When the config identity diverges from the block jacked would
publish for the account that holds the authority, the watcher republishes
that block. It writes only when all of these are true:

1. The service has no ``CLAUDE_CONFIG_DIR`` and the observation is a
   RESOLVED global observation of a Claude row.
2. The stored access token belongs to the observed row. One profile GET
   proves this. The GET never refreshes a token and never writes the
   database. The result is cached per authority payload digest.
3. The switch lease is free, and under the lease the authority digest and
   the observed and desired accounts are unchanged.
4. The config block is unchanged since the watcher read it (compare and
   swap in the writer).

The watcher makes at most one attempt per account per minute. A breaker stops
the writes when one account needs more than three republishes in fifteen
minutes. Logs carry row ids, 8-character uuid prefixes and field names only,
never emails or tokens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jacked.credentials.resolver import ResolverState, SnapshotUpdate

logger = logging.getLogger(__name__)

ATTEMPT_MIN_INTERVAL_SECONDS = 60.0
TRANSIENT_RETRY_SECONDS = 60.0
BREAKER_WINDOW_SECONDS = 15 * 60.0
BREAKER_MAX_REPUBLISHES = 3
CONFIG_LOCK_TIMEOUT_SECONDS = 1.0
# How long a "the block already matches the config" verdict is reused for an
# unchanged row and config. The builder also reads other rows (the shared
# uuid check), so the verdict is not kept forever.
SETTLED_TTL_SECONDS = 15 * 60.0
_OWNER_CACHE_LIMIT = 32

EVIDENCE_PREFIX = "claude-config:identity:"
EVIDENCE_REPUBLISHED = f"{EVIDENCE_PREFIX}republished"
EVIDENCE_PARTIAL = f"{EVIDENCE_PREFIX}partial"
EVIDENCE_UUID_UNKNOWN = f"{EVIDENCE_PREFIX}uuid-unknown"
EVIDENCE_DIVERGENT = f"{EVIDENCE_PREFIX}divergent"
EVIDENCE_FLAPPING = f"{EVIDENCE_PREFIX}flapping"
EVIDENCE_OWNER_UNPROVEN = f"{EVIDENCE_PREFIX}owner-unproven"
EVIDENCE_DEFERRED_SWITCH = f"{EVIDENCE_PREFIX}deferred-switch"
EVIDENCE_STAMP_MISMATCH = "authority:stamp-mismatch"

_LANDED = "written"

_VERDICT_PROFILE = "profile"
_VERDICT_UNPROVEN = "unproven"
_VERDICT_TRANSIENT = "transient"


@dataclass(frozen=True)
class AuthorityView:
    """The parts of the authority payload the watcher needs.

    ``account_id`` is the jacked stamp. ``digest`` is the canonical payload
    digest. The access token never appears in the repr.
    """

    account_id: int | None
    digest: str
    access_token: str | None = field(default=None, repr=False)
    expires_at_ms: float | None = None


@dataclass(frozen=True)
class _OwnerProof:
    """One profile lookup result for one authority digest. No token."""

    verdict: str
    at: float
    email: str | None = None
    organization_uuid: str | None = None
    account_uuid: str | None = None


@dataclass
class _AccountState:
    last_attempt: float | None = None
    republishes: list[float] = field(default_factory=list)
    breaker_key: tuple[str, int | None] | None = None
    tripped: bool = False
    last_reason: str | None = None
    # (sha256 of the row and the config, when) for a block that matched.
    settled: tuple[str, float] | None = None


def _settle_key(row: dict, config: dict) -> str:
    """Digest the row and the config. Only the digest is kept, never a value."""
    raw = json.dumps([row, config], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _expiry_ms(raw: Any) -> float | None:
    """Return an expiry in epoch milliseconds, or None when it is unknown."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return float(raw) if raw > 1e12 else float(raw) * 1000.0


def _read_authority_view(home: Path) -> AuthorityView | None:
    """Read the global authority through the adapter the observer uses."""
    from jacked.api.session_observer import _build_authority
    from jacked.credentials.models import StoreStatus

    store, _declaration = _build_authority(home / ".claude", "global")
    result = store.read()
    if result.status is not StoreStatus.OK or result.payload is None:
        return None
    payload = result.payload
    oauth = payload.to_mapping().get("claudeAiOauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    token = oauth.get("accessToken")
    return AuthorityView(
        account_id=payload.identity.account_id,
        digest=payload.digest,
        access_token=token if isinstance(token, str) and token else None,
        expires_at_ms=_expiry_ms(oauth.get("expiresAt")),
    )


def _default_profile_lookup(access_token: str):
    from jacked.api.authority_guard import lookup_profile

    return lookup_profile(access_token)


def _default_lease(home: Path):
    from jacked.credentials.runtime import switch_lease_for

    return switch_lease_for(home)


def _default_writer(home: Path, block, *, expected=None, lock_timeout: float = 5.0):
    from jacked.api.claude_identity import write_identity

    return write_identity(home, block, expected=expected, lock_timeout=lock_timeout)


def _default_build(row: dict, *, db=None):
    from jacked.api.claude_identity import build_identity_block

    return build_identity_block(row, db=db)


def _default_read_config(home: Path) -> dict | None:
    from jacked.api.claude_identity import read_claude_config_identity

    return read_claude_config_identity(home)


def _desired_account_id(db) -> int | None:
    from jacked.api.session_observer import _desired_identity

    desired = _desired_identity(db)
    return desired.account_id if desired is not None else None


def _casefold(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _prefix(value: Any) -> str:
    return value[:8] if isinstance(value, str) and value else "none"


def _stored_created_at(row: dict) -> str | None:
    raw = row.get("profile_json")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        profile = json.loads(raw)
    except ValueError:
        return None
    account = profile.get("account") if isinstance(profile, dict) else None
    return _text(account.get("created_at")) if isinstance(account, dict) else None


def _diverged_fields(row: dict, config: dict) -> list[str]:
    """Name the config identity fields that disagree with the row."""
    diverged = []
    if _casefold(config.get("emailAddress")) != _casefold(row.get("email")):
        diverged.append("emailAddress")
    row_org = _text(row.get("organization_uuid"))
    if row_org is not None and config.get("organizationUuid") != row_org:
        diverged.append("organizationUuid")
    row_uuid = _text(row.get("account_uuid"))
    if row_uuid is not None and config.get("accountUuid") != row_uuid:
        diverged.append("accountUuid")
    created_at = _stored_created_at(row)
    if created_at is not None and config.get("accountCreatedAt") != created_at:
        # A missing value is "incomplete": Claude Code then re-arms its
        # unguarded profile merge, so it gets one republish too.
        diverged.append("accountCreatedAt")
    return diverged


def _strip(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _block_diverged_fields(block: Any, config: dict) -> list[str]:
    """Name the config identity fields that disagree with the block.

    The raw row check above is only a cheap trigger. The builder can
    legitimately differ from the raw row: it ignores a stored profile that
    belongs to another person, omits a uuid that another person's row also
    carries, and takes the account uuid from the stored profile. A field
    counts here only when the block carries it, so a field the block will
    never write can never cause a republish loop.
    """
    diverged = []
    if _casefold(config.get("emailAddress")) != _casefold(getattr(block, "email", None)):
        diverged.append("emailAddress")
    block_org = _strip(getattr(block, "organization_uuid", None))
    if block_org is not None and _strip(config.get("organizationUuid")) != block_org:
        diverged.append("organizationUuid")
    block_uuid = _text(getattr(block, "account_uuid", None))
    if block_uuid is not None and config.get("accountUuid") != block_uuid:
        diverged.append("accountUuid")
    fields = getattr(block, "fields", None)
    created_at = _text(fields.get("accountCreatedAt")) if isinstance(fields, dict) else None
    if created_at is not None and config.get("accountCreatedAt") != created_at:
        diverged.append("accountCreatedAt")
    return diverged


def _row_for_identity(db, email: Any, organization_uuid: Any) -> int | None:
    """Return the id of the one Claude row with this email (and org), if any."""
    wanted = _casefold(email)
    if not wanted:
        return None
    try:
        rows = db.list_accounts(include_inactive=True)
    except Exception:  # noqa: BLE001 - a log hint must never fail the pass
        return None
    candidates = [
        row
        for row in rows
        if _casefold(row.get("email")) == wanted
        and (row.get("provider") or "claude") == "claude"
    ]
    exact = [row for row in candidates if row.get("organization_uuid") == organization_uuid]
    chosen = exact if exact else candidates
    return int(chosen[0]["id"]) if len(chosen) == 1 else None


def _owner_matches(proof: _OwnerProof, row: dict) -> bool:
    if not proof.email or proof.email != _casefold(row.get("email")):
        return False
    row_org = _text(row.get("organization_uuid"))
    if row_org is not None and proof.organization_uuid != row_org:
        return False
    row_uuid = _text(row.get("account_uuid"))
    return row_uuid is None or proof.account_uuid == row_uuid


def _status_name(result: Any) -> str:
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status))


def _config_mtime(home: Path) -> str:
    try:
        return f"{(home / '.claude.json').stat().st_mtime:.0f}"
    except OSError:
        return "unknown"


class IdentityWatcher:
    """Republish the Claude config identity when it diverges from the authority.

    Every collaborator is injectable for tests. ``resolve(home)`` returns an
    :class:`AuthorityView` or None. ``profile_lookup(token)`` returns an
    object with ``status_code`` and ``profile``. ``lease_factory(home)``
    returns a switch lease. ``writer``, ``build`` and ``read_config`` follow
    the ``jacked.api.claude_identity`` contracts.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        profile_lookup: Callable[[str], Any] | None = None,
        lease_factory: Callable[[Path], Any] | None = None,
        resolve: Callable[[Path], AuthorityView | None] | None = None,
        writer: Callable[..., Any] | None = None,
        build: Callable[..., Any] | None = None,
        read_config: Callable[[Path], dict | None] | None = None,
    ) -> None:
        self._clock = clock
        self._profile_lookup = profile_lookup or _default_profile_lookup
        self._lease_factory = lease_factory or _default_lease
        self._resolve = resolve or _read_authority_view
        self._writer = writer or _default_writer
        self._build = build or _default_build
        self._read_config = read_config or _default_read_config
        self._run_lock = threading.Lock()
        self._evidence_lock = threading.Lock()
        self._evidence: list[str] = []
        self._states: dict[int, _AccountState] = {}
        self._owner_cache: OrderedDict[str, _OwnerProof] = OrderedDict()

    def drain_evidence(self) -> tuple[str, ...]:
        """Return and clear the evidence of the previous runs, in order."""
        with self._evidence_lock:
            drained = tuple(dict.fromkeys(self._evidence))
            self._evidence.clear()
        return drained

    def run(self, update: SnapshotUpdate, db, home: Path) -> None:
        """Check one published snapshot on the caller thread.

        A call while another run is in flight returns at once.
        """
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            self._run(update, db, Path(home))
        finally:
            self._run_lock.release()

    def submit(self, update: SnapshotUpdate, db, home: Path) -> bool:
        """Check one published snapshot on a daemon worker thread.

        Return True when a worker started. Return False when a run is still
        in flight; the next snapshot gets the next check. The worker is a
        daemon, so it never holds up the shutdown of the service. Its
        evidence is held for :meth:`drain_evidence`.
        """
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            worker = threading.Thread(
                target=self._work,
                args=(update, db, Path(home)),
                name="jacked-identity-watcher",
                daemon=True,
            )
            worker.start()
        except BaseException:
            self._run_lock.release()
            raise
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no run is in flight. Return False on a timeout."""
        acquired = self._run_lock.acquire(timeout=-1 if timeout is None else timeout)
        if acquired:
            self._run_lock.release()
        return acquired

    # -- internals -----------------------------------------------------------

    def _work(self, update: SnapshotUpdate, db, home: Path) -> None:
        """Worker body: run one check, then free the worker slot."""
        try:
            self._run(update, db, home)
        except Exception:  # noqa: BLE001 - a worker error must never escape
            logger.warning("Claude config identity watcher failed", exc_info=True)
        finally:
            self._run_lock.release()

    def _emit(self, *items: str) -> None:
        with self._evidence_lock:
            self._evidence.extend(items)

    def _transition(
        self,
        account_id: int,
        state: _AccountState,
        reason: str,
        *,
        level: int = logging.INFO,
    ) -> None:
        """Log a skip reason only when it differs from the previous one."""
        previous = state.last_reason
        state.last_reason = reason
        if reason == previous or (previous is None and reason == "in-sync"):
            return
        logger.log(
            level,
            "Claude config identity for account %d: %s",
            account_id,
            reason,
        )

    def _run(self, update: SnapshotUpdate, db, home: Path) -> None:
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            return
        if update.scope != "global" or update.state is not ResolverState.RESOLVED:
            return
        observed = update.observed
        if db is None or observed is None or observed.account_id is None:
            return
        account_id = int(observed.account_id)
        row = db.get_account(account_id)
        if (
            not row
            or row.get("is_deleted")
            or (row.get("provider") or "claude") != "claude"
        ):
            return
        config = self._read_config(home)
        if not isinstance(config, dict):
            return
        state = self._states.setdefault(account_id, _AccountState())
        if not _diverged_fields(row, config):
            self._transition(account_id, state, "in-sync")
            return
        now = self._clock()
        if (
            state.last_attempt is not None
            and now - state.last_attempt < ATTEMPT_MIN_INTERVAL_SECONDS
        ):
            return
        # The raw check only triggers. The block decides, because the block is
        # what a write would put in the config. A matching block stamps no
        # floor; its verdict is reused while the row and config stay the same,
        # so the builder does not run (and log) on every pass.
        settle_key = _settle_key(row, config)
        if (
            state.settled is not None
            and state.settled[0] == settle_key
            and now - state.settled[1] < SETTLED_TTL_SECONDS
        ):
            return
        state.settled = None
        block = self._build(row, db=db)
        if block is None:
            self._transition(account_id, state, "no-block")
            return
        diverged = _block_diverged_fields(block, config)
        if not diverged:
            state.settled = (settle_key, now)
            self._transition(account_id, state, "in-sync")
            return
        # Stamped at the attempt: a failure never shortens the floor.
        state.last_attempt = now
        self._attempt(update, db, home, row, config, diverged, state, now)

    def _attempt(
        self,
        update: SnapshotUpdate,
        db,
        home: Path,
        row: dict,
        config: dict,
        diverged: list[str],
        state: _AccountState,
        now: float,
    ) -> None:
        account_id = int(row["id"])
        view = self._resolve(home)
        if view is None or view.account_id != account_id:
            self._transition(account_id, state, "authority-changed")
            return

        desired_id = update.desired.account_id if update.desired is not None else None
        key = (view.digest, desired_id)
        if state.breaker_key != key:
            state.breaker_key = key
            state.republishes.clear()
            state.tripped = False
        state.republishes = [
            at for at in state.republishes if now - at < BREAKER_WINDOW_SECONDS
        ]
        if not state.tripped and len(state.republishes) >= BREAKER_MAX_REPUBLISHES:
            state.tripped = True
            logger.warning(
                "Claude config identity for account %d diverged again after %d "
                "republishes in %d minutes; jacked stops writing it until the "
                "authority or the desired account changes",
                account_id,
                len(state.republishes),
                int(BREAKER_WINDOW_SECONDS // 60),
            )
        if state.tripped:
            self._emit(EVIDENCE_FLAPPING)
            state.last_reason = "flapping"
            return

        proof = self._prove_owner(view, now)
        if proof is None:
            self._emit(EVIDENCE_OWNER_UNPROVEN)
            self._transition(account_id, state, "owner-unproven")
            return
        if not _owner_matches(proof, row):
            self._emit(EVIDENCE_STAMP_MISMATCH)
            if state.last_reason != "stamp-mismatch":
                logger.warning(
                    "The credential authority is stamped for account %d, but its "
                    "token belongs to account %s. jacked will not write the Claude "
                    "config identity.",
                    account_id,
                    _row_for_identity(db, proof.email, proof.organization_uuid)
                    or "unknown",
                )
            state.last_reason = "stamp-mismatch"
            return

        with self._lease_factory(home).acquire() as acquired:
            if not acquired:
                self._emit(EVIDENCE_DEFERRED_SWITCH)
                self._transition(account_id, state, "deferred-switch")
                return
            fresh = self._resolve(home)
            if (
                fresh is None
                or fresh.digest != view.digest
                or fresh.account_id != account_id
                or _desired_account_id(db) != desired_id
            ):
                self._transition(account_id, state, "changed-under-lease")
                return
            fresh_row = db.get_account(account_id)
            if not fresh_row or not _owner_matches(proof, fresh_row):
                self._transition(account_id, state, "changed-under-lease")
                return
            block = self._build(fresh_row, db=db)
            if block is None:
                self._transition(account_id, state, "no-block")
                return
            diverged = _block_diverged_fields(block, config)
            if not diverged:
                self._transition(account_id, state, "in-sync")
                return
            mtime = _config_mtime(home)
            try:
                result = self._writer(
                    home,
                    block,
                    expected=config,
                    lock_timeout=CONFIG_LOCK_TIMEOUT_SECONDS,
                )
                status = _status_name(result)
            except Exception:  # noqa: BLE001 - reported as divergent evidence
                logger.debug("Claude config identity write raised", exc_info=True)
                status = "error"

        if status != _LANDED:
            self._emit(EVIDENCE_DIVERGENT)
            self._transition(account_id, state, f"write-{status}")
            return
        state.republishes.append(now)
        evidence = [EVIDENCE_REPUBLISHED]
        if not getattr(block, "complete", False):
            evidence.append(EVIDENCE_PARTIAL)
        if getattr(block, "account_uuid", None) is None:
            evidence.append(EVIDENCE_UUID_UNKNOWN)
        self._emit(*evidence)
        state.last_reason = "republished"
        logger.info(
            "Republished the Claude config identity for account %d (config "
            "identity was account %s; diverged: %s; account uuid %s, config "
            "uuid %s; config mtime %s; complete %s)",
            account_id,
            _row_for_identity(
                db, config.get("emailAddress"), config.get("organizationUuid")
            )
            or "unknown",
            ", ".join(diverged),
            _prefix(getattr(block, "account_uuid", None)),
            _prefix(config.get("accountUuid")),
            mtime,
            bool(getattr(block, "complete", False)),
        )

    def _prove_owner(self, view: AuthorityView, now: float) -> _OwnerProof | None:
        """Return the cached or fresh profile proof, or None when unproven."""
        if not view.access_token:
            return None
        if view.expires_at_ms is not None and view.expires_at_ms <= now * 1000.0:
            # An expired token would only earn a 401. Never send it, and never
            # refresh it here: a refresh would spend a lineage jacked must keep.
            return None
        proof = self._owner_cache.get(view.digest)
        if proof is None or (
            proof.verdict == _VERDICT_TRANSIENT
            and now - proof.at >= TRANSIENT_RETRY_SECONDS
        ):
            proof = self._lookup(view.access_token, now)
            self._owner_cache[view.digest] = proof
            self._owner_cache.move_to_end(view.digest)
            while len(self._owner_cache) > _OWNER_CACHE_LIMIT:
                self._owner_cache.popitem(last=False)
        return proof if proof.verdict == _VERDICT_PROFILE else None

    def _lookup(self, access_token: str, now: float) -> _OwnerProof:
        try:
            result = self._profile_lookup(access_token)
        except Exception:  # noqa: BLE001 - a transient failure, retried later
            logger.debug("Owner profile lookup raised", exc_info=True)
            return _OwnerProof(_VERDICT_TRANSIENT, now)
        status = getattr(result, "status_code", None)
        profile = getattr(result, "profile", None)
        if status in (401, 403):
            return _OwnerProof(_VERDICT_UNPROVEN, now)
        if status != 200 or not isinstance(profile, dict):
            return _OwnerProof(_VERDICT_TRANSIENT, now)
        account = profile.get("account")
        organization = profile.get("organization")
        account = account if isinstance(account, dict) else {}
        organization = organization if isinstance(organization, dict) else {}
        email = _casefold(account.get("email"))
        if not email:
            return _OwnerProof(_VERDICT_UNPROVEN, now)
        return _OwnerProof(
            _VERDICT_PROFILE,
            now,
            email=email,
            organization_uuid=_text(organization.get("uuid")),
            account_uuid=_text(account.get("uuid")),
        )
