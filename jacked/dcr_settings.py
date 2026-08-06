"""DCR review-engine config: read/write/resolve ``<home>/.claude/jacked-dcr.json``.

The ``/dcr`` command spawns parallel reviewer agents. By default those reviewers
run on Claude; this file lets the user point them at the OpenAI Codex CLI (a
model + reasoning effort) instead, while keeping a named list of lenses pinned to
Claude. Both the ``jacked dcr`` CLI group and the dashboard read and write
through here so the two surfaces can never disagree about the contract.

Reads are TOLERANT, writes are STRICT, and the contract re-validates:

* a genuinely absent file -> a copy of ``DEFAULTS`` (nothing configured yet),
* an existing-but-unreadable file -> ``DcrSettingsUnreadableError`` (STOP; refuse
  to touch it), mirroring ``jacked/memory/settings_io.py``. A tolerant reader
  that returned defaults on a corrupt file would be a data-loss bug: the next
  write would silently CLOBBER whatever the user actually had in there.
* ``read_config`` does NOT validate values (a hand-edited ``effort`` typo still
  reads back so the user can see and fix it). ``write_config`` validates every
  field before it touches the disk, so it is the boundary that rules out
  unusable and shell-unsafe values for everything JACKED writes.
* ``resolve`` re-validates on the way OUT, because jacked is not the only writer:
  the file is hand-editable and a foreign or older build can put anything
  json-representable in it. Every field that fails is replaced with its
  ``DEFAULTS`` value and explained in ``reason``, so a hand-edited file can
  neither crash a consumer nor smuggle a shell-unsafe value into the ``/dcr``
  command line.

``resolve`` is the one JSON contract both the ``/dcr`` command and the dashboard
consume. It never raises and never writes: a corrupt config degrades to Claude
with a human reason attached, because a broken config file must never block a
code review, and the corrupt bytes must survive for the user to repair. It also
never LOGS: Claude Code's Bash tool merges stderr into stdout, so a warning line
would corrupt the ``--json`` stream the ``/dcr`` command parses. ``reason`` is
the delivery mechanism.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

try:  # POSIX advisory locking. Absent on Windows -- see ``_config_lock``.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Model ids the config accepts: letters/digits then letters, digits, dot,
# underscore, hyphen, colon, or slash (covers gpt-5.6-luna, o3, provider:model,
# org/model). Deliberately NO whitespace, quotes, or shell metacharacters: the
# /dcr command interpolates this value into a `codex exec -m "<model>"` shell
# line. ``write_config`` applies this to everything jacked writes, and
# ``resolve`` applies it again to everything it READS, so a hand-edited or
# foreign-written file cannot smuggle an injection shape onto that command line.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")

CONFIG_NAME = "jacked-dcr.json"
CONFIG_VERSION = 1

# Ordered for help text / UI menus; VALID_ENGINES is the membership check.
ENGINE_CHOICES = ("claude", "codex")
VALID_ENGINES = set(ENGINE_CHOICES)

# Ordered for help text / UI menus (weakest to strongest); VALID_EFFORTS is the
# membership check. All seven are accepted by the codex CLI's
# ``model_reasoning_effort`` config key.
EFFORT_CHOICES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
VALID_EFFORTS = set(EFFORT_CHOICES)

# Model/effort defaults exist even while the engine is "claude" so that flipping
# the engine to codex with no flags does something sensible.
DEFAULTS = {
    "version": CONFIG_VERSION,
    "engine": "claude",
    "model": "gpt-5.6-luna",
    "effort": "xhigh",
    "keep_on_claude": ["Security", "Frontend Design"],
}


class DcrSettingsUnreadableError(Exception):
    """jacked-dcr.json exists but cannot be safely read, or was written corrupt.

    Raised instead of silently falling back to ``DEFAULTS`` so a read-modify-write
    never overwrites a config file we failed to parse.
    """


class DcrSettingsAccessError(Exception):
    """The config could not be WRITTEN or DELETED (a filesystem refusal).

    Distinct from ``DcrSettingsUnreadableError``, which means "the bytes are
    there but unusable". This one means the operating system said no --
    permissions, a read-only volume, a full disk. Raised instead of leaking a
    bare ``PermissionError`` so both surfaces can turn it into a friendly
    failure (CLI: ``[FAIL]`` + exit 1; API: 503) instead of a traceback.
    """


def jacked_home() -> Path:
    """Resolve jacked's home dir, honoring ``$JACKED_HOME``.

    Delegates to ``jacked.memory.vault.jacked_home`` so every jacked surface
    resolves the same home in tests and in prod. The import is function-local on
    purpose: it keeps this module import-cheap and stdlib-only for the CLI, which
    imports it eagerly for its ``click.Choice`` values.
    """
    from jacked.memory.vault import jacked_home as _vault_jacked_home

    return _vault_jacked_home()


def config_path(home: Path | str) -> Path:
    """``<home>/.claude/jacked-dcr.json``."""
    return Path(home) / ".claude" / CONFIG_NAME


def lock_path(home: Path | str) -> Path:
    """``<home>/.claude/jacked-dcr.json.lock`` -- the read-modify-write lock."""
    path = config_path(home)
    return path.parent / (path.name + ".lock")


def schema_path() -> Path:
    """Absolute path to the packaged reviewer-output JSON Schema."""
    return (Path(__file__).parent / "data" / "schemas" / "dcr-review-output.schema.json").resolve()


def _defaults_copy() -> dict:
    """A deep-enough copy of DEFAULTS (the list must not be shared)."""
    copy = dict(DEFAULTS)
    copy["keep_on_claude"] = list(DEFAULTS["keep_on_claude"])
    return copy


def read_config(home: Path | str) -> dict:
    """Read the DCR config. Defaults ONLY for a missing file; raise otherwise.

    Missing keys are filled from ``DEFAULTS`` and unknown keys are preserved (a
    newer jacked may have written fields this build does not know about; dropping
    them on the next write would be silent data loss). Values are not validated
    here -- see the module docstring for why reads are tolerant.

    The raised message carries the REAL cause (a parse failure and a permission
    failure are different problems with different fixes), because ``resolve``
    builds its user-facing ``reason`` straight from it.
    """
    path = config_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _defaults_copy()
    except OSError as e:
        raise DcrSettingsUnreadableError(f"{path} cannot be read: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DcrSettingsUnreadableError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise DcrSettingsUnreadableError(f"{path} is not a JSON object")

    merged = _defaults_copy()
    merged.update(data)
    return merged


def _choice_ok(value, choices: set) -> bool:
    """Membership test that tolerates an UNHASHABLE stored value.

    A hand-edited file can hold ``"engine": []``; a bare ``value in choices``
    raises ``TypeError`` on a list or a dict. Anything that is not a string is
    simply not one of our choices.
    """
    return isinstance(value, str) and value in choices


def _model_ok(model) -> bool:
    """A usable model id: a non-blank string with no shell-unsafe characters."""
    return isinstance(model, str) and bool(model.strip()) and bool(_MODEL_RE.fullmatch(model))


def _keep_ok(keep) -> bool:
    """A usable keep-on-Claude list: a list of non-blank lens names.

    A plain string is NOT usable even though it is iterable -- iterating it would
    silently turn "Security" into eight single-character lens names.
    """
    return isinstance(keep, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in keep
    )


def _validate(config: dict) -> None:
    """Raise ``ValueError`` unless every known field is a value we can run with."""
    engine = config.get("engine")
    if not _choice_ok(engine, VALID_ENGINES):
        raise ValueError(
            f"engine must be one of {sorted(VALID_ENGINES)}, got {engine!r}"
        )
    effort = config.get("effort")
    if not _choice_ok(effort, VALID_EFFORTS):
        raise ValueError(
            f"effort must be one of {list(EFFORT_CHOICES)}, got {effort!r}"
        )
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"model must be a non-empty string, got {model!r}")
    if not _MODEL_RE.fullmatch(model):
        raise ValueError(
            "model may only contain letters, digits, and . _ : / - "
            f"(no spaces or shell characters), got {model!r}"
        )
    keep = config.get("keep_on_claude")
    if not _keep_ok(keep):
        raise ValueError(
            f"keep_on_claude must be a list of non-empty strings, got {keep!r}"
        )


def _short(value, limit: int = 40) -> str:
    """A short, safe rendering of a stored value for a human reason string.

    ``repr`` first, so a value carrying quotes, newlines, or shell metacharacters
    can never be echoed raw into a terminal or a dashboard string; then truncate,
    so a hand-pasted megabyte does not become a megabyte-long reason.
    """
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _sanitize_fields(config: dict) -> tuple[dict, list[str]]:
    """Return usable values for the four known fields, plus human reasons.

    The READ-side twin of ``_validate``: every field that fails its check is
    replaced with the ``DEFAULTS`` value and explained. Nothing here raises, for
    ANY json-representable input -- that promise is what lets ``resolve`` be
    exception-free and lets ``update_config`` self-heal a stale stored value
    instead of locking the user out of every other field.
    """
    values: dict = {}
    reasons: list[str] = []

    engine = config.get("engine")
    if _choice_ok(engine, VALID_ENGINES):
        values["engine"] = engine
    else:
        # A corrupt-file fallback already carries the (valid) default engine, so
        # this only fires on a genuinely wrong stored value.
        values["engine"] = DEFAULTS["engine"]
        reasons.append(
            f"stored engine {_short(engine)} is not one of "
            f"{', '.join(ENGINE_CHOICES)}; using {DEFAULTS['engine']}"
        )

    model = config.get("model")
    if _model_ok(model):
        values["model"] = model
    else:
        values["model"] = DEFAULTS["model"]
        reasons.append(
            f"stored model {_short(model)} is not a valid model id; "
            f"using {DEFAULTS['model']}"
        )

    effort = config.get("effort")
    if _choice_ok(effort, VALID_EFFORTS):
        values["effort"] = effort
    else:
        values["effort"] = DEFAULTS["effort"]
        reasons.append(
            f"stored effort {_short(effort)} is not one of "
            f"{', '.join(EFFORT_CHOICES)}; using {DEFAULTS['effort']}"
        )

    keep = config.get("keep_on_claude")
    if _keep_ok(keep):
        values["keep_on_claude"] = list(keep)
    else:
        values["keep_on_claude"] = list(DEFAULTS["keep_on_claude"])
        reasons.append(
            f"stored keep_on_claude {_short(keep)} is not a list of lens names; "
            f"using {', '.join(DEFAULTS['keep_on_claude'])}"
        )

    return values, reasons


def write_config(home: Path | str, config: dict) -> None:
    """Validate, then atomically write the DCR config and verify it re-reads.

    The atomic write is writer-unique (mkstemp + ``os.replace``) because the CLI,
    the dashboard service, and hook processes are separate processes that can all
    write the same home; a shared tmp path would let one ``os.replace`` the
    other's half-written file away. The tmp is unlinked on any ``BaseException``
    so a crash never leaves a stray tmp or a half-written target. After the
    replace the file is re-read and parsed: a corrupt write raises
    ``DcrSettingsUnreadableError`` rather than being trusted.

    Raises ``ValueError`` when a field is unusable; the file is untouched then.
    Raises ``DcrSettingsAccessError`` when the filesystem refuses the write (an
    unwritable directory, a read-only volume, a full disk), so callers get one
    named failure to catch instead of a bare ``PermissionError`` traceback.
    """
    data = dict(config)
    data.setdefault("version", CONFIG_VERSION)
    _validate(data)

    path = config_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    except OSError as e:
        raise DcrSettingsAccessError(
            f"cannot write {path}: {e}. Check the permissions on {path.parent}."
        ) from e

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp_name, path)
    except BaseException as e:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        if isinstance(e, OSError):
            raise DcrSettingsAccessError(
                f"cannot write {path}: {e}. Check the permissions on {path.parent}."
            ) from e
        raise

    try:
        verify = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DcrSettingsUnreadableError(f"post-write verification failed for {path}: {e}") from e
    if not isinstance(verify, dict):
        raise DcrSettingsUnreadableError(
            f"post-write verification failed for {path}: not a JSON object"
        )


@contextlib.contextmanager
def _config_lock(home: Path | str, timeout: float = 5.0):
    """Serialize a read-modify-write of the config ACROSS PROCESSES.

    The CLI, the dashboard service, and hook processes are separate processes
    that write the same file, so an in-process lock cannot protect the
    read-modify-write window: two writers would each read the old config and the
    last one out would drop the other's field. A POSIX ``flock`` on
    ``<config>.lock`` closes that window.

    FAIL-OPEN, mirroring ``jacked.memory.vault``: if the lock file cannot be
    opened, or the lock cannot be taken within ``timeout`` seconds, this warns
    and proceeds. A rare lost update is bad; a ``jacked dcr engine set`` that
    hangs forever is worse. On a platform with no ``fcntl`` (Windows) there is no
    lock at all: the codex engine path is a POSIX-shell feature, and degrading to
    an unlocked write there beats failing the command outright.
    """
    path = lock_path(home)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover - Windows
        yield
        return

    try:
        fh = open(path, "a+")  # noqa: SIM115 -- closed in the finally below
    except OSError as e:
        logger.warning("dcr: could not open lock %s (%s); proceeding unlocked", path, e)
        yield
        return

    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "dcr: lock %s unavailable after %.1fs; proceeding without it",
                        path, timeout,
                    )
                    break
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            fh.close()


def update_config(
    home: Path | str,
    *,
    engine: str | None = None,
    model=None,
    effort: str | None = None,
    keep_on_claude=None,
) -> dict:
    """Merge the given fields into the stored config and write it. Returns it.

    THE one read-modify-write implementation: the CLI and the dashboard both go
    through here, so the two surfaces cannot drift on merge semantics. Only
    non-``None`` arguments are applied; every other stored field (including keys
    this build does not know about) survives.

    The whole read-modify-write runs under ``_config_lock``, so a CLI write and a
    dashboard write cannot each read the old config and drop the other's field.

    Stored fields that would fail validation are SELF-HEALED to their defaults
    before the update applies. Without that, one stale invalid value (a
    hand-edited ``effort`` typo) would fail validation on the way out and lock
    the user out of changing anything else -- including switching back to Claude,
    the very thing you do to escape a broken codex setup.

    Raises ``DcrSettingsUnreadableError`` when the file exists but cannot be
    parsed (nothing is written: unparseable bytes are never clobbered),
    ``ValueError`` when an argument itself is unusable, and
    ``DcrSettingsAccessError`` when the filesystem refuses the write.
    """
    with _config_lock(home):
        config = read_config(home)

        healed, _reasons = _sanitize_fields(config)
        config.update(healed)

        if engine is not None:
            config["engine"] = engine
        if model is not None:
            config["model"] = model.strip() if isinstance(model, str) else model
        if effort is not None:
            config["effort"] = effort
        if keep_on_claude is not None:
            config["keep_on_claude"] = list(keep_on_claude)
        config.setdefault("version", CONFIG_VERSION)

        write_config(home, config)
        return config


def clear_config(home: Path | str) -> bool:
    """Delete the DCR config. Returns whether a file was there to delete.

    Raises ``DcrSettingsAccessError`` when the file is there but the filesystem
    refuses to remove it, so the caller reports a real cause instead of claiming
    the config was cleared (or dying with a ``PermissionError`` traceback).
    """
    path = config_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise DcrSettingsAccessError(
            f"cannot delete {path}: {e}. Check the permissions on {path.parent}."
        ) from e
    return True


def codex_preflight(timeout: float = 10.0) -> dict:
    """Report whether the codex CLI is installed and signed in. Never raises.

    Returns ``codex_installed``, ``codex_logged_in``, ``reason``, and
    ``codex_path`` (the resolved binary, or ``None`` when it is not installed) --
    the path is what turns "not signed in" into a diagnosable report when several
    codex builds are on PATH.

    ``codex login status`` exits 0 when a session is present. Anything that stops
    us from learning the answer (the CLI hanging, a spawn failure) degrades to
    installed-but-not-usable with a human ``reason`` rather than an exception:
    this runs on the /dcr hot path and on a dashboard request, and neither may
    die because a subprocess misbehaved.
    """
    codex = shutil.which("codex")
    if not codex:
        return {
            "codex_installed": False,
            "codex_logged_in": False,
            "codex_path": None,
            "reason": "Codex CLI is not installed. Install it, then run: codex login",
        }

    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, resolved binary
            [codex, "login", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # No inherited stdin: a codex build that decides to prompt would
            # otherwise hang on the /dcr hot path or block a dashboard request.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {
            "codex_installed": True,
            "codex_logged_in": False,
            "codex_path": codex,
            "reason": f"Codex CLI did not respond within {timeout:g}s",
        }
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.debug("codex login status failed to run", exc_info=True)
        return {
            "codex_installed": True,
            "codex_logged_in": False,
            "codex_path": codex,
            "reason": f"Could not run the Codex CLI: {e}",
        }

    if proc.returncode == 0:
        return {
            "codex_installed": True,
            "codex_logged_in": True,
            "codex_path": codex,
            "reason": None,
        }
    return {
        "codex_installed": True,
        "codex_logged_in": False,
        "codex_path": codex,
        "reason": "Codex CLI is not signed in. Run: codex login",
    }


def resolve(home: Path | str, *, preflight: bool = True) -> dict:
    """The single engine contract the /dcr command and the dashboard consume.

    Keys: ``engine``, ``model``, ``effort``, ``keep_on_claude``, ``usable``,
    ``reason``, ``codex_installed``, ``codex_logged_in``, ``codex_path``,
    ``schema_path``.

    Every field is re-validated on the way out (see ``_sanitize_fields``): a
    value this build cannot run with is replaced by its default and explained in
    ``reason``, so a hand-edited file cannot crash a consumer or put a
    shell-unsafe model on the ``codex exec`` command line.

    Engine "claude" is always usable and runs no subprocess (the preflight fields
    stay null). Engine "codex" is usable only when the codex CLI is installed AND
    signed in; ``reason`` then also carries the preflight explanation. With
    ``preflight=False`` no check runs, so the preflight fields stay null and the
    config is reported as usable: callers that pass it want the stored config,
    not a live capability check.

    Never raises, never writes, and never logs. A corrupt config degrades to
    Claude with a reason -- a broken file must not block a review, the bytes must
    survive for the user to repair, and a log line on stderr would corrupt the
    ``--json`` stream the /dcr command parses (Claude Code's Bash merges stderr
    into stdout).
    """
    reasons: list[str] = []
    try:
        config = read_config(home)
    except DcrSettingsUnreadableError as e:
        # Carry the REAL cause: "corrupt JSON" printed at someone whose file is
        # permission-denied sends them hunting a syntax error that is not there.
        config = _defaults_copy()
        reasons.append(f"{e}; using Claude until it is fixed")

    values, field_reasons = _sanitize_fields(config)
    reasons.extend(field_reasons)

    engine = values["engine"]
    resolved = {
        "engine": engine,
        "model": values["model"],
        "effort": values["effort"],
        "keep_on_claude": values["keep_on_claude"],
        "usable": True,
        "reason": None,
        "codex_installed": None,
        "codex_logged_in": None,
        "codex_path": None,
        "schema_path": str(schema_path()),
    }

    if engine == "codex" and preflight:
        check = codex_preflight()
        resolved["codex_installed"] = check["codex_installed"]
        resolved["codex_logged_in"] = check["codex_logged_in"]
        resolved["codex_path"] = check.get("codex_path")
        resolved["usable"] = bool(check["codex_installed"] and check["codex_logged_in"])
        if check["reason"]:
            reasons.append(check["reason"])

    resolved["reason"] = "; ".join(reasons) or None
    return resolved
