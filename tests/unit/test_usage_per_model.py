"""Per-model usage caps: parser + binding selection + API surfacing.

Anthropic and Codex report per-model weekly caps in ``cached_usage_raw`` — the
account page surfaces them as an inline "binding" bar plus a full per-model list
in the details expander. These tests use realistic payloads (mirrors of live
`~/.claude/jacked.db` rows and the recorded Codex app-server result) to lock the
extraction, the binding-selection rule, and the AccountResponse surfacing.
"""

import json

import pytest

from jacked.api.routes.auth import (
    _account_to_response,
    _build_account_usage,
    _parse_usage_details,
)
from jacked.service.menubar_summary import (
    binding_model,
    binding_model_compact,
    parse_per_model,
)

# --- Realistic payload fixtures -------------------------------------------

# Claude: Fable is the flagged-active binding constraint (account 13/8 shape).
CLAUDE_FABLE_ACTIVE = {
    "five_hour": {"utilization": 0.0, "resets_at": "2026-07-03T18:00:00+00:00"},
    "seven_day": {"utilization": 52.0, "resets_at": "2026-07-06T05:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 0,
         "severity": "normal", "is_active": False, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 52,
         "severity": "normal", "is_active": False, "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 90,
         "severity": "critical", "resets_at": "2026-07-06T05:00:00+00:00",
         "is_active": True,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}

# Claude: Fable is high (92%) but the 5h session (96%) is the instantaneous
# active limit, so no scoped model is flagged is_active (account 9 shape).
CLAUDE_FABLE_HIGH_INACTIVE = {
    "five_hour": {"utilization": 96.0},
    "seven_day": {"utilization": 68.0},
    "limits": [
        {"kind": "session", "percent": 96, "severity": "critical",
         "is_active": True, "scope": None},
        {"kind": "weekly_all", "percent": 68, "severity": "normal",
         "is_active": False, "scope": None},
        {"kind": "weekly_scoped", "percent": 92, "severity": "critical",
         "resets_at": "2026-07-03T22:59:59+00:00", "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
}

# Claude: the active limit is the overall weekly window; the only scoped model
# is idle (account 4 shape) → nothing to surface inline, but Sonnet still
# appears in the per-model list.
CLAUDE_NO_BINDING = {
    "five_hour": {"utilization": 0.0},
    "seven_day": {"utilization": 3.0},
    "limits": [
        {"kind": "weekly_all", "percent": 3, "severity": "normal",
         "is_active": True, "scope": None},
        {"kind": "weekly_scoped", "percent": 0, "severity": "normal",
         "resets_at": None, "is_active": False,
         "scope": {"model": {"display_name": "Sonnet"}}},
    ],
}

# Codex: the app-server result — the named limit carries a limitName (Spark);
# the bare "codex" overall entry is skipped (already the 5h/7d bars).
CODEX_SPARK = {
    "rateLimits": {"primary": {"usedPercent": 2}, "secondary": {"usedPercent": 26}},
    "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 2}, "planType": "pro"},
        "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark",
                            "primary": {"usedPercent": 7}},
    },
}

# Legacy cached payload (pre-`limits`): only the seven_day_* keys.
LEGACY_SEVEN_DAY = {
    "five_hour": {"utilization": 10.0},
    "seven_day": {"utilization": 20.0},
    "seven_day_sonnet": {"utilization": 42.5, "resets_at": "2026-02-08T00:00:00Z"},
    "seven_day_opus": None,
}


# --- parse_per_model -------------------------------------------------------

def test_parse_extracts_claude_scoped_model():
    pm = parse_per_model(CLAUDE_FABLE_ACTIVE)
    assert [(m["label"], m["utilization"], m["severity"], m["is_active"]) for m in pm] == [
        ("Fable", 90, "critical", True)
    ]


def test_parse_extracts_codex_named_limit_and_skips_overall():
    pm = parse_per_model(CODEX_SPARK)
    # Only the named Spark limit — the bare "codex" overall entry is skipped.
    assert [(m["label"], m["utilization"]) for m in pm] == [("GPT-5.3-Codex-Spark", 7)]


def test_parse_legacy_seven_day_fallback():
    pm = parse_per_model(LEGACY_SEVEN_DAY)
    assert len(pm) == 1
    assert pm[0]["key"] == "sonnet"
    assert pm[0]["label"] == "Sonnet"
    assert pm[0]["utilization"] == 42.5


def test_parse_sorts_by_utilization_descending():
    raw = {"limits": [
        {"kind": "weekly_scoped", "percent": 10, "is_active": False,
         "scope": {"model": {"display_name": "Opus"}}},
        {"kind": "weekly_scoped", "percent": 88, "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}
    labels = [m["label"] for m in parse_per_model(raw)]
    assert labels == ["Fable", "Opus"]


def test_parse_handles_empty_and_junk():
    assert parse_per_model(None) == []
    assert parse_per_model({}) == []
    assert parse_per_model({"limits": "not a list"}) == []


# --- binding_model ---------------------------------------------------------

def test_binding_prefers_flagged_active():
    bm = binding_model(parse_per_model(CLAUDE_FABLE_ACTIVE))
    assert bm is not None and bm["label"] == "Fable" and bm["is_active"] is True


def test_binding_surfaces_high_model_even_when_not_active():
    # Fable at 92% is worth surfacing even though the 5h session is the
    # instantaneous active limit.
    bm = binding_model(parse_per_model(CLAUDE_FABLE_HIGH_INACTIVE))
    assert bm is not None and bm["label"] == "Fable" and bm["utilization"] == 92


def test_binding_surfaces_only_scoped_model_even_when_idle():
    # The account's only scoped model (Sonnet 0%, not the active window) still
    # surfaces — users want to see per-model usage at any level.
    bm = binding_model(parse_per_model(CLAUDE_NO_BINDING))
    assert bm is not None and bm["label"] == "Sonnet" and bm["utilization"] == 0


@pytest.mark.parametrize("pct", [0, 1, 38, 70, 71, 100])
def test_binding_surfaces_scoped_model_at_any_level(pct):
    # No utilization threshold: a reported model surfaces regardless of level.
    pm = parse_per_model({"limits": [
        {"kind": "weekly_scoped", "percent": pct, "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}}]})
    assert binding_model(pm) is not None


def test_binding_surfaces_low_codex_model():
    # Spark at 7% surfaces (it's the account's per-model cap), not hidden.
    bm = binding_model(parse_per_model(CODEX_SPARK))
    assert bm is not None and bm["label"] == "GPT-5.3-Codex-Spark"


def test_binding_none_only_without_any_model():
    # None strictly when the payload reports no per-model cap at all.
    assert binding_model(parse_per_model({"five_hour": {"utilization": 5}})) is None
    assert binding_model([]) is None


def test_binding_compact_shape():
    assert binding_model_compact(CLAUDE_FABLE_ACTIVE) == {
        "label": "Fable",
        "utilization": 90,
        "resets_at": "2026-07-06T05:00:00+00:00",
        "severity": "critical",
    }
    # An idle-but-reported model still yields a compact payload (not None).
    assert binding_model_compact(CLAUDE_NO_BINDING)["label"] == "Sonnet"


# --- API surfacing ---------------------------------------------------------

def _row(raw: dict) -> dict:
    """Minimal account row carrying a raw usage payload."""
    return {
        "id": 1,
        "email": "u@x.com",
        "cached_usage_5h": raw.get("five_hour", {}).get("utilization", 0),
        "cached_usage_7d": raw.get("seven_day", {}).get("utilization", 0),
        "cached_usage_raw": json.dumps(raw),
    }


def test_build_account_usage_surfaces_binding_and_per_model():
    u = _build_account_usage(_row(CLAUDE_FABLE_HIGH_INACTIVE))
    assert u is not None
    assert u.binding_model is not None
    assert u.binding_model.label == "Fable"
    assert u.binding_model.utilization == 92
    assert "fable" in u.per_model


def test_build_account_usage_surfaces_idle_model():
    # Even an idle scoped model (Sonnet 0%) surfaces as the inline binding bar
    # and is listed in per_model.
    u = _build_account_usage(_row(CLAUDE_NO_BINDING))
    assert u.binding_model is not None and u.binding_model.label == "Sonnet"
    assert "sonnet" in u.per_model


def test_build_account_usage_none_without_usage():
    assert _build_account_usage({"id": 1, "email": "u@x.com"}) is None


def test_parse_usage_details_returns_binding_key():
    pm, binding_key, extra = _parse_usage_details(json.dumps(CLAUDE_FABLE_ACTIVE))
    assert binding_key == "fable"
    assert pm["fable"].is_active is True
    assert extra is None


# --- Robustness against malformed provider payloads -----------------------

def test_parse_coerces_string_percents():
    # A provider payload with a string percent must not crash the sort/compare;
    # it coerces to a number and still yields a binding.
    raw = {"limits": [
        {"kind": "weekly_scoped", "percent": "92", "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}
    pm = parse_per_model(raw)
    assert pm[0]["utilization"] == 92.0
    assert binding_model(pm)["label"] == "Fable"  # coerced 92 surfaces


def test_parse_mixed_percent_types_sorts_without_typeerror():
    # int vs string percents in the same payload previously raised TypeError in
    # sorted(); coercion makes the ordering numeric and safe.
    raw = {"limits": [
        {"kind": "weekly_scoped", "percent": "50", "is_active": False,
         "scope": {"model": {"display_name": "Sonnet"}}},
        {"kind": "weekly_scoped", "percent": 90, "is_active": False,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}
    assert [m["label"] for m in parse_per_model(raw)] == ["Fable", "Sonnet"]


def test_parse_legacy_scalar_seven_day():
    # Older cached payloads stored a bare number under seven_day_<model>.
    pm = parse_per_model({"seven_day_opus": 33})
    assert pm == [{"key": "opus", "label": "Opus", "utilization": 33,
                   "resets_at": None, "severity": None, "is_active": False}]


def test_parse_malformed_shapes_do_not_raise():
    # Junk in every branch must degrade to [], never throw.
    assert parse_per_model({"limits": "garbage"}) == []
    assert parse_per_model({"limits": ["x", 1, None]}) == []
    assert parse_per_model({"limits": [
        {"kind": "weekly_scoped", "scope": "not-a-dict"}]}) == []
    assert parse_per_model({"limits": [
        {"kind": "weekly_scoped", "scope": {"model": {"display_name": ""}}}]}) == []
    assert parse_per_model({"rateLimitsByLimitId": {"x": "not-a-dict"}}) == []


def test_build_account_usage_survives_malformed_raw():
    # A single corrupt cached_usage_raw must not 500 the accounts endpoint —
    # it degrades to no per-model data while 5h/7d still render.
    row = {
        "id": 1, "email": "u@x.com",
        "cached_usage_5h": 20.0, "cached_usage_7d": 40.0,
        "cached_usage_raw": '{"limits": [{"kind": "weekly_scoped", '
                            '"percent": {"nested": "junk"}, '
                            '"scope": {"model": {"display_name": "Fable"}}}]}',
    }
    u = _build_account_usage(row)  # must not raise
    assert u is not None
    assert u.five_hour == 20.0  # windows still surface


def test_account_response_includes_binding_model_for_ws_payload():
    # The bulk-refresh WS path sends _account_to_response(...).model_dump();
    # the inline bar depends on usage.binding_model being present there.
    row = _row(CLAUDE_FABLE_ACTIVE)
    row.update({"expires_at": 9999999999, "provider": "claude"})
    dumped = _account_to_response(row).model_dump()
    assert dumped["usage"]["binding_model"]["label"] == "Fable"
    assert dumped["usage"]["per_model"]["fable"]["utilization"] == 90
