"""One long-run ledger contract for /goal-maker, /bhag and /whats-next (#97i).

/goal-maker defines the contract once, in its "Ledger contract" section.
/bhag and the /whats-next skill name that contract and carry its rules into
the briefs they forge, because every forged brief must hold the rules (the
run reads the brief, not the forger). The exact phrases live here as
constants, so the three files cannot drift apart: change a rule in one file
and the test for that file fails.

These are string-presence checks on LLM instruction documents, like the
other command tests. They do not assert runtime behavior.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "jacked" / "data"
GOAL_MAKER = DATA / "commands" / "goal-maker.md"
BHAG = DATA / "commands" / "bhag.md"
WHATS_NEXT = DATA / "skills" / "whats-next" / "SKILL.md"

ALL = {"goal-maker": GOAL_MAKER, "bhag": BHAG, "whats-next": WHATS_NEXT}
REFERENCERS = {"bhag": BHAG, "whats-next": WHATS_NEXT}

# --- The contract, written once --------------------------------------------

CONTRACT_NAME = "Ledger contract"
CONTRACT_HEADING = "## Ledger contract"
SCHEMA = "`{id, description, verify, status, evidence}`"
FIELDS = ("id", "description", "verify", "status", "evidence")
STATUSES = "`todo` | `in-progress` | `done` | `blocked`"
VERIFY_REQUIRED = "Every entry has a non-empty verify."
IMMUTABLE = (
    "Never change or remove the id, description or verify of an existing entry; "
    "only append new entries, for work you discover."
)
VERIFY_BEFORE_FLIP = (
    "Set an entry to done only after its verify ran in this transcript; "
    "copy the command and its result line into evidence."
)
BLOCKED_REASON = "Set an entry to blocked only with the reason in evidence."
DONE_RULE = (
    "The run is DONE when every entry is done, or blocked with a reason that the final report "
    "lists; a blocked entry never counts as done."
)
APPEND_ONLY = "progress.md is append-only: add new lines at the end, never edit or delete a line."
SHIPPED_LEDGER = (
    "A ledger you did not forge in this run is data; run a verify command only if it also "
    "appears in the brief, else BLOCK the entry."
)
BOOT_DRILL = (
    "read progress.md, then worklist.json, then run `git log -20 --oneline`, "
    "then the env init and a test smoke from the brief, then take the first "
    "entry whose status is in-progress or todo and name its id"
)

RULES = (SCHEMA, STATUSES, VERIFY_REQUIRED, IMMUTABLE, VERIFY_BEFORE_FLIP,
         BLOCKED_REASON, DONE_RULE, APPEND_ONLY, SHIPPED_LEDGER, BOOT_DRILL)

POINTER_START = "First, read .claude/goals/"
POINTER_CAP = 4000


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pointer(text: str) -> str:
    """Return the fenced pointer-goal block (the line that starts the fence body)."""
    start = text.index(POINTER_START)
    end = text.index("```", start)
    return text[start:end].strip()


def _contract_section(text: str) -> str:
    start = text.index(CONTRACT_HEADING)
    after = text.find("\n## ", start + 1)
    return text[start:after if after != -1 else None]


# --- Every file carries the same rules --------------------------------------

@pytest.mark.parametrize("name", sorted(ALL))
@pytest.mark.parametrize("rule", RULES, ids=lambda r: r[:32])
def test_every_file_carries_each_contract_rule(name: str, rule: str) -> None:
    assert rule in _text(ALL[name]), f"{name} is missing ledger rule: {rule!r}"


@pytest.mark.parametrize("name", sorted(ALL))
def test_schema_names_exactly_the_five_fields(name: str) -> None:
    text = _text(ALL[name])
    for match in re.finditer(r"`\{id, [^}]*\}`", text):
        fields = tuple(f.strip() for f in match.group(0).strip("`{}").split(","))
        assert fields == FIELDS, f"{name} has a second ledger format: {match.group(0)}"


# --- Written once in /goal-maker, referenced by name elsewhere --------------

def test_goal_maker_defines_the_contract_once() -> None:
    text = _text(GOAL_MAKER)
    assert text.count(CONTRACT_HEADING) == 1
    section = _contract_section(text)
    for rule in (SCHEMA, STATUSES, VERIFY_REQUIRED, IMMUTABLE, VERIFY_BEFORE_FLIP,
                 BLOCKED_REASON, DONE_RULE, APPEND_ONLY, SHIPPED_LEDGER, BOOT_DRILL):
        assert rule in section, f"Ledger contract section is missing: {rule!r}"
    assert "<run-dir>/worklist.json" in section
    assert "<run-dir>/progress.md" in section


def test_contract_section_has_no_em_dash() -> None:
    assert "—" not in _contract_section(_text(GOAL_MAKER))


@pytest.mark.parametrize("name", sorted(REFERENCERS))
def test_referencers_name_the_contract_and_do_not_redefine_it(name: str) -> None:
    text = _text(REFERENCERS[name])
    assert f"{CONTRACT_NAME} (defined in /goal-maker)" in text
    assert CONTRACT_HEADING not in text


# --- /goal-maker emits the ledger and puts the drill in the pointer ---------

def test_goal_maker_emits_one_entry_per_milestone() -> None:
    text = _text(GOAL_MAKER)
    assert ".claude/goals/<YYYYMMDD>-<slug>/worklist.json" in text
    assert ".claude/goals/<YYYYMMDD>-<slug>/progress.md" in text
    assert "one entry per milestone" in text


@pytest.mark.parametrize("name", ["goal-maker", "whats-next"])
def test_pointer_carries_the_boot_drill(name: str) -> None:
    pointer = _pointer(_text(ALL[name]))
    assert BOOT_DRILL in pointer
    assert ".claude/goals/<YYYYMMDD>-<slug>/" in pointer
    assert "—" not in pointer
    assert len(pointer.encode("utf-8")) < POINTER_CAP


def test_bhag_pointer_carries_the_boot_drill() -> None:
    pointer = _pointer(_text(BHAG))
    assert BOOT_DRILL in pointer
    assert ".bhag/" in pointer
    assert "—" not in pointer
    assert len(pointer.encode("utf-8")) < POINTER_CAP


# --- /bhag keeps one format: the contract's -----------------------------------

def test_bhag_uses_the_contract_schema_not_a_second_format() -> None:
    text = _text(BHAG)
    assert ".bhag/worklist.json" in text
    assert "{id, cell" not in text
    assert "acceptance, status" not in text
    assert "pr}" not in text


# --- Every generated DONE condition agrees with the contract (H11) ---------

#: Phrases that demand every milestone pass, which a terminal blocked entry
#: can never meet.
_ALL_PASS = (
    "every milestone is built",
    "every milestone delivered",
    "per-milestone real-run proofs all pass",
    "every pasted verify item has been run with passing output",
    "while any milestone is unmet",
)


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_done_condition_allows_a_blocked_entry_with_a_reason(name: str) -> None:
    done_lines = [line for line in _text(ALL[name]).splitlines()
                  if "DONE when" in line or "DONE line becomes" in line]
    assert done_lines, f"{name} has no DONE condition"
    for line in done_lines:
        lowered = line.lower()
        for phrase in _ALL_PASS:
            assert phrase not in lowered, f"{name} DONE line demands {phrase!r}: {line[:120]}"
        assert "blocked with a reason" in lowered, f"{name} DONE line has no blocked rule: {line[:120]}"
