"""Skill-listing budget report: which skills Claude Code shows without a description.

Claude Code injects a listing of every model-visible skill and command into each
session (``- name: description``). The listing has a character budget. When the
listing is over budget, Claude Code keeps every NAME but drops whole
DESCRIPTIONS. A skill with no description almost never gets auto-invoked, so it
never gains usage, so it stays dropped. jacked's skill packs push users over the
budget without any visible signal, so this module measures the listing, simulates
the drop, and recommends the two settings that fix it.

The model below mirrors the Claude Code source (read from the 2.1.287 binary on
2026-10-01, functions ``h4e``/``_ct``/``tnr``/``pXt``/``ERt``/``Sct``/``sE``):

* budget chars = ``SLASH_COMMAND_TOOL_CHAR_BUDGET`` when set, else
  ``floor(context_window_tokens * chars_per_token * skillListingBudgetFraction)``
  (fraction default 0.01; window default 200,000 when Claude Code cannot tell).
* entry text = ``description`` plus ``" - " + when_to_use`` when present, cut to
  ``skillListingMaxDescChars`` (default 1536; a cut entry ends in an ellipsis).
* entry chars = ``len(name) + 4 + len(text)`` for ``- name: text``, or
  ``len(name) + 2`` for a bare ``- name``; entries are joined by newlines.
* over budget: bundled (built-in) entries and ``name-only`` overrides are kept
  as they are. Every other entry starts as a bare name. Entries are then visited
  in DESCENDING usage score (ties keep listing order) and each one gets its
  description back when that still fits the remaining budget. So the
  least-used descriptions are the ones dropped.
* usage score = ``usageCount * max(0.5 ** (days_since_last_use / 7), 0.1)`` from
  the ``skillUsage`` map in ``~/.claude.json``.
* ``disable-model-invocation: true`` removes an entry from the listing, and so
  does a skill or command with neither a description nor ``when_to_use``.
  ``skillOverrides`` ``off`` / ``user-invocable-only`` remove a user skill;
  ``name-only`` shrinks it to a bare name. Plugin entries ignore overrides.

This module is pure (no FastAPI imports). The dashboard route, the CLI and the
tests all call ``build_report``; the writer goes through the corruption-safe
``jacked.memory.settings_io`` pair.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Claude Code constants and the calibration
# --------------------------------------------------------------------------- #

DEFAULT_MAX_DESC_CHARS = 1536
"""Claude Code's default ``skillListingMaxDescChars``."""

DEFAULT_BUDGET_FRACTION = 0.01
"""Claude Code's default ``skillListingBudgetFraction`` (1% of the window)."""

DEFAULT_CONTEXT_WINDOW = 1_000_000
"""Headline window for the report. The real window is not knowable from disk."""

REPORT_WINDOWS = (200_000, 1_000_000)

ENV_BUDGET_VAR = "SLASH_COMMAND_TOOL_CHAR_BUDGET"

SYNCED_PREFIX = "anthropic-skills:"

CALIBRATED_CLAUDE_VERSION = "2.1.287"
"""The Claude Code release every version-pinned constant here was read from."""

MAX_YAML_ALIASES = 100
"""YAML aliases allowed in one frontmatter block. Real skills use none; the cap
stops an alias "billion laughs" file from expanding to gigabytes."""

MAX_FRONTMATTER_BYTES = 1_000_000
"""Read at most this much of a skill file looking for the closing ``---``."""

MAX_WORKFLOW_BYTES = 1_000_000
"""Read at most this much of a plugin workflow file (``meta`` is at the top)."""

ENTRY_PREFIX_CHARS = 4
"""``"- "`` before the name plus ``": "`` after it (one entry, with description)."""

NAME_ONLY_PREFIX_CHARS = 2
"""``"- "`` before a bare name."""

SEPARATOR_CHARS = 1
"""The newline between two entries."""

CHARS_PER_TOKEN = 3
"""Characters per token Claude Code uses for the skill listing (current models).

Claude Code's ``Eg(model)`` returns 4 for the legacy models in
``LEGACY_FOUR_CHAR_MODELS`` and 3 for every other model (Opus 4.7 and later,
Opus 5.x, Fable, Sonnet 5). The same factor sets the budget in characters
(window x factor x fraction) AND converts characters to tokens, so the budget
in TOKENS is ``window x fraction`` for every model; only the character budget
moves.

CALIBRATION (2026-10-01, ``claude --version`` = 2.1.287, model claude-opus-5-5,
1M-token window, this machine's real ``~/.claude`` inventory). Each run was
``claude -p "/context"`` in a scratch dir whose ``.claude/settings.json`` set
only the two listing keys:

* fraction 0.05, maxDescChars 1536 (nothing dropped): ``/context`` Skills row
  = 35.6k tokens. Estimator: 106,911 chars / 3 = 35,637 tokens (+0.1%).
* fraction 0.05, maxDescChars 250: ``/context`` Skills row = 18.2k tokens.
  Estimator: 54,652 chars / 3 = 18,217 tokens (+0.1%).
* fraction 0.01, maxDescChars 1536 (Claude Code defaults): ``/context`` Skills
  row = 9.9k tokens with 170 entries at "< 20" tokens. Estimator: a 30,000-char
  budget (10,000 tokens), full, with 170 descriptions dropped; 168 of the 170
  names match, and the 2 swaps are low-usage entries at the end of the greedy
  fill, where the approximate built-in lengths decide what fits.
* 238 entries in all three runs (118 user skills and commands, 91 plugin
  entries, 14 built-in, 15 claude.ai synced); every per-entry row is within
  12 tokens of the estimate.

A first fit with 4 chars per token was 27% low on both runs with a constant
per-entry ratio of about 1.35, which is how the factor 3 was found (and then
confirmed in the source). The per-entry overhead is NOT a fitted constant: it
is the literal listing format (``ENTRY_PREFIX_CHARS`` plus the name, plus
``SEPARATOR_CHARS``), read from the Claude Code source and confirmed by the two
fits. The ``/context`` excerpts and the inventory snapshot live in
``tests/fixtures/skill_listing/``.
"""

LEGACY_FOUR_CHAR_MODELS = (  # Claude Code 2.1.287 ``DI``
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet",
    "claude-3-5-haiku", "claude-3-7-sonnet", "claude-opus-4-0", "claude-opus-4-1",
    "claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-0", "claude-sonnet-4-5",
    "claude-sonnet-4-6", "claude-haiku-4-5",
)


# Claude Code's built-in entries are compiled into the binary, so they cannot
# be read from disk. These lengths are the measured ``/context`` rows on 2.1.287
# (token row x 3 chars, minus the ``- name: `` prefix). The third field says
# whether Claude Code protects the entry: bundled skills are never dropped (but
# ARE cut to skillListingMaxDescChars); the built-in ``init`` and
# ``security-review`` commands lose their descriptions like any other entry
# (both read "< 20" tokens in the defaults run). Rows change between Claude Code
# releases; a drift of a few hundred characters does not move a recommendation.
BUILTIN_ENTRIES: tuple[tuple[str, int, bool], ...] = (
    ("dataviz", 1429, True),
    ("update-config", 703, True),
    ("keybindings-help", 220, True),
    ("code-review", 825, True),
    ("simplify", 168, True),
    ("fewer-permission-prompts", 152, True),
    ("loop", 352, True),
    ("schedule", 378, True),
    ("claude-api", 1066, True),
    ("workflow-authoring", 218, True),
    ("run", 353, True),
    ("plugin-authoring", 220, True),
    ("init", 52, False),
    ("security-review", 71, False),
)

MAX_DESC_MIN = 100
MAX_DESC_MAX = 1536
FRACTION_MAX = 1.0
"""Claude Code accepts any fraction above 0 up to 1."""

RECOMMEND_MAX_DESC_CANDIDATES = tuple(range(150, 1501, 50)) + (1536,)
RECOMMEND_FALLBACK_MAX_DESC = 250
RECOMMEND_FALLBACK_FRACTIONS = (0.015, 0.02, 0.025, 0.03)

SOURCE_LABELS = {
    "user": "Your skills",
    "command": "Your commands",
    "synced": "claude.ai skills",
    "builtin": "Built into Claude Code",
}


class ListingSettingsError(ValueError):
    """A listing setting value is out of range or the wrong type."""


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #

@dataclass
class ListingEntry:
    """One model-visible line of the skill listing."""

    name: str
    source: str              # user | command | plugin | synced | builtin
    group: str               # grouping key (pack name, plugin name, or source)
    group_label: str         # human label for the group
    desc_len: int            # UTF-16 length of description [+ " - " + when_to_use]
    path: str = ""           # file the entry came from ("" for built-ins)
    name_only: bool = False  # skillOverrides "name-only"
    protected: bool = False  # never dropped (bundled or name-only)
    usage: float = 0.0       # recency-decayed usage score

    def full_chars(self, max_desc: int) -> int:
        if self.name_only:
            return _js_len(self.name) + NAME_ONLY_PREFIX_CHARS
        return _js_len(self.name) + ENTRY_PREFIX_CHARS + min(self.desc_len, max_desc)

    def bare_chars(self) -> int:
        return _js_len(self.name) + NAME_ONLY_PREFIX_CHARS


@dataclass
class YamlWarning:
    name: str
    path: str
    error: str


@dataclass
class Inventory:
    entries: list[ListingEntry] = field(default_factory=list)
    yaml_warnings: list[YamlWarning] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    not_counted: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _js_len(text: str) -> int:
    """String length the way JavaScript counts it (UTF-16 code units)."""
    # surrogatepass: YAML "\\ud83d\\ude00" escapes decode to lone surrogates.
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _read_json(path: Path) -> dict:
    """Read a JSON object; ``{}`` for a missing or unreadable file (read-only use)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def _split_frontmatter(text: str) -> str | None:
    m = _FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


def _lenient_frontmatter(block: str) -> dict:
    """Line-based fallback for frontmatter that is not strict YAML.

    Claude Code reads such files leniently (a skill whose description holds an
    unquoted ``": "`` still shows its description), so the estimate must too.
    Handles ``key: value``, quoted values, block scalars (``>``/``|``) and
    indented continuation lines of a plain scalar.
    """
    result: dict = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line or line[0] in (" ", "\t", "#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val[:1] in (">", "|") and val.strip("><|+-0123456789") == "":
            parts = []
            while i < len(lines) and (lines[i].strip() == "" or lines[i][:1] in (" ", "\t")):
                parts.append(lines[i].strip())
                i += 1
            joiner = " " if val[0] == ">" else "\n"
            result[key] = joiner.join(p for p in parts if p)
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # A plain scalar may continue on indented lines.
            parts = [val]
            while i < len(lines) and lines[i][:1] in (" ", "\t") and lines[i].strip():
                parts.append(lines[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        result[key] = val
    return result


def _bounded_loader():
    """A ``yaml.SafeLoader`` that refuses more than ``MAX_YAML_ALIASES`` aliases."""
    import yaml

    class _BoundedSafeLoader(yaml.SafeLoader):
        _alias_count = 0

        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                self._alias_count += 1
                if self._alias_count > MAX_YAML_ALIASES:
                    raise yaml.composer.ComposerError(
                        None, None, f"more than {MAX_YAML_ALIASES} YAML aliases",
                        self.peek_event().start_mark,
                    )
            return super().compose_node(parent, index)

    return _BoundedSafeLoader


TEXT_FIELDS = ("name", "description", "when_to_use")


MAX_TEXT_INT = 10 ** 18
"""Largest integer a text field may hold. YAML 1.1 reads ``0xfff...`` as an int,
and ``str()`` of a huge int raises (Python's int digit limit)."""

MAX_FIELD_CHARS = 100_000
"""Cap on the text joined from a flat-list field, before anything else uses it."""


def _scalar_ok(value) -> bool:
    if isinstance(value, bool) or isinstance(value, (str, float)):
        return True
    return isinstance(value, int) and abs(value) < MAX_TEXT_INT


def _text_field_ok(value) -> bool:
    """A string, a bool, a float, a small int, or a FLAT list of those."""
    if value is None or _scalar_ok(value):
        return True
    return isinstance(value, list) and all(_scalar_ok(v) for v in value)


def parse_frontmatter(text: str) -> tuple[dict, str | None]:
    """Return ``(frontmatter, strict_yaml_error)``. Never raises.

    Strict YAML first, through a loader that caps aliases. When that fails for
    ANY reason (bad YAML, an unknown tag, too many aliases, nesting deep enough
    to exhaust the stack, a parser bug) or a name/description/when_to_use is
    not plain text, fall back to the lenient line parse and return the reason
    so the caller can report the file: other runtimes (Codex, for one) reject
    such files.
    """
    import yaml

    block = _split_frontmatter(text)
    if block is None:
        return {}, None
    try:
        data = yaml.load(block, Loader=_bounded_loader())  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 2})" if mark is not None else ""
        return _lenient_frontmatter(block), f"{first}{where}"
    except RecursionError:
        return _lenient_frontmatter(block), "YAML nesting is too deep"
    except Exception as exc:  # noqa: BLE001 - one file must never stop the report
        return _lenient_frontmatter(block), f"YAML could not be loaded ({type(exc).__name__})"
    if not isinstance(data, dict):
        return {}, None
    bad = [k for k in TEXT_FIELDS if not _text_field_ok(data.get(k))]
    if bad:
        return _lenient_frontmatter(block), f"{', '.join(bad)} is not plain text"
    return data, None


def _as_text(value) -> str:
    """Text of a field already checked by ``_text_field_ok`` (never recursive)."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts, size = [], 0
        for v in value:
            if not _scalar_ok(v):
                continue
            piece = str(v)
            parts.append(piece)
            size += len(piece) + 1
            if size > MAX_FIELD_CHARS:
                break
        return " ".join(parts)[:MAX_FIELD_CHARS].strip()
    if _scalar_ok(value):
        return str(value).strip()
    return ""


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return False


def entry_text(description: str, when_to_use: str) -> str:
    """The listing text Claude Code builds for one entry (before the cap)."""
    return f"{description} - {when_to_use}" if when_to_use else description


# --------------------------------------------------------------------------- #
# Settings + usage
# --------------------------------------------------------------------------- #

def settings_file(home: Path) -> Path:
    from jacked.memory.settings_io import settings_path

    return settings_path(home)


def managed_settings_paths() -> list[Path]:
    """Claude Code's managed-settings files for this OS (base file, then drop-ins)."""
    if sys.platform == "darwin":
        root = Path("/Library/Application Support/ClaudeCode")
    elif sys.platform == "win32":
        root = Path(r"C:\Program Files\ClaudeCode")
    else:
        root = Path("/etc/claude-code")
    paths = [root / "managed-settings.json"]
    try:
        paths += sorted((root / "managed-settings.d").glob("*.json"))
    except OSError:
        pass
    return paths


_MERGED_DICT_KEYS = ("skillOverrides", "enabledPlugins", "env")
_LISTING_KEYS = ("skillListingMaxDescChars", "skillListingBudgetFraction")


def _merge_settings(home: Path, cwd: Path | None) -> tuple[dict, list[str], bool, list[str]]:
    """Merge settings the way Claude Code layers them (low to high priority).

    user < project ``.claude/settings.json`` < project ``settings.local.json``
    < managed. Object keys that Claude Code merges (skillOverrides,
    enabledPlugins, env) merge per entry; every other key is replaced by the
    higher source. Returns ``(merged, sources_read, user_unreadable,
    sources_overriding_the_listing_keys)``. Only the USER file can make the
    report "unreadable" (it is the file jacked writes); any other unreadable
    source is logged and skipped.
    """
    from jacked.memory.settings_io import SettingsUnreadableError, read_settings

    layers: list[tuple[str, Path]] = [("user", settings_file(home))]
    if cwd is not None:
        layers.append(("project", Path(cwd) / ".claude" / "settings.json"))
        layers.append(("local", Path(cwd) / ".claude" / "settings.local.json"))
    layers += [("managed", p) for p in managed_settings_paths()]

    merged: dict = {}
    sources: list[str] = []
    overriding: list[str] = []
    user_unreadable = False
    for kind, path in layers:
        try:
            if kind != "user" and not path.is_file():
                continue
            data = read_settings(path)
        except SettingsUnreadableError:
            if kind == "user":
                user_unreadable = True
            else:
                logger.warning("Skill listing: ignored unreadable settings %s", path)
            continue
        except OSError:
            continue
        if kind != "user" or path.exists():
            sources.append(str(path))
        if kind != "user" and any(k in data for k in _LISTING_KEYS):
            overriding.append(str(path))
        for key, val in data.items():
            if key in _MERGED_DICT_KEYS and isinstance(val, dict):
                base = merged.get(key) if isinstance(merged.get(key), dict) else {}
                merged[key] = {**base, **val}
            else:
                merged[key] = val
    return merged, sources, user_unreadable, overriding


def read_listing_settings(
    home: Path, environ: dict | None = None, cwd: Path | None = None,
) -> dict:
    """Current listing settings, read-only. Never raises on a bad file.

    Reads the user settings, the project settings for ``cwd`` when given, and
    the managed settings, merged like Claude Code. ``settings_unreadable`` is
    True when the USER settings.json exists but cannot be parsed; the values
    then come from the other sources or Claude Code's defaults.
    """
    environ = os.environ if environ is None else environ
    settings, sources, unreadable, overriding = _merge_settings(Path(home), cwd)

    raw_max = settings.get("skillListingMaxDescChars")
    max_set = isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0
    raw_frac = settings.get("skillListingBudgetFraction")
    frac_set = (
        isinstance(raw_frac, (int, float)) and not isinstance(raw_frac, bool)
        and math.isfinite(raw_frac) and 0 < raw_frac <= 1
    )

    # Claude Code copies settings ``env`` into its own process environment, so
    # a value there wins over the shell. A value only in THIS process's
    # environment is the jacked service's environment, which may not be the
    # environment the user's Claude Code sessions start with.
    env_block = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    env_budget = _positive_int(env_block.get(ENV_BUDGET_VAR))
    env_from = "settings.json env" if env_budget else None
    if env_budget is None:
        env_budget = _positive_int(environ.get(ENV_BUDGET_VAR))
        env_from = ("the jacked process environment (your shell may differ)"
                    if env_budget else None)

    def env_flag(name: str) -> bool:
        return _truthy(env_block.get(name)) or _truthy(environ.get(name))

    model, model_from = _configured_model(settings, env_block, environ)

    overrides = settings.get("skillOverrides")
    enabled_plugins = settings.get("enabledPlugins")
    scope = "user and managed settings" if cwd is None else "user, project and managed settings"
    return {
        "max_desc_chars": raw_max if max_set else DEFAULT_MAX_DESC_CHARS,
        "max_desc_chars_set": bool(max_set),
        "budget_fraction": float(raw_frac) if frac_set else DEFAULT_BUDGET_FRACTION,
        "budget_fraction_set": bool(frac_set),
        "env_budget": env_budget,
        "env_budget_from": env_from,
        "skill_overrides": overrides if isinstance(overrides, dict) else {},
        "enabled_plugins": enabled_plugins if isinstance(enabled_plugins, dict) else {},
        "model": model,
        "model_from": model_from,
        "disable_bundled_skills": (_truthy(settings.get("disableBundledSkills"))
                                   or env_flag("CLAUDE_CODE_DISABLE_BUNDLED_SKILLS")),
        "disable_1m_context": env_flag("CLAUDE_CODE_DISABLE_1M_CONTEXT"),
        "settings_unreadable": unreadable,
        "settings_path": str(settings_file(home)),
        "settings_sources": [display_path(p) for p in sources],
        "settings_scope": scope,
        "listing_keys_overridden_by": overriding,
    }


def _configured_model(settings: dict, env_block: dict, environ) -> tuple[str | None, str | None]:
    """The model a new session starts with, in Claude Code's order.

    ``ANTHROPIC_MODEL`` (settings ``env`` first, as Claude Code copies it into
    its environment, then the process environment) ranks above the settings
    ``model`` key; ``ANTHROPIC_DEFAULT_MODEL`` ranks below it.
    """
    def text(value) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    if text(env_block.get("ANTHROPIC_MODEL")):
        return text(env_block.get("ANTHROPIC_MODEL")), "ANTHROPIC_MODEL (settings.json env)"
    if text(environ.get("ANTHROPIC_MODEL")):
        return (text(environ.get("ANTHROPIC_MODEL")),
                "ANTHROPIC_MODEL (the jacked process environment)")
    if text(settings.get("model")):
        return text(settings.get("model")), "settings model"
    if text(env_block.get("ANTHROPIC_DEFAULT_MODEL")):
        return (text(env_block.get("ANTHROPIC_DEFAULT_MODEL")),
                "ANTHROPIC_DEFAULT_MODEL (settings.json env)")
    if text(environ.get("ANTHROPIC_DEFAULT_MODEL")):
        return (text(environ.get("ANTHROPIC_DEFAULT_MODEL")),
                "ANTHROPIC_DEFAULT_MODEL (the jacked process environment)")
    return None, None


def display_path(path: str) -> str:
    """A path safe to show: the synced-skills account folder becomes ``<account>``.

    The folder name is ``<organizationUuid>_<accountUuid>``; those ids never
    leave this module.
    """
    return re.sub(r"(synced[/\\])[^/\\]+", r"\1<account>", str(path))


# --------------------------------------------------------------------------- #
# Model resolution (pinned to CALIBRATED_CLAUDE_VERSION)
# --------------------------------------------------------------------------- #

# Claude Code 2.1.287 ``OI``: lower-case the id, then the first substring hit
# in this order names the model. Provider ids (Bedrock ``us.anthropic.``
# prefixes, Vertex ``@date`` suffixes, dated ids) all land on the same names.
_CANONICAL_CHAIN: tuple[tuple[str, str], ...] = (
    ("claude-fable-5-1", "claude-fable-5-1"), ("claude-fable-5", "claude-fable-5"),
    ("claude-mythos-5-1", "claude-mythos-5-1"), ("claude-mythos-5", "claude-mythos-5"),
    ("claude-opus-5-5", "claude-opus-5-5"), ("claude-opus-5", "claude-opus-5"),
    ("claude-opus-4-8", "claude-opus-4-8"), ("claude-opus-4-7", "claude-opus-4-7"),
    ("claude-opus-4-6", "claude-opus-4-6"), ("claude-opus-4-5", "claude-opus-4-5"),
    ("claude-opus-4-1", "claude-opus-4-1"), (r"re:claude-opus-4(?!-\d(?!\d))", "claude-opus-4-0"),
    ("claude-sonnet-5-5", "claude-sonnet-5-5"), ("claude-sonnet-5", "claude-sonnet-5"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6"), ("claude-sonnet-4-5", "claude-sonnet-4-5"),
    (r"re:claude-sonnet-4(?!-\d(?!\d))", "claude-sonnet-4-0"),
    ("claude-haiku-4-5", "claude-haiku-4-5"), ("claude-3-7-sonnet", "claude-3-7-sonnet"),
    ("claude-3-5-sonnet", "claude-3-5-sonnet"), ("claude-3-5-haiku", "claude-3-5-haiku"),
    ("claude-3-opus", "claude-3-opus"), ("claude-3-sonnet", "claude-3-sonnet"),
    ("claude-3-haiku", "claude-3-haiku"),
)

# Context window per model, from the 2.1.287 model catalog (``context.window``).
MODEL_WINDOWS: dict[str, int] = {
    "claude-3-opus": 200_000, "claude-3-sonnet": 200_000, "claude-3-haiku": 200_000,
    "claude-3-5-haiku": 200_000, "claude-3-5-sonnet": 200_000, "claude-3-7-sonnet": 200_000,
    "claude-haiku-4-5": 200_000, "claude-sonnet-4-0": 200_000, "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 200_000, "claude-opus-4-0": 200_000, "claude-opus-4-1": 200_000,
    "claude-opus-4-5": 200_000, "claude-opus-4-6": 200_000,
    "claude-sonnet-5": 1_000_000, "claude-sonnet-5-5": 1_000_000,
    "claude-opus-4-7": 1_000_000, "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 1_000_000, "claude-opus-5-5": 1_000_000,
    "claude-fable-5": 1_000_000, "claude-fable-5-1": 1_000_000,
    "claude-mythos-5": 1_000_000, "claude-mythos-5-1": 1_000_000,
}

# Model aliases resolve through Anthropic's served catalog at run time. These
# are the newest catalog entry per family in 2.1.287; an alias can move to a
# newer model in a later release (the version note then flags the drift).
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5-5", "opusplan": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}

DEFAULT_CLAUDE_WINDOW = 200_000
"""Claude Code's window for a Claude model with no catalog entry (``r2e``)."""


def resolve_model(model: str | None) -> str | None:
    """Canonical model name (Claude Code's ``OI``), or None when unknown."""
    if not model or not isinstance(model, str):
        return None
    norm = re.sub(r"\[[12]m\]$", "", model.strip().lower())
    if norm in MODEL_ALIASES:
        return MODEL_ALIASES[norm]
    for needle, name in _CANONICAL_CHAIN:
        if needle.startswith("re:"):
            if re.search(needle[3:], norm):
                return name
        elif needle in norm:
            return name
    return None


def chars_per_token_for_model(model: str | None) -> int:
    """Claude Code's ``Eg``: 4 for the legacy models, 3 for every other model.

    An unset model, or one jacked cannot resolve, counts as a current model
    (3): every model in the 2.1.287 catalog newer than Opus 4.6 / Sonnet 4.6 /
    Haiku 4.5 uses 3. Claude Code itself uses 4 only before it knows the model.
    """
    canonical = resolve_model(model)
    if canonical is None:
        return CHARS_PER_TOKEN
    return 4 if canonical in LEGACY_FOUR_CHAR_MODELS else CHARS_PER_TOKEN


def context_window_for_model(model: str | None, environ: dict | None = None,
                             disable_1m: bool = False) -> tuple[int | None, str]:
    """``(window, source)`` the way Claude Code sizes it (``ov``), or ``(None, "unknown")``.

    ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` forces 200k; a ``[1m]`` suffix gives 1M;
    otherwise the catalog window of the resolved model; a Claude model the
    catalog does not list gets Claude Code's 200k default.
    """
    environ = os.environ if environ is None else environ
    if not model or not isinstance(model, str):
        return None, "unknown"
    if disable_1m or _truthy(environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")):
        return 200_000, "CLAUDE_CODE_DISABLE_1M_CONTEXT"
    if re.search(r"\[1m\]", model, re.I):
        return 1_000_000, "model"
    canonical = resolve_model(model)
    if canonical is not None:
        return MODEL_WINDOWS.get(canonical, DEFAULT_CLAUDE_WINDOW), "model"
    if "claude-" in model.lower():
        return DEFAULT_CLAUDE_WINDOW, "model"
    return None, "unknown"


# --------------------------------------------------------------------------- #
# Installed Claude Code version (cached, bounded)
# --------------------------------------------------------------------------- #

_VERSION_CACHE: dict = {}
_VERSION_LOCK = threading.Lock()
VERSION_PROBE_TIMEOUT = 2.0
VERSION_CACHE_SECONDS = 600


def claude_code_version() -> str | None:
    """The installed Claude Code version (see ``_probe_claude_code_version``)."""
    return _probe_claude_code_version()


def _probe_claude_code_version() -> str | None:
    """``claude --version`` of the installed CLI, cached for 10 minutes.

    The probe has a 2-second timeout and every failure returns None, so the
    report never waits long on it.
    """
    now = time.monotonic()
    with _VERSION_LOCK:
        cached = _VERSION_CACHE.get("value")
        if cached is not None and now - cached[1] < VERSION_CACHE_SECONDS:
            return cached[0]
        version = None
        exe = shutil.which("claude")
        if exe:
            try:
                out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace",
                                     timeout=VERSION_PROBE_TIMEOUT)
                m = re.search(r"(\d+\.\d+\.\d+)", out.stdout or "")
                version = m.group(1) if m else None
            except (OSError, ValueError, subprocess.SubprocessError):
                version = None
        _VERSION_CACHE["value"] = (version, now)
        return version


def _positive_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def usage_scores(home: Path, now: float | None = None) -> dict[str, float]:
    """``name -> score`` from ``~/.claude.json`` ``skillUsage`` (Claude Code's ``Sct``)."""
    data = _read_json(Path(home) / ".claude.json")
    raw = data.get("skillUsage")
    if not isinstance(raw, dict):
        return {}
    now_ms = (time.time() if now is None else now) * 1000
    scores: dict[str, float] = {}
    for name, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        count = rec.get("usageCount")
        last = rec.get("lastUsedAt")
        if not isinstance(count, (int, float)) or isinstance(count, bool):
            continue
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            continue
        days = max(0.0, (now_ms - last) / 86_400_000)
        scores[str(name)] = float(count) * max(0.5 ** (days / 7), 0.1)
    return scores


def _lookup_usage(scores: dict[str, float], name: str) -> float:
    if name in scores:
        return scores[name]
    lowered = name.lower()
    for key, val in scores.items():
        if key.lower() == lowered:
            return val
    return 0.0


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #

def _pack_index(data_root: Path | None) -> dict[str, tuple[str, str]]:
    """``skill name -> (pack name, pack display name)`` from the bundled registry."""
    if data_root is None:
        return {}
    from jacked import packs

    index: dict[str, tuple[str, str]] = {}
    for name, pack in packs.load_registry(data_root).items():
        for skill in pack.skills:
            index.setdefault(skill, (name, pack.display_name or name))
    return index


def read_frontmatter_text(path: Path) -> tuple[str | None, str | None]:
    """The file text up to and including the closing frontmatter fence.

    Returns ``(text, None)``, or ``(None, reason)`` when the file cannot be read
    or the frontmatter does not close within ``MAX_FRONTMATTER_BYTES``. A file
    without frontmatter returns just its first line (nothing to parse).
    """
    try:
        with open(path, "rb") as fh:
            first = fh.readline(MAX_FRONTMATTER_BYTES + 1)
            if not first.rstrip(b"\r\n").rstrip(b" \t") == b"---":
                return first.decode("utf-8", "replace"), None
            chunks, total = [first], len(first)
            while total <= MAX_FRONTMATTER_BYTES:
                line = fh.readline(MAX_FRONTMATTER_BYTES + 1 - total)
                if not line:
                    break
                chunks.append(line)
                total += len(line)
                if line.rstrip(b"\r\n").rstrip(b" \t") == b"---":
                    return b"".join(chunks).decode("utf-8", "replace"), None
            if total > MAX_FRONTMATTER_BYTES:
                return None, "frontmatter is larger than 1 MB, not read"
            return b"".join(chunks).decode("utf-8", "replace"), None
    except OSError as exc:
        return None, f"cannot read the file ({type(exc).__name__})"


def _read_entry_file(path: Path, inv: "Inventory | None" = None, name: str = "",
                     source: str = "") -> tuple[dict, str | None] | None:
    text, problem = read_frontmatter_text(path)
    if text is None:
        logger.warning("Skill listing: skipped %s: %s", display_path(str(path)), problem)
        if inv is not None:
            inv.excluded.append({"name": name or path.parent.name, "source": source,
                                 "reason": problem})
        return None
    return parse_frontmatter(text)


def _make_entry(
    inv: Inventory, *, name: str, path: Path, fm: dict, err: str | None,
    source: str, group: str, group_label: str, overrides: dict | None,
) -> None:
    """Apply the inclusion rules for one file and append it to ``inv``."""
    if err:
        inv.yaml_warnings.append(YamlWarning(name=name, path=display_path(str(path)), error=err))
    if _truthy(fm.get("disable-model-invocation")):
        inv.excluded.append({"name": name, "source": source, "reason": "disable-model-invocation"})
        return
    description = _as_text(fm.get("description"))
    when_to_use = _as_text(fm.get("when_to_use"))
    if not description and not when_to_use:
        inv.excluded.append({"name": name, "source": source, "reason": "no description"})
        return
    mode = "on"
    if overrides is not None:
        raw_mode = overrides.get(name)
        if isinstance(raw_mode, str):
            mode = raw_mode
    if mode in ("off", "user-invocable-only"):
        inv.excluded.append({"name": name, "source": source, "reason": f"skillOverrides {mode}"})
        return
    if any(e.name == name for e in inv.entries):
        # Claude Code merges its sources by name and keeps the first one.
        inv.excluded.append({"name": name, "source": source, "reason": "duplicate name"})
        return
    name_only = mode == "name-only"
    inv.entries.append(ListingEntry(
        name=name, source=source, group=group, group_label=group_label,
        desc_len=_js_len(entry_text(description, when_to_use)), path=display_path(str(path)),
        name_only=name_only, protected=name_only,
    ))


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _read_js_string(src: str, i: int) -> tuple[str | None, int]:
    """Read one JS string literal starting at ``src[i]``. ``(None, i)`` if none."""
    if i >= len(src) or src[i] not in "'\"`":
        return None, i
    quote, i, out = src[i], i + 1, []
    while i < len(src):
        ch = src[i]
        if ch == "\\" and i + 1 < len(src):
            nxt = src[i + 1]
            if nxt == "u" and i + 5 < len(src):
                try:
                    out.append(chr(int(src[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nxt == "\n":
                i += 2
                continue
            out.append(_JS_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if quote == "`" and ch == "$" and src[i + 1:i + 2] == "{":
            return None, i  # template with interpolation: not a static string
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None, i


def _skip_js_string(src: str, i: int) -> int:
    """Index just past the string literal that opens at ``src[i]`` (always advances)."""
    quote, i = src[i], i + 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return len(src)


_META_KEY_RE = re.compile(r"(name|description|whenToUse)\s*:\s*")
_PLUS_RE = re.compile(r"\s*\+\s*")
_META_OPEN_RE = re.compile(r"export\s+const\s+meta\s*=\s*\{")


def parse_workflow_meta(text: str) -> dict | None:
    """Static ``name``/``description``/``whenToUse`` of a plugin workflow.

    A plugin workflow file opens with ``export const meta = { ... }``. Only the
    top level of that object literal is read, and only plain string values
    (two literals joined with ``+`` are also read). Anything else returns
    ``None`` for that key; a file without the block returns ``None``.
    """
    m = _META_OPEN_RE.search(text)
    if not m:
        return None
    i, depth, meta = m.end(), 1, {}
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch in "'\"`":
            i = _skip_js_string(text, i)
            continue
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = len(text) if nl == -1 else nl + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif depth == 1:
            km = _META_KEY_RE.match(text, i)
            prev = text[i - 1] if i > 0 else ","
            if km and (prev in ",{ \t\r\n"):
                j = km.end()
                value, j2 = _read_js_string(text, j)
                if value is not None:
                    while True:
                        plus = _PLUS_RE.match(text, j2)
                        if not plus:
                            break
                        more, j3 = _read_js_string(text, plus.end())
                        if more is None:
                            value = None
                            break
                        value, j2 = value + more, j3
                if value is not None:
                    meta[km.group(1)] = value
                    i = j2
                    continue
                i = j
                continue
        i += 1
    return meta or None


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _guarded(inv: "Inventory", name: str, source: str, path: Path, step) -> None:
    """Run one file's enumeration step; any failure excludes only that file.

    A permission error, an encoding problem, or a parser bug in one skill must
    never stop the report. The file is listed in ``excluded`` with the error
    type and logged.
    """
    try:
        step()
    except Exception as exc:  # noqa: BLE001 - isolate the one bad file
        logger.warning("Skill listing: skipped %s (%s): %s: %s", name,
                       display_path(str(path)), type(exc).__name__, exc)
        inv.excluded.append({"name": name, "source": source,
                             "reason": f"could not be read ({type(exc).__name__})"})


def _sorted_dirs(root: Path) -> list[Path]:
    try:
        return sorted((p for p in root.iterdir() if _is_dir(p)), key=lambda p: p.name)
    except OSError:
        return []


def _sorted_md(root: Path) -> list[Path]:
    try:
        return sorted((p for p in root.glob("*.md") if p.is_file()), key=lambda p: p.name)
    except OSError:
        return []


def _enabled_plugins(home: Path, enabled: dict) -> list[tuple[str, Path]]:
    """``(plugin name, install path)`` for every plugin set true, in settings order."""
    installed = _read_json(Path(home) / ".claude" / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins")
    if not isinstance(plugins, dict):
        return []
    out: list[tuple[str, Path]] = []
    for key, on in enabled.items():
        if on is not True:
            continue
        records = plugins.get(key)
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            continue
        install = records[0].get("installPath")
        if not isinstance(install, str) or not install:
            continue
        out.append((str(key).split("@", 1)[0], Path(install)))
    return out


def _synced_dir(home: Path) -> Path | None:
    """The claude.ai synced-skills folder of the signed-in account, if any."""
    data = _read_json(Path(home) / ".claude.json")
    acct = data.get("oauthAccount")
    if not isinstance(acct, dict):
        return None
    org, user = acct.get("organizationUuid"), acct.get("accountUuid")
    if not (isinstance(org, str) and isinstance(user, str) and org and user):
        return None
    path = Path(home) / ".claude" / "skills" / "synced" / f"{org}_{user}"
    return path if _is_dir(path) else None


def _not_counted_subdirs(inv: Inventory, root: Path, owner: str) -> None:
    """Report command sub-folders: their naming is not confirmed, so they are not counted."""
    try:
        subdirs = sorted(p for p in root.iterdir() if _is_dir(p))
    except OSError:
        return
    for sub in subdirs:
        try:
            count = sum(1 for _ in sub.rglob("*.md"))
        except OSError:
            count = 0
        if count:
            inv.not_counted.append({
                "path": display_path(str(sub)), "owner": owner,
                "reason": f"{count} command file(s) in a sub-folder; "
                          "jacked does not count namespaced commands",
            })


def _not_counted_plugin_manifest(inv: Inventory, plugin: str, root: Path) -> None:
    """Report custom ``commands``/``skills`` paths from a plugin's plugin.json."""
    manifest = _read_json(root / ".claude-plugin" / "plugin.json")
    for key in ("commands", "skills"):
        value = manifest.get(key)
        values = value if isinstance(value, list) else [value]
        outside = []
        for item in values:
            if not isinstance(item, str) or not item.strip():
                continue
            norm = item.strip().replace("\\", "/")
            while norm.startswith("./"):
                norm = norm[2:]
            norm = norm.rstrip("/")
            if norm != key and not norm.startswith(key + "/"):
                outside.append(item)
        if not outside:
            continue  # every path is inside the default folder, which is counted
        inv.not_counted.append({
            "path": display_path(str(root / ".claude-plugin" / "plugin.json")),
            "owner": f"{plugin} (plugin)",
            "reason": f"plugin.json sets custom {key} paths; jacked counts only the "
                      f"default {key}/ folder",
        })


def _read_workflow(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_WORKFLOW_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_WORKFLOW_BYTES:
        logger.warning("Skill listing: read only the first 1 MB of %s", display_path(str(path)))
        data = data[:MAX_WORKFLOW_BYTES]
    return data.decode("utf-8", "replace")


def enumerate_entries(
    home: Path, settings: dict | None = None, *, data_root: Path | None = None,
    include_builtins: bool = True,
) -> Inventory:
    """Every model-visible listing entry, in Claude Code's listing order.

    Order: user skills, user commands, plugin workflows, plugin commands,
    plugin skills (plugins in ``enabledPlugins`` order), Claude Code's built-in
    entries, then the signed-in account's claude.ai synced skills. Layouts the
    estimate does not count are listed in ``Inventory.not_counted``.
    """
    home = Path(home)
    settings = read_listing_settings(home) if settings is None else settings
    overrides = settings.get("skill_overrides") or {}
    packs_by_skill = _pack_index(data_root)
    inv = Inventory()
    claude = home / ".claude"

    def read(path: Path, name: str, source: str):
        return _read_entry_file(path, inv, name, source)

    # User skills (pack skills are symlinks into ~/.agents/skills; is_dir follows them).
    def user_skill(skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            return
        name = skill_dir.name
        parsed = read(skill_md, name, "user")
        if parsed is None:
            return
        fm, err = parsed
        if name in packs_by_skill:
            pack_name, pack_label = packs_by_skill[name]
            source, group, label = "user", f"pack:{pack_name}", f"{pack_label} (skill pack)"
        else:
            source, group, label = "user", "user", SOURCE_LABELS["user"]
        _make_entry(inv, name=name, path=skill_md, fm=fm, err=err, source=source,
                    group=group, group_label=label, overrides=overrides)

    for skill_dir in _sorted_dirs(claude / "skills"):
        _guarded(inv, skill_dir.name, "user", skill_dir, lambda d=skill_dir: user_skill(d))

    # User commands (top level only; sub-folders are reported, not counted).
    def user_command(cmd: Path) -> None:
        parsed = read(cmd, cmd.stem, "command")
        if parsed is None:
            return
        fm, err = parsed
        _make_entry(inv, name=cmd.stem, path=cmd, fm=fm, err=err, source="command",
                    group="command", group_label=SOURCE_LABELS["command"], overrides=overrides)

    for cmd in _sorted_md(claude / "commands"):
        _guarded(inv, cmd.stem, "command", cmd, lambda c=cmd: user_command(c))
    _guarded(inv, "commands", "command", claude / "commands",
             lambda: _not_counted_subdirs(inv, claude / "commands", SOURCE_LABELS["command"]))

    plugins = _enabled_plugins(home, settings.get("enabled_plugins") or {})

    def plugin_workflow(plugin: str, wf: Path) -> None:
        text = _read_workflow(wf)
        meta = parse_workflow_meta(text) if text is not None else None
        if meta is None:
            inv.excluded.append({"name": f"{plugin}:{wf.stem}", "source": "plugin",
                                 "reason": "workflow meta not readable"})
            return
        fm = {"description": meta.get("description"), "when_to_use": meta.get("whenToUse")}
        _make_entry(inv, name=f"{plugin}:{meta.get('name') or wf.stem}", path=wf, fm=fm,
                    err=None, source="plugin", group=f"plugin:{plugin}",
                    group_label=f"{plugin} (plugin)", overrides=None)

    def plugin_command(plugin: str, cmd: Path) -> None:
        parsed = read(cmd, f"{plugin}:{cmd.stem}", "plugin")
        if parsed is None:
            return
        fm, err = parsed
        _make_entry(inv, name=f"{plugin}:{cmd.stem}", path=cmd, fm=fm, err=err,
                    source="plugin", group=f"plugin:{plugin}", group_label=f"{plugin} (plugin)",
                    overrides=None)

    def plugin_skill(plugin: str, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            return
        parsed = read(skill_md, f"{plugin}:{skill_dir.name}", "plugin")
        if parsed is None:
            return
        fm, err = parsed
        base = _as_text(fm.get("name")) or skill_dir.name
        _make_entry(inv, name=f"{plugin}:{base}", path=skill_md, fm=fm, err=err,
                    source="plugin", group=f"plugin:{plugin}", group_label=f"{plugin} (plugin)",
                    overrides=None)

    def workflow_files(root: Path) -> list[Path]:
        try:
            return sorted(p for p in (root / "workflows").glob("*.js") if p.is_file())
        except OSError:
            return []

    # Plugin workflows, then plugin commands, then plugin skills (Claude Code's
    # merge order as /context lists it).
    for plugin, root in plugins:
        _guarded(inv, plugin, "plugin", root,
                 lambda p=plugin, r=root: _not_counted_plugin_manifest(inv, p, r))
        for wf in workflow_files(root):
            _guarded(inv, f"{plugin}:{wf.stem}", "plugin", wf,
                     lambda p=plugin, w=wf: plugin_workflow(p, w))
    for plugin, root in plugins:
        for cmd in _sorted_md(root / "commands"):
            _guarded(inv, f"{plugin}:{cmd.stem}", "plugin", cmd,
                     lambda p=plugin, c=cmd: plugin_command(p, c))
        _guarded(inv, f"{plugin}:commands", "plugin", root / "commands",
                 lambda p=plugin, r=root: _not_counted_subdirs(inv, r / "commands", f"{p} (plugin)"))
    for plugin, root in plugins:
        for skill_dir in _sorted_dirs(root / "skills"):
            _guarded(inv, f"{plugin}:{skill_dir.name}", "plugin", skill_dir,
                     lambda p=plugin, d=skill_dir: plugin_skill(p, d))

    if include_builtins:
        no_bundled = bool(settings.get("disable_bundled_skills"))
        for name, desc_len, bundled in BUILTIN_ENTRIES:
            if bundled and no_bundled:
                continue  # disableBundledSkills / CLAUDE_CODE_DISABLE_BUNDLED_SKILLS
            mode = overrides.get(name) if isinstance(overrides.get(name), str) else "on"
            if mode in ("off", "user-invocable-only"):
                continue
            inv.entries.append(ListingEntry(
                name=name, source="builtin", group="builtin",
                group_label=SOURCE_LABELS["builtin"], desc_len=desc_len,
                name_only=mode == "name-only", protected=bundled or mode == "name-only",
            ))

    synced = _synced_dir(home)
    if synced is not None:
        # A synced skill whose bare name collides with another skill answers
        # only to ``anthropic-skills:<name>`` (Claude Code's ``Eyt``). Plugin
        # skills also claim their bare name, so both forms count as taken.
        taken = {e.name for e in inv.entries}
        taken |= {e.name.split(":", 1)[1] for e in inv.entries if ":" in e.name}
        def synced_skill(skill_dir: Path) -> None:
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                return
            parsed = read(skill_md, skill_dir.name, "synced")
            if parsed is None:
                return
            fm, err = parsed
            name = skill_dir.name
            if name in taken:
                name = f"{SYNCED_PREFIX}{name}"
            _make_entry(inv, name=name, path=skill_md, fm=fm, err=err,
                        source="synced", group="synced", group_label=SOURCE_LABELS["synced"],
                        overrides=overrides)

        for skill_dir in _sorted_dirs(synced):
            _guarded(inv, skill_dir.name, "synced", skill_dir,
                     lambda d=skill_dir: synced_skill(d))
    return inv


# --------------------------------------------------------------------------- #
# Budget math + drop simulation
# --------------------------------------------------------------------------- #

def budget_chars(
    fraction: float, context_window: int, env_budget: int | None = None,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> int:
    """Claude Code's ``h4e``: the env var wins, else window x factor x fraction."""
    if env_budget:
        return int(env_budget)
    return max(1, math.floor(context_window * chars_per_token * fraction))


def listing_chars(entries: list[ListingEntry], max_desc: int) -> int:
    """Characters of the full listing (every description, cut to ``max_desc``)."""
    if not entries:
        return 0
    return sum(e.full_chars(max_desc) for e in entries) + SEPARATOR_CHARS * (len(entries) - 1)


@dataclass
class Simulation:
    budget: int
    full_chars: int          # listing with every description (cut to max_desc)
    emitted_chars: int       # listing Claude Code actually emits
    fits: bool
    dropped: list[ListingEntry]


def simulate(entries: list[ListingEntry], max_desc: int, budget: int) -> Simulation:
    """Claude Code's ``pXt``/``ERt`` drop pass."""
    full = listing_chars(entries, max_desc)
    if full <= budget or not entries:
        return Simulation(budget, full, full, True, [])
    seps = SEPARATOR_CHARS * (len(entries) - 1)
    base = sum(e.full_chars(max_desc) if e.protected else e.bare_chars() for e in entries) + seps
    remaining = budget - base
    droppable = [(i, e) for i, e in enumerate(entries) if not e.protected]
    # Stable sort: descending usage, ties keep listing order.
    droppable.sort(key=lambda pair: -pair[1].usage)
    kept: set[int] = set()
    for idx, entry in droppable:
        extra = entry.full_chars(max_desc) - entry.bare_chars()
        if extra <= remaining:
            kept.add(idx)
            remaining -= extra
    dropped = [e for i, e in enumerate(entries) if not e.protected and i not in kept]
    emitted = budget - remaining
    return Simulation(budget, full, emitted, False, dropped)


def tokens(chars: int, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    return int(round(chars / chars_per_token))


# --------------------------------------------------------------------------- #
# Recommendation
# --------------------------------------------------------------------------- #

def recommend(
    entries: list[ListingEntry], *, fraction: float, max_desc: int,
    context_window: int, env_budget: int | None = None,
    chars_per_token: int = CHARS_PER_TOKEN,
) -> dict | None:
    """Settings that show more descriptions, or ``None``.

    ``None`` when every description already fits, or when no option drops
    fewer descriptions than the current settings. The recommended fraction is
    never below the current one, so Apply can never make the listing worse.

    1. Keep the current budget: the largest ``maxDescChars`` in 150..1536
       (step 50) that fits.
    2. Else cap at 250 characters and take the smallest fraction in
       0.015 / 0.02 / 0.025 / 0.03 that is above the current one and fits. A
       budget from ``SLASH_COMMAND_TOOL_CHAR_BUDGET`` cannot be raised this way.
    3. Else the option (cap 150 or 250, current or a higher fraction) that
       drops the fewest descriptions, with ``fits`` False, if it beats now.
    """
    cpt = chars_per_token
    budget = budget_chars(fraction, context_window, env_budget, cpt)
    current = simulate(entries, max_desc, budget)
    if current.fits:
        return None
    for cand in sorted(RECOMMEND_MAX_DESC_CANDIDATES, reverse=True):
        if listing_chars(entries, cand) <= budget:
            return _rec(entries, cand, fraction, current, budget, env_budget, cpt)
    higher = [] if env_budget else [f for f in RECOMMEND_FALLBACK_FRACTIONS if f > fraction]
    for frac in higher:
        new_budget = budget_chars(frac, context_window, None, cpt)
        if listing_chars(entries, RECOMMEND_FALLBACK_MAX_DESC) <= new_budget:
            return _rec(entries, RECOMMEND_FALLBACK_MAX_DESC, frac, current, new_budget,
                        None, cpt)
    best = None
    for frac in [fraction] + higher:
        new_budget = budget_chars(frac, context_window, env_budget, cpt)
        for cap in (RECOMMEND_FALLBACK_MAX_DESC, RECOMMEND_MAX_DESC_CANDIDATES[0]):
            dropped = len(simulate(entries, cap, new_budget).dropped)
            key = (dropped, frac, -cap)
            if best is None or key < best[0]:
                best = (key, cap, frac, new_budget)
    if best is None or best[0][0] >= len(current.dropped):
        return None
    _, cap, frac, new_budget = best
    return _rec(entries, cap, frac, current, new_budget, env_budget, cpt)


def _rec(entries, max_desc, fraction, current: Simulation, budget, env_budget, cpt) -> dict:
    after = simulate(entries, max_desc, budget)
    extra = max(0, tokens(after.emitted_chars, cpt) - tokens(current.emitted_chars, cpt))
    return {
        "max_desc_chars": int(max_desc),
        "budget_fraction": float(fraction),
        "fits": after.fits,
        "dropped_after": len(after.dropped),
        "listing_tokens_after": tokens(after.emitted_chars, cpt),
        "extra_tokens": extra,
        "budget_from_env": bool(env_budget),
        "summary": recommendation_text(max_desc, fraction, extra, bool(env_budget), after.fits),
    }


def _format_tokens(n: int) -> str:
    if n >= 10_000:
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _format_percent(fraction: float) -> str:
    return f"{round(fraction * 100, 2):g}%"


def recommendation_text(
    max_desc: int, fraction: float, extra_tokens: int, from_env: bool, fits: bool = True,
) -> str:
    """One plain sentence that states exactly what the apply step writes."""
    if from_env:
        text = (f"Cap descriptions at {max_desc} characters. The budget comes from "
                f"{ENV_BUDGET_VAR}, so jacked cannot raise it")
    else:
        text = (f"Cap descriptions at {max_desc} characters and use a "
                f"{_format_percent(fraction)} budget")
    if extra_tokens > 0:
        text += f" (about {_format_tokens(extra_tokens)} more tokens per session)"
    text += "."
    if not fits:
        text += " Some skills still show without a description. Turn off skills you do not use."
    return text


def applied_text(max_desc: int, fraction: float, dropped_after: int,
                 window: int | None = None, overridden_by: list[str] | None = None) -> str:
    """Confirmation after a write, in the same plain style, naming the window.

    When another settings file also sets a listing value, it wins over the
    user settings, so the message says the saved values are NOT in effect.
    """
    if overridden_by:
        return (f"Saved to your user settings: descriptions capped at {max_desc} characters "
                f"and a {_format_percent(fraction)} budget. These saved values are overridden "
                f"by {', '.join(overridden_by)}, which also sets them, so Claude Code does not "
                "use them. Change that file to change the listing.")
    where = f" with a {window_label(window)} context window" if window else ""
    text = (f"Saved. Descriptions are capped at {max_desc} characters and the budget is "
            f"{_format_percent(fraction)}.")
    if dropped_after:
        noun = "skill still shows" if dropped_after == 1 else "skills still show"
        text += f" {dropped_after} {noun} without a description{where}."
    else:
        text += f" Every skill shows its description{where}."
    return text + " New Claude Code sessions use the change."


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _entry_dict(e: ListingEntry) -> dict:
    return {
        "name": e.name, "source": e.source, "group": e.group, "group_label": e.group_label,
        "usage": round(e.usage, 3), "path": e.path,
    }


def _group_dropped(dropped: list[ListingEntry]) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in dropped:
        g = groups.setdefault(e.group, {
            "key": e.group, "label": e.group_label, "source": e.source, "names": [],
        })
        g["names"].append(e.name)
    return sorted(groups.values(), key=lambda g: (-len(g["names"]), g["label"].lower()))


def _window_summary(entries, settings, window, cpt) -> dict:
    budget = budget_chars(settings["budget_fraction"], window, settings["env_budget"], cpt)
    sim = simulate(entries, settings["max_desc_chars"], budget)
    return {
        "context_window": window,
        "budget_chars": budget,
        "budget_tokens": tokens(budget, cpt),
        "full_chars": sim.full_chars,
        "full_tokens": tokens(sim.full_chars, cpt),
        "listing_tokens": tokens(sim.emitted_chars, cpt),
        "fits": sim.fits,
        "dropped_count": len(sim.dropped),
    }


def _format_chars(n: int) -> str:
    return _format_tokens(n)


def window_label(window: int) -> str:
    if window == 1_000_000:
        return "1M-token"
    if window % 1000 == 0 and window < 1_000_000:
        return f"{window // 1000}k-token"
    return f"{window:,}-token"


def summary_short(report: dict) -> str:
    """One status line that always names the window it is about."""
    label = window_label(report["context_window"])
    size = f"({_format_chars(report['full_chars'])} of {_format_chars(report['budget_chars'])} characters)"
    n = report["dropped_count"]
    if n == 0:
        text = f"With a {label} context window, every skill shows its description {size}."
    else:
        noun = "skill shows" if n == 1 else "skills show"
        text = (f"With a {label} context window, {n} {noun} without a description, "
                f"so Claude rarely picks them {size}.")
    for key, other in sorted(report["windows"].items(), key=lambda kv: int(kv[0])):
        if int(key) == report["context_window"] or not other["dropped_count"]:
            continue
        m = other["dropped_count"]
        noun = "skill shows" if m == 1 else "skills show"
        text += f" With a {window_label(int(key))} window, {m} {noun} without a description."
    if not report.get("window_known", True):
        text = (f"Your model's context window is unknown, so this uses a {label} window. "
                + text)
    return text


def summary_text(report: dict) -> str:
    """The CLI status line."""
    return "Skill listing: " + summary_short(report)


_PROBE = object()


def build_report(
    home: Path, *, context_window: int | None = None,
    environ: dict | None = None, data_root: Path | None = None, now: float | None = None,
    chars_per_token: int | None = None, cwd: Path | None = None, claude_version=_PROBE,
) -> dict:
    """Full skill-listing report (JSON-serializable).

    ``context_window`` defaults to the window of the configured model (see
    ``context_window_for_model``); when that is unknown the report uses 1M,
    says so, and still reports both windows. ``chars_per_token`` defaults to
    the factor for the configured model.
    """
    home = Path(home)
    environ = os.environ if environ is None else environ
    settings = read_listing_settings(home, environ, cwd)
    model = settings.get("model")
    cpt = chars_per_token or chars_per_token_for_model(model)
    derived, derived_source = context_window_for_model(
        model, environ, settings.get("disable_1m_context", False))
    if context_window is not None:
        window, window_source, known = context_window, "selected", True
    elif derived is not None:
        window, window_source, known = derived, derived_source, True
    else:
        window, window_source, known = DEFAULT_CONTEXT_WINDOW, "unknown", False

    inv = enumerate_entries(home, settings, data_root=data_root)
    scores = usage_scores(home, now)
    for e in inv.entries:
        e.usage = _lookup_usage(scores, e.name)

    entries = inv.entries
    max_desc = settings["max_desc_chars"]
    budget = budget_chars(settings["budget_fraction"], window, settings["env_budget"], cpt)
    sim = simulate(entries, max_desc, budget)
    rec = recommend(entries, fraction=settings["budget_fraction"], max_desc=max_desc,
                    context_window=window, env_budget=settings["env_budget"],
                    chars_per_token=cpt)

    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1

    version = claude_code_version() if claude_version is _PROBE else claude_version
    version_note = None
    if version and version != CALIBRATED_CLAUDE_VERSION:
        version_note = (f"jacked calibrated this estimate for Claude Code "
                        f"{CALIBRATED_CLAUDE_VERSION}. You run {version}, so the numbers "
                        "can be a little different.")

    windows = sorted(set(REPORT_WINDOWS) | {window})
    report = {
        "settings": {k: v for k, v in settings.items()
                     if k not in ("skill_overrides", "enabled_plugins")},
        "context_window": window,
        "window_source": window_source,
        "window_known": known,
        "derived_window": derived,
        "model_resolved": resolve_model(model),
        "chars_per_token": cpt,
        "entry_count": len(entries),
        "counts_by_source": counts,
        "full_chars": sim.full_chars,
        "full_tokens": tokens(sim.full_chars, cpt),
        "listing_chars": sim.emitted_chars,
        "listing_tokens": tokens(sim.emitted_chars, cpt),
        "budget_chars": budget,
        "budget_tokens": tokens(budget, cpt),
        "fits": sim.fits,
        "dropped_count": len(sim.dropped),
        "dropped": [_entry_dict(e) for e in sim.dropped],
        "dropped_groups": _group_dropped(sim.dropped),
        "windows": {str(w): _window_summary(entries, settings, w, cpt) for w in windows},
        "recommendation": rec,
        "strict_yaml_warnings": [asdict(w) for w in inv.yaml_warnings],
        "excluded": inv.excluded,
        "not_counted": inv.not_counted,
        "claude_code_version": version,
        "calibrated_for": CALIBRATED_CLAUDE_VERSION,
        "version_note": version_note,
        "limits": {
            "max_desc_chars_min": MAX_DESC_MIN, "max_desc_chars_max": MAX_DESC_MAX,
            "budget_fraction_max": FRACTION_MAX,
        },
    }
    report["summary_short"] = summary_short(report)
    report["summary"] = summary_text(report)
    return report


def inventory_snapshot(
    home: Path, *, data_root: Path | None = None, now: float | None = None,
) -> dict:
    """Names, description lengths, sources and usage scores (no file contents)."""
    settings = read_listing_settings(home)
    inv = enumerate_entries(home, settings, data_root=data_root)
    scores = usage_scores(home, now)
    return {
        "entries": [
            {"name": e.name, "source": e.source, "group": e.group, "desc_len": e.desc_len,
             "name_only": e.name_only, "protected": e.protected,
             "usage": round(_lookup_usage(scores, e.name), 4)}
            for e in inv.entries
        ],
    }


def entries_from_snapshot(snapshot: dict) -> list[ListingEntry]:
    return [
        ListingEntry(
            name=row["name"], source=row["source"], group=row.get("group", row["source"]),
            group_label=row.get("group", row["source"]), desc_len=int(row["desc_len"]),
            name_only=bool(row.get("name_only")), protected=bool(row.get("protected")),
            usage=float(row.get("usage", 0.0)),
        )
        for row in snapshot["entries"]
    ]


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

def validate_max_desc_chars(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ListingSettingsError("skillListingMaxDescChars must be a whole number.")
    if not MAX_DESC_MIN <= value <= MAX_DESC_MAX:
        raise ListingSettingsError(
            f"skillListingMaxDescChars must be from {MAX_DESC_MIN} to {MAX_DESC_MAX}."
        )
    return value


def validate_budget_fraction(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ListingSettingsError("skillListingBudgetFraction must be a number.")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= FRACTION_MAX:
        raise ListingSettingsError(
            f"skillListingBudgetFraction must be more than 0 and at most {FRACTION_MAX:g}."
        )
    return value


def apply_listing_settings(home: Path, max_desc_chars, budget_fraction) -> dict:
    """Write the two listing keys into ``~/.claude/settings.json``.

    Every other key stays as it is. Raises ``ListingSettingsError`` for a bad
    value (nothing written) and ``SettingsUnreadableError`` when settings.json
    exists but cannot be parsed (nothing written). Callers that share the
    dashboard's in-process ``_settings_lock`` hold it around this call.
    """
    from jacked.memory.settings_io import read_settings, write_settings

    max_desc = validate_max_desc_chars(max_desc_chars)
    fraction = validate_budget_fraction(budget_fraction)
    path = settings_file(home)
    settings = read_settings(path)
    settings["skillListingMaxDescChars"] = max_desc
    settings["skillListingBudgetFraction"] = fraction
    path.parent.mkdir(parents=True, exist_ok=True)
    write_settings(path, settings)
    return {"skillListingMaxDescChars": max_desc, "skillListingBudgetFraction": fraction}


def reset_listing_settings(home: Path) -> bool:
    """Remove both listing keys (Claude Code defaults apply). True when a key was removed."""
    from jacked.memory.settings_io import read_settings, write_settings

    path = settings_file(home)
    settings = read_settings(path)
    removed = False
    for key in ("skillListingMaxDescChars", "skillListingBudgetFraction"):
        if key in settings:
            del settings[key]
            removed = True
    if removed:
        write_settings(path, settings)
    return removed


def apply_action(
    home: Path, action: str, *, context_window: int | None = None,
    max_desc_chars=None, budget_fraction=None, data_root: Path | None = None,
    environ: dict | None = None, claude_version=_PROBE,
) -> dict:
    """Apply ``recommended`` / ``explicit`` values or ``reset``, then report.

    The one implementation behind both the dashboard PUT and the CLI flags.
    Returns the fresh report with two extra keys: ``applied`` (the written
    values, or None) and ``message`` (one plain sentence). Raises
    ``ListingSettingsError`` for a bad request and ``SettingsUnreadableError``
    when settings.json exists but cannot be parsed; nothing is written then.
    """
    def fresh() -> dict:
        return build_report(home, context_window=context_window, environ=environ,
                            data_root=data_root, claude_version=claude_version)

    if action == "reset":
        removed = reset_listing_settings(home)
        report = fresh()
        report["applied"] = None
        report["message"] = (
            "Removed the skill listing settings. Claude Code uses its defaults."
            if removed else "The skill listing settings were already at the defaults."
        )
        return report

    if action == "recommended":
        before = fresh()
        if before["settings"].get("settings_unreadable"):
            from jacked.memory.settings_io import SettingsUnreadableError

            raise SettingsUnreadableError(f"{before['settings']['settings_path']} is unreadable")
        rec = before["recommendation"]
        if rec is None:
            before["applied"] = None
            before["message"] = "Every skill already shows its description. Nothing changed."
            return before
        max_desc_chars, budget_fraction = rec["max_desc_chars"], rec["budget_fraction"]
    elif action == "explicit":
        if max_desc_chars is None or budget_fraction is None:
            raise ListingSettingsError("Give both max_desc_chars and budget_fraction.")
    else:
        raise ListingSettingsError(f"Unknown action: {action}")

    applied = apply_listing_settings(home, max_desc_chars, budget_fraction)
    report = fresh()
    report["applied"] = applied
    report["message"] = applied_text(
        applied["skillListingMaxDescChars"], applied["skillListingBudgetFraction"],
        report["dropped_count"], report["context_window"],
        report["settings"].get("listing_keys_overridden_by") or None,
    )
    return report
