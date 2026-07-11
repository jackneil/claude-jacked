"""Runner for the agent-reach external integration.

Single implementation behind both the ``jacked reach`` CLI (Milestone 3) and the
API routes. Every install path is locked to the vendored pin (see
:mod:`jacked.integrations.pinfile`): a pinned commit SHA, a fully-pinned
constraints file, and post-install hash verification of the skill files. Nothing
resolves at install time and ``--safe`` is never omitted, so a poisoned upstream
release or transitive dep cannot reach the machine.

Break-glass override persistence is *injected* (get_setting/set_setting
callables; this module never imports the DB). Subprocess discipline: arg lists
only, a timeout on every call, explicit failure checks; the post-install hash
verify proves the install landed intact (exit codes are never trusted alone).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from jacked.integrations._util import (
    ReachUserError,
    channels_status,
    configure_hint,
    format_drift,
    hash_preflight,
    parse_uv_version,
    run_doctor,
    stderr_excerpt,
    truthy,
    verify_skill_hashes,
)
from jacked.integrations.pinfile import ChannelBackend, PinFile, load_pin
from jacked.integrations import override as reach_override
from jacked.integrations import rules as reach_rules
from jacked.integrations import state as reach_state
from jacked.integrations import upstream as reach_upstream
from jacked.winproc import NO_WINDOW

# Re-exported so system._PROTECTED_SETTING_KEYS and the runner share one
# definition; the override machinery is the single source (see override.py).
from jacked.integrations.override import (  # noqa: F401
    OVERRIDE_CONSTRAINTS_NAME,
    SETTING_OVERRIDE_ACK,
    SETTING_OVERRIDE_AT,
    SETTING_OVERRIDE_SHA,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

#: uv >= this is required: `uv tool install -c/--constraints` on a git+SHA source.
MIN_UV_VERSION = (0, 5, 0)

# subprocess timeouts (seconds)
LOCAL_TIMEOUT = 30
NETWORK_TIMEOUT = 120

#: Skill dirs (relative to home) upstream's installer writes into; jacked
#: hash-verifies every one that exists after install, and rollback/remove delete
#: the same set — including OpenClaw's, or a tampered skill would survive there.
_SKILL_DIR_RELPATHS = (
    Path(".claude") / "skills" / "agent-reach",
    Path(".agents") / "skills" / "agent-reach",
    Path(".openclaw") / "skills" / "agent-reach",
)

SettingGetter = Callable[[str], Optional[str]]
SettingSetter = Callable[[str, Optional[str]], None]


class AgentReachRunner:
    """Install/status/update/remove/override operations for agent-reach.

    ``get_setting``/``set_setting`` are required: break-glass override state must
    persist through a real accessor (the API owns the DB; the CLI passes DB
    accessors in Milestone 3). A silent no-op fallback would drop override state,
    so there is no default -- callers must inject persistence.
    """

    def __init__(
        self,
        get_setting: SettingGetter,
        set_setting: SettingSetter,
        *,
        pin_path: Path | str | None = None,
        home: Path | str | None = None,
    ) -> None:
        self._get_setting = get_setting
        self._set_setting = set_setting
        self._pin_path = pin_path
        # Track whether home was injected: only then do we scope Codex under it
        # (tests/isolation). A real run leaves Codex to its own $CODEX_HOME/~.
        self._codex_scope = Path(home) if home is not None else None
        self._home = Path(home) if home is not None else Path.home()
        self._pin: PinFile | None = None

    # ---- public operations ------------------------------------------------ #

    def install(self) -> dict:
        """Install at the authoritative SHA, verify skill hashes, record state.

        Order: uv gate -> pre-flight CLAUDE.md markers (fail fast before any side
        effect) -> pinned `uv tool install -c` -> `agent-reach install --safe` ->
        hash-verify -> re-pin enabled channels -> rules overlay -> write state. A
        hash mismatch rolls back (uninstall + delete skill dirs) and raises.
        """
        pin = self._load_pin()
        self._check_uv_version()
        # Fail fast on a corrupt reach-marker layout in CLAUDE.md, before the
        # network installs — the actual (fatal) rules write happens later, and a
        # late raise would leave the uv env + skill dirs written with no state.
        reach_rules.preflight_reach_rules(self._home)

        override_sha, override_active = reach_override.resolve_override(
            self._get_setting, self._set_setting, self._home, pin.commit_sha, mutate=True
        )
        if override_active:
            authoritative_sha = override_sha
            constraints = reach_override.override_constraints_path(self._home)
        else:
            authoritative_sha = pin.commit_sha
            constraints = pin.constraints_path()
        if not constraints.is_file():
            raise RuntimeError(
                f"constraints file not found: {constraints} "
                f"({'override' if override_active else 'shipped pin'})"
            )

        logger.info(
            "reach install: %s at %s (constraints=%s)",
            "UNVETTED override" if override_active else "vetted pin",
            authoritative_sha[:12],
            constraints.name,
        )
        # Enforce the vendored ARTIFACT hashes before the real install: uv tool
        # install ignores constraint-file hashes, so a `--require-hashes`
        # pre-flight is what actually blocks a poisoned same-version PyPI wheel.
        hash_preflight(constraints, self._run)

        uv = self._require_bin("uv")
        git_url = f"agent-reach @ git+{pin.upstream}@{authoritative_sha}"
        # --force so update() deterministically overwrites an existing install
        # when the authoritative SHA changes; content stays pin+constraints-locked.
        self._run(
            [uv, "tool", "install", "--force", "-c", str(constraints), git_url],
            timeout=NETWORK_TIMEOUT,
        )
        logger.info("reach install: uv tool install complete; running agent-reach --safe")

        # --safe is NON-NEGOTIABLE: default mode curl|bashes NodeSource, writes apt
        # keyrings, and npm -g installs unprompted. Never build a path without it.
        self._run(
            [self._agent_reach_bin(), "install", "--safe", "--env=auto"],
            timeout=NETWORK_TIMEOUT,
        )

        verified, drift = self._verify_skill_hashes(pin)
        if not verified:
            logger.warning("reach install: skill hash mismatch, rolling back")
            self._rollback()
            raise RuntimeError(
                "agent-reach skill file hash mismatch after install "
                "(possible tampering) -- rolled back. Details: " + format_drift(drift)
            )
        logger.info("reach install: skill hashes verified against the pin")

        # Re-pin enabled channels so their backends follow a pin bump to the vetted
        # versions (no-op on a fresh install).
        self._reinstall_channels(pin)

        # Rules overlay AFTER hash verification. Claude side is fatal (pre-flight
        # already ruled out a corrupt layout); Codex side is best-effort.
        rules_action = reach_rules.install_reach_rules(self._home)
        codex_action = reach_rules.install_reach_rules_codex(self._codex_scope)
        logger.info("reach install: rules overlay claude=%s codex=%s", rules_action, codex_action)

        self._write_state(
            installed_sha=authoritative_sha,
            override_active=override_active,
            channels_enabled=self._existing_channels(),
        )
        logger.info("reach install: complete, state written")
        return {
            "installed": True,
            "installed_sha": authoritative_sha,
            "override_active": override_active,
            "skill_hashes_verified": True,
            "rules": rules_action,
            "codex_rules": codex_action,
        }

    def status(self) -> dict:
        """Full status dict. A PURE READ: never writes or unlinks (override
        auto-clear happens only in mutating paths), so a dashboard poll cannot
        mutate state. Outbound only: `agent-reach doctor` + cached upstream check."""
        pin = self._load_pin()
        state = self._read_state()
        installed = state is not None
        installed_sha = state.get("installed_sha") if state else None

        # mutate=False: a read must not clear the override even when it equals the
        # shipped pin; the next install/update performs the cleanup.
        override_sha, override_active = reach_override.resolve_override(
            self._get_setting, self._set_setting, self._home, pin.commit_sha, mutate=False
        )
        authoritative_sha = override_sha if override_active else pin.commit_sha
        sha_matches = installed and installed_sha == authoritative_sha

        drift: list[dict] = []
        if installed:
            _ok, drift = self._verify_skill_hashes(pin)

        doctor, doctor_error = run_doctor()

        return {
            "installed": installed,
            "installed_sha": installed_sha,
            "pin_sha": pin.commit_sha,
            "authoritative_sha": authoritative_sha,
            "sha_matches": sha_matches,
            "drift": drift,
            "override": {
                "active": override_active,
                "sha": override_sha,
                "ack": truthy(self._get_setting(SETTING_OVERRIDE_ACK)),
                "at": self._get_setting(SETTING_OVERRIDE_AT),
            },
            "doctor": doctor,
            "doctor_error": doctor_error,
            "channels": channels_status(pin, self._existing_channels()),
            "upstream_check": reach_upstream.check_upstream(
                pin.commit_sha, pin.upstream, now=time.time()
            ),
            "pin": {
                "version_label": pin.version_label,
                "vetted_at": pin.vetted_at,
                "short_sha": pin.short_sha,
            },
        }

    def update(self) -> dict:
        """Re-install at the currently-authoritative SHA (override if active, else shipped pin)."""
        return self.install()

    def enable_channel(self, name: str) -> str:
        """Install a channel's pinned backends; return upstream's configure hint.

        jacked installs only the vetted, pinned backends -- never a freestyle
        `npm i -g`/`pipx install`. It does NOT run the cookie/login config; it
        returns the hint text for the user or agent to run.
        """
        pin = self._load_pin()
        channel = pin.channels.get(name)
        if channel is None:
            valid = ", ".join(sorted(pin.channels)) or "(none)"
            raise ReachUserError(f"unknown channel {name!r}; valid channels: {valid}")

        if self._read_state() is None:
            raise ReachUserError(
                "agent-reach is not installed; run 'jacked reach install' before enabling channels"
            )

        installed_specs: list[str] = []
        for backend in channel.backends:
            if not backend.is_installable:
                logger.info("channel %s: skipping manual backend (%s)", name, backend.note)
                continue
            self._run(self._channel_install_cmd(backend), timeout=NETWORK_TIMEOUT)
            installed_specs.append(backend.spec)

        self._record_channel(name, installed_specs)
        logger.info("reach: channel %s enabled (%s)", name, ", ".join(installed_specs) or "no-op")
        return configure_hint(name)

    def remove(self) -> dict:
        """Uninstall, delete skill dirs, strip rules, clear override; verify residue.

        Tolerant of a partial/absent install. Does NOT trust the uninstall exit
        code: deletes the skill dirs itself (they outlive the binary after a bare
        `uv tool uninstall`) and VERIFIES no residue remains, returning any leftover
        so the caller reports honestly instead of a blanket "removed".
        """
        ar = shutil.which("agent-reach")
        if ar:
            self._run([ar, "uninstall"], timeout=NETWORK_TIMEOUT, check=False)
        uv = shutil.which("uv")
        if uv:
            self._run([uv, "tool", "uninstall", "agent-reach"], timeout=LOCAL_TIMEOUT, check=False)
        # Delete the skill dirs ourselves (V5 prompt-injection surface): upstream's
        # uninstall only runs when its binary is present, and never touches these
        # after a bare `uv tool uninstall`. A tampered SKILL.md must not survive.
        for rel in _SKILL_DIR_RELPATHS:
            skill_dir = self._home / rel
            if skill_dir.is_dir():
                shutil.rmtree(skill_dir, ignore_errors=True)
        # Strip jacked's reach rules overlay from CLAUDE.md + Codex AGENTS.md.
        # Both are tolerant of absence; the Codex side never raises.
        reach_rules.remove_reach_rules(self._home)
        reach_rules.remove_reach_rules_codex(self._codex_scope)
        self._delete_state()
        # Clear override AFTER state is gone so it does not trigger a reinstall.
        reach_override.clear_override_state(self._set_setting, self._home)

        # Verify the destructive op actually landed; report residue, don't assume.
        residue = [str(self._home / rel) for rel in _SKILL_DIR_RELPATHS if (self._home / rel).exists()]
        if shutil.which("agent-reach"):
            residue.append("agent-reach binary still on PATH")
        if residue:
            logger.warning("reach remove: residue remains: %s", residue)
        else:
            logger.info("reach remove: complete, no residue")
        return {"removed": True, "residue": residue}

    def set_override(self, ref: str, *, ack: bool) -> dict:
        """Break-glass: resolve ref->SHA, compile fresh (unvetted) constraints, persist.

        Requires explicit ``ack=True``. A raw 40-hex SHA is accepted verbatim;
        any other ref (branch, lightweight or annotated tag) is resolved via
        ``git ls-remote``. Delegates to :mod:`jacked.integrations.override`.
        """
        pin = self._load_pin()
        return reach_override.set_override(
            ref,
            ack=ack,
            upstream=pin.upstream,
            get_setting=self._get_setting,
            set_setting=self._set_setting,
            home=self._home,
            run=self._run,
            require_bin=self._require_bin,
        )

    def clear_override(self) -> None:
        """Clear override persistence; reinstall at the shipped pin if installed."""
        reach_override.clear_override_state(self._set_setting, self._home)
        if self._read_state() is not None:
            self.update()

    # ---- pin ------------------------------------------------------------- #

    def _load_pin(self) -> PinFile:
        if self._pin is None:
            self._pin = load_pin(self._pin_path)
        return self._pin

    # State-file I/O lives in jacked.integrations.state; these are thin adapters
    # binding the runner's home so the call sites stay readable.
    def _read_state(self) -> dict | None:
        return reach_state.read_state(self._home)

    def _existing_channels(self) -> list[str]:
        return reach_state.existing_channels(self._home)

    def _write_state(self, *, installed_sha: str, override_active: bool, channels_enabled: list[str]) -> None:
        reach_state.write_state(
            self._home, installed_sha=installed_sha,
            override_active=override_active, channels_enabled=channels_enabled,
        )

    def _delete_state(self) -> None:
        reach_state.delete_state(self._home)

    def _record_channel(self, name: str, specs: list[str]) -> None:
        reach_state.record_channel(self._home, name, specs)

    # ---- channels --------------------------------------------------------- #

    def _reinstall_channels(self, pin: PinFile) -> None:
        """Re-install pinned backends for every enabled channel so a pin bump
        carries them to the new vetted versions (else they stay at first-enable)."""
        for name in self._existing_channels():
            channel = pin.channels.get(name)
            if channel is None:
                logger.warning("reach: enabled channel %s not in current pin; skipping re-pin", name)
                continue
            specs: list[str] = []
            for backend in channel.backends:
                if not backend.is_installable:
                    continue
                self._run(self._channel_install_cmd(backend), timeout=NETWORK_TIMEOUT)
                specs.append(backend.spec)
            if specs:
                self._record_channel(name, specs)
                logger.info("reach: re-pinned channel %s (%s)", name, ", ".join(specs))

    def _channel_install_cmd(self, backend: ChannelBackend) -> list[str]:
        # `--` terminates options so a spec can never be reparsed as a flag
        # (defense-in-depth; pinfile already rejects a dash-leading spec).
        if backend.kind == "npm":
            npm = shutil.which("npm") or "npm"
            return [npm, "install", "-g", "--", backend.spec]
        if backend.kind in ("pipx", "pipx-git"):
            # uv only -- never pipx/pip, per the supply-chain posture.
            return [self._require_bin("uv"), "tool", "install", "--", backend.spec]
        raise RuntimeError(f"unsupported channel backend kind: {backend.kind!r}")

    # ---- skill hash verification ------------------------------------------ #

    def _verify_skill_hashes(self, pin: PinFile) -> tuple[bool, list[dict]]:
        return verify_skill_hashes(self._home, _SKILL_DIR_RELPATHS, pin.skill_hashes)

    def _rollback(self) -> None:
        """Undo a failed/tampered install: uninstall the tool, delete skill dirs."""
        uv = shutil.which("uv")
        if uv:
            self._run([uv, "tool", "uninstall", "agent-reach"], timeout=LOCAL_TIMEOUT, check=False)
        for rel in _SKILL_DIR_RELPATHS:
            skill_dir = self._home / rel
            if skill_dir.is_dir():
                shutil.rmtree(skill_dir, ignore_errors=True)
        remaining = [str(self._home / rel) for rel in _SKILL_DIR_RELPATHS if (self._home / rel).is_dir()]
        if remaining:
            logger.warning("reach rollback: skill dirs could not be removed: %s", remaining)
        else:
            logger.info("reach rollback: tool uninstalled and skill dirs removed")

    # ---- subprocess plumbing ---------------------------------------------- #

    def _require_bin(self, name: str) -> str:
        found = shutil.which(name)
        if not found:
            raise RuntimeError(f"{name} is not installed or not on PATH")
        return found

    def _agent_reach_bin(self) -> str:
        # After `uv tool install` this is on PATH; fall back to the bare name so a
        # PATH-refresh lag surfaces as a clear "could not run" error, not a crash.
        return shutil.which("agent-reach") or "agent-reach"

    def _check_uv_version(self) -> tuple[int, int, int]:
        uv = self._require_bin("uv")
        proc = self._run([uv, "--version"], timeout=LOCAL_TIMEOUT)
        version = parse_uv_version(proc.stdout)
        if version is None:
            raise RuntimeError(
                f"could not parse uv version from {proc.stdout.strip()!r}; uv >= 0.5.0 required"
            )
        if version < MIN_UV_VERSION:
            found = ".".join(str(p) for p in version)
            raise RuntimeError(
                f"uv >= 0.5.0 required for pinned constraint installs; found {found}"
            )
        return version

    def _run(
        self, cmd: list[str], *, timeout: int, check: bool = True
    ) -> subprocess.CompletedProcess:
        logger.debug("agent-reach: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired as e:
            # Surface whatever the hung command printed before the timeout — the
            # single most useful clue for where a `uv tool install` stalled.
            partial = stderr_excerpt(e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr)
            logger.warning("reach: command timed out after %ss: %s; stderr: %s",
                           timeout, " ".join(cmd), partial)
            raise RuntimeError(f"command timed out after {timeout}s: {' '.join(cmd)}") from e
        except OSError as e:
            raise RuntimeError(f"could not run {cmd[0]!r}: {e}") from e
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
                f"{stderr_excerpt(proc.stderr)}"
            )
        return proc
