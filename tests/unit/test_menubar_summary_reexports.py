"""The names that moved out of ``menubar_summary`` still import from it.

The per-model limit code moved to two modules (#227i):
:mod:`jacked.service.per_model` merges the limits and selects the one to
show, and :mod:`jacked.service.per_model_sources` holds the parser for each
payload shape and their helpers. The provider usage-block verdict moved to
:mod:`jacked.service.usage_block`. Callers and tests import these names
from :mod:`jacked.service.menubar_summary`, which re-exports them. These
tests prove three things. Each re-exported name is the same object as the
name in its new module. Each moved function has its definition in its new
module. The pill reads ``binding_model_compact`` and ``usage_block_of_row``
from the ``menubar_summary`` globals, so a patch on ``menubar_summary``
still changes what the pill does.
"""

from __future__ import annotations

import importlib
import types

import pytest

from jacked.service import menubar_summary

PER_MODEL = "jacked.service.per_model"
PER_MODEL_SOURCES = "jacked.service.per_model_sources"
USAGE_BLOCK = "jacked.service.usage_block"

# (new module, name) for each name that moved out of menubar_summary.
MOVED_CONSTANTS = [
    (PER_MODEL_SOURCES, "_LEGACY_MODEL_LABELS"),
    (USAGE_BLOCK, "USAGE_STALE_SECS"),
]
MOVED_FUNCTIONS = [
    (PER_MODEL_SOURCES, "_epoch_to_iso"),
    (PER_MODEL_SOURCES, "_log_skipped_limit"),
    (PER_MODEL_SOURCES, "_claude_scoped_models"),
    (PER_MODEL_SOURCES, "_codex_named_models"),
    (PER_MODEL_SOURCES, "_legacy_seven_day_models"),
    (PER_MODEL, "parse_per_model"),
    (PER_MODEL, "binding_model"),
    (PER_MODEL, "binding_model_compact"),
    (USAGE_BLOCK, "usage_block"),
    (USAGE_BLOCK, "usage_block_of_row"),
]
MOVED = MOVED_CONSTANTS + MOVED_FUNCTIONS


def _ids(pairs):
    return [name for _, name in pairs]


@pytest.mark.parametrize(("home", "name"), MOVED, ids=_ids(MOVED))
def test_each_moved_name_imports_from_menubar_summary_as_the_same_object(home, name):
    new_module = importlib.import_module(home)
    assert getattr(menubar_summary, name) is getattr(new_module, name)


@pytest.mark.parametrize(("home", "name"), MOVED_FUNCTIONS, ids=_ids(MOVED_FUNCTIONS))
def test_each_moved_function_has_its_definition_in_its_new_module(home, name):
    assert getattr(importlib.import_module(home), name).__module__ == home


def test_the_pill_reads_binding_model_compact_from_the_menubar_summary_globals(monkeypatch):
    calls = []

    def _sentinel(raw, plan=None):
        calls.append((raw, plan))
        return {"label": "Sentinel", "utilization": 12.34}

    monkeypatch.setattr(menubar_summary, "binding_model_compact", _sentinel)
    raw = {"limits": []}
    plan = object()
    with_plan = menubar_summary._model_cap({"id": 1}, types.SimpleNamespace(plan=plan), raw)
    without_view = menubar_summary._model_cap({"id": 1}, None, raw)
    assert with_plan == without_view == {"label": "Sentinel", "utilization": 12.3}
    assert calls == [(raw, plan), (raw, None)]


def test_the_pill_reads_usage_block_of_row_from_the_menubar_summary_globals(monkeypatch):
    rows = []

    def _blocked(row, now=None):
        rows.append(row["id"])
        return {"reason": "blocked", "resets_at": None, "read_at": None}

    monkeypatch.setattr(menubar_summary, "usage_block_of_row", _blocked)
    summary = menubar_summary.summarize_account(
        {"id": 7, "cached_usage_5h": 10, "cached_usage_7d": 20}
    )
    assert (summary["blocked"], summary["color"]) == (True, "red")
    assert rows == [7]
