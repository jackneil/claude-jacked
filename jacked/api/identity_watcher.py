"""Keep the Claude Code config identity true to the credential authority.

Claude Code merges a profile into ``~/.claude.json`` ``oauthAccount`` from
whatever token a process holds. After a switch, a stale process can write the
previous account back into the config, and running sessions then key their
usage cache, consent and gating on the wrong account.

The service observer submits each published snapshot to this watcher. The
watcher runs on one long-lived daemon worker thread, so a slow profile lookup
or a held config lock never delays the observer. A submit while a check is
queued or running is skipped. When the config identity diverges from the
block jacked would publish for the account that holds the authority, the
watcher republishes that block. It writes only when all of these are true:

1. The service has no ``CLAUDE_CONFIG_DIR`` and the observation is a
   RESOLVED global observation of a Claude row.
2. The stored access token belongs to the observed row and to the block
   that would be written. One profile GET proves this. The GET never refreshes a token and never writes the
   database. The result is cached per authority payload digest.
3. The switch lease is free, and under the lease the authority digest and
   the observed and desired accounts are unchanged.
4. The config block is unchanged since the watcher read it (compare and
   swap in the writer).

The watcher makes at most one attempt per account per minute. A breaker stops
the writes when one account needs more than three republishes in fifteen
minutes. A tripped breaker, an unproven owner and a stamp mismatch stay in
every snapshot while they last, a divergence older than five minutes logs a
WARNING, and ``status()`` reports each account for diagnostics. Logs carry row ids, 8-character uuid prefixes and field names only,
never emails or tokens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
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
# A divergence that survives this long gets one WARNING, so a watcher that
# cannot repair (owner unproven, breaker tripped, write locked) is visible.
LONG_DIVERGENCE_SECONDS = 5 * 60.0
_OWNER_CACHE_LIMIT = 32
# The account row columns that decide the published block. A change in any
# other column (usage caches, timestamps) does not invalidate a verdict.
IDENTITY_COLUMNS = (
    "id",
    "provider",
    "email",
    "organization_uuid",
    "organization_name",
    "account_uuid",
    "profile_json",
    "profile_fetched_at",
    "rate_limit_tier",
)

EVIDENCE_PREFIX = "claude-config:identity:"
EVIDENCE_REPUBLISHED = f"{EVIDENCE_PREFIX}republished"
EVIDENCE_PARTIAL = f"{EVIDENCE_PREFIX}partial"
EVIDENCE_UUID_UNKNOWN = f"{EVIDENCE_PREFIX}uuid-unknown"
EVIDENCE_DIVERGENT = f"{EVIDENCE_PREFIX}divergent"
EVIDENCE_FLAPPING = f"{EVIDENCE_PREFIX}flapping"
EVIDENCE_OWNER_UNPROVEN = f"{EVIDENCE_PREFIX}owner-unproven"
EVIDENCE_DEFERRED_SWITCH = f"{EVIDENCE_PREFIX}deferred-switch"
EVIDENCE_STAMP_MISMATCH = "authority:stamp-mismatch"
EVIDENCE_DISABLED = f"{EVIDENCE_PREFIX}disabled"
# Reasons that stay in every snapshot while they last. One-shot evidence
# alone vanishes from the snapshot ten seconds later.
_STICKY_EVIDENCE = {
    "flapping": EVIDENCE_FLAPPING,
    "stamp-mismatch": EVIDENCE_STAMP_MISMATCH,
    "owner-unproven": EVIDENCE_OWNER_UNPROVEN,
}

_LANDED = "written"
# A write that landed and was then reverted counts as a republish for the
# breaker: that is exactly the flapping case.
_COUNTED_STATUSES = frozenset({"written", "post_verify_mismatch"})

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
    reason_since: float | None = None
    # (sha256 of the identity columns and the config, when) for a block
    # that matched.
    settled: tuple[str, float] | None = None
    divergent_since: float | None = None
    long_divergence_warned: bool = False
    last_republish_at: float | None = None


def _identity_row(row: dict) -> dict:
    """Return only the columns that decide the published block."""
    return {key: row.get(key) for key in IDENTITY_COLUMNS}


def _settle_key(row: dict, config: dict) -> str:
    """Digest the identity columns and the config. Only the digest is kept."""
    raw = json.dumps([_identity_row(row), config], sort_keys=True, default=str)
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


def _strip(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _block_diverged_fields(block: Any, config: dict) -> list[str]:
    """Name the config identity fields that disagree with the block.

    The block is what a write would put in the config, so it is the only
    trigger. A field counts only when the block carries it, so a field the
    block will never write can never cause a republish loop.
    ``accountCreatedAt`` is fixed for one person and
    ``subscriptionCreatedAt`` for one organization subscription: a
    different value proves that another account's profile was merged in,
    and a missing value means Claude Code will re-run its unguarded merge.
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
    fields = fields if isinstance(fields, dict) else {}
    for key in ("accountCreatedAt", "subscriptionCreatedAt"):
        value = _text(fields.get(key))
        if value is not None and config.get(key) != value:
            diverged.append(key)
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


def _proof_matches(proof: _OwnerProof, email: Any, org: Any, account_uuid: Any) -> bool:
    if not proof.email or proof.email != _casefold(email):
        return False
    wanted_org = _strip(org)
    if wanted_org is not None and _strip(proof.organization_uuid) != wanted_org:
        return False
    wanted_uuid = _text(account_uuid)
    return wanted_uuid is None or proof.account_uuid == wanted_uuid


def _owner_matches(proof: _OwnerProof, row: dict, block: Any = None) -> bool:
    """True when the token's profile owns the row AND the block to write.

    The block can carry an org or a uuid that the row does not (both come
    from the stored profile), so the proof is checked against both.
    """
    if not _proof_matches(
        proof, row.get("email"), row.get("organization_uuid"), row.get("account_uuid")
    ):
        return False
    if block is None:
        return True
    return _proof_matches(
        proof,
        getattr(block, "email", None),
        getattr(block, "organization_uuid", None),
        getattr(block, "account_uuid", None),
    )


def _status_name(result: Any) -> str:
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status))


def _config_mtime(home: Path) -> str:
    from jacked.api.claude_config_store import claude_config_path

    path = claude_config_path(home)
    try:
        return f"{path.stat().st_mtime:.0f}" if path is not None else "unknown"
    except OSError:
        return "unknown"


_DISABLED_WARNED = False


def _warn_disabled_once() -> None:
    """Log once per process that ``CLAUDE_CONFIG_DIR`` disables the watcher."""
    global _DISABLED_WARNED
    if _DISABLED_WARNED:
        return
    _DISABLED_WARNED = True
    logger.warning(
        "The Claude config identity watcher is disabled because CLAUDE_CONFIG_DIR "
        "is set in the jacked service environment; a stale Claude Code process "
        "can write a previous account into the config and jacked will not repair it"
    )


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
        self._disabled = False
        # One long-lived daemon worker with a queue of one: a submit while a
        # check is queued or running is skipped.
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._worker: threading.Thread | None = None
        self._worker_guard = threading.Lock()

    def drain_evidence(self) -> tuple[str, ...]:
        """Return the evidence for the next snapshot, in order.

        One-shot evidence of the previous runs is cleared. Sticky evidence
        (a tripped breaker, a stamp mismatch, an unproven owner, a disabled
        watcher) is returned again on every call while it lasts.
        """
        with self._evidence_lock:
            items = list(self._evidence)
            self._evidence.clear()
        if self._disabled:
            items.append(EVIDENCE_DISABLED)
        for state in list(self._states.values()):
            sticky = _STICKY_EVIDENCE.get(state.last_reason or "")
            if sticky:
                items.append(sticky)
        return tuple(dict.fromkeys(items))

    def status(self) -> dict:
        """Return a values-free status for diagnostics.

        Each account entry carries the last reason, how long it has held,
        the time of the last republish and whether the breaker is tripped.
        """
        now = self._clock()
        accounts = {}
        for account_id, state in sorted(self._states.items()):
            accounts[str(account_id)] = {
                "reason": state.last_reason,
                "reason_for_seconds": (
                    round(now - state.reason_since) if state.reason_since else None
                ),
                "divergent_for_seconds": (
                    round(now - state.divergent_since) if state.divergent_since else None
                ),
                "last_republish_at": state.last_republish_at,
                "breaker_tripped": state.tripped,
            }
        return {"disabled": self._disabled, "accounts": accounts}

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
        """Queue one published snapshot for the daemon worker.

        Return True when the check was queued. Return False when a check is
        still queued or running; the next snapshot gets the next check. The
        worker is a daemon, so it never holds up the shutdown of the service.
        Its evidence is held for :meth:`drain_evidence`.
        """
        self._ensure_worker()
        if self._run_lock.locked():
            return False
        try:
            self._queue.put_nowait((update, db, Path(home)))
        except queue.Full:
            return False
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no check is queued or running. Return False on a timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._queue.mutex:
                if self._queue.unfinished_tasks == 0:
                    return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # -- internals -----------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._worker_guard:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, name="jacked-identity-watcher", daemon=True
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            update, db, home = self._queue.get()
            try:
                with self._run_lock:
                    self._run(update, db, home)
            except Exception:  # noqa: BLE001 - a worker error must never escape
                logger.warning("Claude config identity watcher failed", exc_info=True)
            finally:
                self._queue.task_done()

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
        """Record a reason; log it only when it differs from the previous one."""
        previous = state.last_reason
        state.last_reason = reason
        if reason == previous:
            return
        state.reason_since = self._clock()
        if previous is None and reason == "in-sync":
            return
        logger.log(
            level,
            "Claude config identity for account %d: %s",
            account_id,
            reason,
        )

    def _mark_in_sync(self, account_id: int, state: _AccountState) -> None:
        state.divergent_since = None
        state.long_divergence_warned = False
        self._transition(account_id, state, "in-sync")

    def _note_divergence(
        self, account_id: int, state: _AccountState, diverged: list[str], now: float
    ) -> None:
        if state.divergent_since is None:
            state.divergent_since = now
            return
        if (
            not state.long_divergence_warned
            and now - state.divergent_since >= LONG_DIVERGENCE_SECONDS
        ):
            state.long_divergence_warned = True
            logger.warning(
                "Claude config identity for account %d has diverged for %d minutes "
                "(fields: %s; last reason: %s)",
                account_id,
                int((now - state.divergent_since) // 60),
                ", ".join(diverged),
                state.last_reason or "none",
            )

    def _run(self, update: SnapshotUpdate, db, home: Path) -> None:
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            self._disabled = True
            _warn_disabled_once()
            return
        self._disabled = False
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
        now = self._clock()
        # A matching verdict is reused while the identity columns and the
        # config stay the same, so the builder does not run (and log) on
        # every pass.
        settle_key = _settle_key(row, config)
        if (
            state.settled is not None
            and state.settled[0] == settle_key
            and now - state.settled[1] < SETTLED_TTL_SECONDS
        ):
            return
        if (
            state.last_attempt is not None
            and now - state.last_attempt < ATTEMPT_MIN_INTERVAL_SECONDS
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
            self._mark_in_sync(account_id, state)
            return
        self._note_divergence(account_id, state, diverged, now)
        # Stamped at the attempt: a failure never shortens the floor.
        state.last_attempt = now
        self._attempt(update, db, home, row, block, config, diverged, state, now)

    def _check_breaker(
        self, account_id: int, state: _AccountState, key: tuple, now: float
    ) -> bool:
        """Return True when the breaker is tripped for this authority."""
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
        return state.tripped

    def _attempt(
        self,
        update: SnapshotUpdate,
        db,
        home: Path,
        row: dict,
        block: Any,
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
        if self._check_breaker(account_id, state, (view.digest, desired_id), now):
            self._emit(EVIDENCE_FLAPPING)
            # The breaker already logged its one WARNING.
            self._transition(account_id, state, "flapping")
            return

        proof = self._prove_owner(view, now)
        if proof is None:
            self._emit(EVIDENCE_OWNER_UNPROVEN)
            self._transition(account_id, state, "owner-unproven")
            return
        if not _owner_matches(proof, row, block):
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
            self._transition(account_id, state, "stamp-mismatch")
            return

        # Everything slow happened above. Under the lease only the authority
        # digest, the desired account and the identity columns are checked
        # again, and then the config is written, so a user switch that lands
        # meanwhile is refused for as short a time as possible.
        identity = _identity_row(row)
        with self._lease_factory(home).acquire() as acquired:
            if not acquired:
                self._emit(EVIDENCE_DEFERRED_SWITCH)
                self._transition(account_id, state, "deferred-switch")
                return
            fresh = self._resolve(home)
            fresh_row = db.get_account(account_id)
            if (
                fresh is None
                or fresh.digest != view.digest
                or fresh.account_id != account_id
                or _desired_account_id(db) != desired_id
                or not fresh_row
                or _identity_row(fresh_row) != identity
            ):
                self._transition(account_id, state, "changed-under-lease")
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
            except Exception as exc:  # noqa: BLE001 - reported as divergent evidence
                logger.warning(
                    "Claude config identity write for account %d raised %s",
                    account_id,
                    type(exc).__name__,
                )
                status = "error"

        if status in _COUNTED_STATUSES:
            state.republishes.append(now)
        if status != _LANDED:
            self._emit(EVIDENCE_DIVERGENT)
            self._transition(account_id, state, f"write-{status}")
            return
        state.last_republish_at = now
        evidence = [EVIDENCE_REPUBLISHED]
        if not getattr(block, "complete", False):
            evidence.append(EVIDENCE_PARTIAL)
        if getattr(block, "account_uuid", None) is None:
            evidence.append(EVIDENCE_UUID_UNKNOWN)
        self._emit(*evidence)
        state.last_reason = "republished"
        state.reason_since = now
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
        if status == 401:
            # Only a 401 proves the token is rejected. A 403 can be a
            # transient or scope problem, so it is retried like a 5xx.
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
