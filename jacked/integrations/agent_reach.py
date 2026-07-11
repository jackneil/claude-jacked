"""Runner for the agent-reach external integration.

Single implementation behind both the ``jacked reach`` CLI (Milestone 3) and the
API routes. Every install path is locked to the vendored pin (see
:mod:`jacked.integrations.pinfile`): a pinned commit SHA, a fully-pinned
constraints file, and post-install hash verification of the skill files. Nothing
resolves at install time and ``--safe`` is never omitted, so a poisoned upstream
release or transitive dep cannot reach the machine.

Break-glass override persistence is *injected* -- the runner takes
``get_setting``/``set_setting`` callables so the API layer owns the DB and the
CLI passes real DB accessors; this module never imports the DB. Subprocess
discipline: explicit arg lists (no ``shell=True``), a timeout on every call,
captured output, an explicit failure check per step; the post-install hash verify
is what proves the install landed intact (exit codes are never trusted alone).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from jacked.integrations._util import (
    atomic_write_json,
    configure_hint,
    format_drift,
    now_iso,
    parse_uv_version,
    stderr_excerpt,
    truthy,
    verify_skill_hashes,
)
from jacked.integrations.pinfile import ChannelBackend, PinFile, load_pin
from jacked.integrations import rules as reach_rules
from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

STATE_FILE_NAME = "jacked-reach-state.json"
OVERRIDE_CONSTRAINTS_NAME = "jacked-reach-override-constraints.txt"

#: uv >= this is required: `uv tool install -c/--constraints` on a git+SHA source.
MIN_UV_VERSION = (0, 5, 0)

# DB settings keys owned by the injected persistence layer.
SETTING_OVERRIDE_SHA = "reach_override_sha"
SETTING_OVERRIDE_ACK = "reach_override_ack"
SETTING_OVERRIDE_AT = "reach_override_at"

# subprocess timeouts (seconds)
LOCAL_TIMEOUT = 30
NETWORK_TIMEOUT = 120

#: Skill dirs (relative to home) upstream's installer writes into; jacked
#: hash-verifies every one that exists after install, and rollback removes the
#: same set — including OpenClaw's, or a tampered skill would survive there.
_SKILL_DIR_RELPATHS = (
    Path(".claude") / "skills" / "agent-reach",
    Path(".agents") / "skills" / "agent-reach",
    Path(".openclaw") / "skills" / "agent-reach",
)

_HEX40 = re.compile(r"[0-9a-fA-F]{40}")

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
        self._home = Path(home) if home is not None else Path.home()
        self._pin: PinFile | None = None

    # ---- public operations ------------------------------------------------ #

    def install(self) -> dict:
        """Install agent-reach at the authoritative SHA, verify skill hashes, record state.

        Order: uv-version gate -> pinned `uv tool install -c` -> `agent-reach
        install --safe --env=auto` -> hash-verify skill files -> write state.
        Any hash mismatch rolls back (uninstall + delete skill dirs) and raises.
        """
        pin = self._load_pin()
        self._check_uv_version()

        override_sha, override_active = self._resolve_override(pin)
        if override_active:
            authoritative_sha = override_sha
            constraints = self._override_constraints_path()
        else:
            authoritative_sha = pin.commit_sha
            constraints = pin.constraints_path()
        if not constraints.is_file():
            raise RuntimeError(
                f"constraints file not found: {constraints} "
                f"({'override' if override_active else 'shipped pin'})"
            )

        uv = self._require_bin("uv")
        git_url = f"agent-reach @ git+{pin.upstream}@{authoritative_sha}"
        # --force so update() deterministically overwrites an existing install
        # when the authoritative SHA changes; content stays pin+constraints-locked.
        self._run(
            [uv, "tool", "install", "--force", "-c", str(constraints), git_url],
            timeout=NETWORK_TIMEOUT,
        )

        # --safe is NON-NEGOTIABLE: default mode curl|bashes NodeSource, writes apt
        # keyrings, and npm -g installs unprompted. Never build a path without it.
        self._run(
            [self._agent_reach_bin(), "install", "--safe", "--env=auto"],
            timeout=NETWORK_TIMEOUT,
        )

        verified, drift = self._verify_skill_hashes(pin)
        if not verified:
            self._rollback()
            raise RuntimeError(
                "agent-reach skill file hash mismatch after install "
                "(possible tampering) -- rolled back. Details: " + format_drift(drift)
            )

        # Install jacked's reach rules overlay AFTER hash verification. The Claude
        # side is fatal (config integrity) and raises on a corrupt CLAUDE.md; the
        # Codex side is best-effort. Done before _write_state so a rules failure
        # leaves no state file and a retry (both are idempotent) heals it.
        reach_rules.install_reach_rules(self._home)
        reach_rules.install_reach_rules_codex()

        self._write_state(
            installed_sha=authoritative_sha,
            override_active=override_active,
            channels_enabled=self._existing_channels(),
        )
        return {
            "installed": True,
            "installed_sha": authoritative_sha,
            "override_active": override_active,
            "skill_hashes_verified": True,
        }

    def status(self) -> dict:
        """Full status dict. No network except `agent-reach doctor`'s own behavior."""
        pin = self._load_pin()
        state = self._read_state()
        installed = state is not None
        installed_sha = state.get("installed_sha") if state else None

        override_sha, override_active = self._resolve_override(pin)
        authoritative_sha = override_sha if override_active else pin.commit_sha
        sha_matches = installed and installed_sha == authoritative_sha

        drift: list[dict] = []
        if installed:
            _ok, drift = self._verify_skill_hashes(pin)

        doctor, doctor_error = self._doctor()

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
            raise RuntimeError(f"unknown channel {name!r}; valid channels: {valid}")

        if self._read_state() is None:
            raise RuntimeError(
                "agent-reach is not installed; run 'jacked reach install' before enabling channels"
            )

        for backend in channel.backends:
            if not backend.is_installable:
                logger.info("channel %s: skipping manual backend (%s)", name, backend.note)
                continue
            self._run(self._channel_install_cmd(backend), timeout=NETWORK_TIMEOUT)

        self._record_channel(name)
        return configure_hint(name)

    def remove(self) -> None:
        """Uninstall agent-reach, delete state, clear any override. Tolerant of absence."""
        ar = shutil.which("agent-reach")
        if ar:
            self._run([ar, "uninstall"], timeout=NETWORK_TIMEOUT, check=False)
        uv = shutil.which("uv")
        if uv:
            self._run([uv, "tool", "uninstall", "agent-reach"], timeout=LOCAL_TIMEOUT, check=False)
        # Strip jacked's reach rules overlay from CLAUDE.md + Codex AGENTS.md.
        # Both are tolerant of absence; the Codex side never raises.
        reach_rules.remove_reach_rules(self._home)
        reach_rules.remove_reach_rules_codex()
        self._delete_state()
        # Clear override AFTER state is gone so it does not trigger a reinstall.
        self.clear_override()

    def set_override(self, ref: str, *, ack: bool) -> dict:
        """Break-glass: resolve ref->SHA, compile fresh (unvetted) constraints, persist.

        Requires explicit ``ack=True``. A raw 40-hex SHA is accepted verbatim;
        any other ref is resolved via ``git ls-remote``.
        """
        if not ack:
            raise RuntimeError(
                "break-glass override requires explicit acknowledgement (ack=True): "
                "this installs UNVETTED upstream code"
            )
        pin = self._load_pin()
        sha = self._resolve_ref(pin.upstream, ref)
        constraints_path = self._override_constraints_path()
        self._compile_constraints(pin.upstream, sha, constraints_path)

        self._set_setting(SETTING_OVERRIDE_SHA, sha)
        self._set_setting(SETTING_OVERRIDE_ACK, "true")
        self._set_setting(SETTING_OVERRIDE_AT, now_iso())
        return {"override_sha": sha, "constraints": str(constraints_path)}

    def clear_override(self) -> None:
        """Clear override persistence; reinstall at shipped pin if installed."""
        self._clear_override_state()
        if self._read_state() is not None:
            self.update()

    # ---- pin + state ------------------------------------------------------ #

    def _load_pin(self) -> PinFile:
        if self._pin is None:
            self._pin = load_pin(self._pin_path)
        return self._pin

    def _state_path(self) -> Path:
        return self._home / ".claude" / STATE_FILE_NAME

    def _read_state(self) -> dict | None:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.warning("ignoring unreadable reach state file: %s", e)
            return None

    def _existing_channels(self) -> list[str]:
        state = self._read_state()
        channels = state.get("channels_enabled") if state else None
        return list(channels) if isinstance(channels, list) else []

    def _write_state(self, *, installed_sha: str, override_active: bool, channels_enabled: list[str]) -> None:
        state = {
            "installed_sha": installed_sha,
            "installed_at": now_iso(),
            "skill_hashes_verified": True,
            "channels_enabled": list(channels_enabled),
            "override_active": bool(override_active),
        }
        atomic_write_json(self._state_path(), state)

    def _delete_state(self) -> None:
        try:
            self._state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("could not remove reach state file: %s", e)

    def _record_channel(self, name: str) -> None:
        state = self._read_state() or {}
        enabled = state.get("channels_enabled")
        enabled = list(enabled) if isinstance(enabled, list) else []
        if name not in enabled:
            enabled.append(name)
        state["channels_enabled"] = sorted(enabled)
        atomic_write_json(self._state_path(), state)

    # ---- override --------------------------------------------------------- #

    def _override_constraints_path(self) -> Path:
        return self._home / ".claude" / OVERRIDE_CONSTRAINTS_NAME

    def _resolve_override(self, pin: PinFile) -> tuple[str | None, bool]:
        """Return (override_sha, active). Auto-clears a stale override.

        An override auto-clears once jacked's shipped pin advances to the
        overridden SHA (spec Trust Model). We can only detect the equality case
        offline -- true ancestry ("past") needs git history we do not fetch here.
        """
        sha = self._get_setting(SETTING_OVERRIDE_SHA)
        if not sha or not truthy(self._get_setting(SETTING_OVERRIDE_ACK)):
            return None, False
        if sha == pin.commit_sha:
            self._clear_override_state()
            return None, False
        return sha, True

    def _clear_override_state(self) -> None:
        self._set_setting(SETTING_OVERRIDE_SHA, None)
        self._set_setting(SETTING_OVERRIDE_ACK, None)
        self._set_setting(SETTING_OVERRIDE_AT, None)
        try:
            self._override_constraints_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("could not remove override constraints file: %s", e)

    def _resolve_ref(self, upstream: str, ref: str) -> str:
        if _HEX40.fullmatch(ref):
            return ref.lower()
        git = self._require_bin("git")
        proc = self._run([git, "ls-remote", upstream, ref], timeout=NETWORK_TIMEOUT)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            raise RuntimeError(f"could not resolve ref {ref!r} against {upstream}")
        sha = lines[0].split()[0]
        if not _HEX40.fullmatch(sha):
            raise RuntimeError(f"git ls-remote returned an unexpected sha for {ref!r}: {sha!r}")
        return sha.lower()

    def _compile_constraints(self, upstream: str, sha: str, out_path: Path) -> None:
        uv = self._require_bin("uv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        req_line = f"agent-reach @ git+{upstream}@{sha}\n"
        tmp = tempfile.NamedTemporaryFile("w", suffix=".in", delete=False, encoding="utf-8")
        try:
            tmp.write(req_line)
            tmp.close()
            self._run(
                [uv, "pip", "compile", tmp.name, "-o", str(out_path)],
                timeout=NETWORK_TIMEOUT,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ---- channels --------------------------------------------------------- #

    def _channel_install_cmd(self, backend: ChannelBackend) -> list[str]:
        if backend.kind == "npm":
            npm = shutil.which("npm") or "npm"
            return [npm, "install", "-g", backend.spec]
        if backend.kind in ("pipx", "pipx-git"):
            # uv only -- never pipx/pip, per the supply-chain posture.
            return [self._require_bin("uv"), "tool", "install", backend.spec]
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

    # ---- doctor ----------------------------------------------------------- #

    def _doctor(self) -> tuple[dict | None, str | None]:
        """Best-effort `agent-reach doctor --json`. Never raises."""
        ar = shutil.which("agent-reach")
        if not ar:
            return None, "agent-reach CLI not found on PATH"
        try:
            proc = subprocess.run(
                [ar, "doctor", "--json"],
                capture_output=True,
                text=True,
                timeout=LOCAL_TIMEOUT,
                stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return None, f"agent-reach doctor failed to run: {e}"
        if proc.returncode != 0:
            return None, f"agent-reach doctor exited {proc.returncode}: {stderr_excerpt(proc.stderr)}"
        try:
            return json.loads(proc.stdout), None
        except json.JSONDecodeError as e:
            return None, f"agent-reach doctor emitted invalid JSON: {e}"

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
            raise RuntimeError(f"command timed out after {timeout}s: {' '.join(cmd)}") from e
        except OSError as e:
            raise RuntimeError(f"could not run {cmd[0]!r}: {e}") from e
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
                f"{stderr_excerpt(proc.stderr)}"
            )
        return proc
