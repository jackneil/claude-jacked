"""Per-model usage caps: parser + binding selection + API surfacing.

Anthropic and Codex report per-model weekly caps in ``cached_usage_raw`` — the
account page surfaces them as an inline "binding" bar plus a full per-model list
in the details expander. These tests use realistic payloads (mirrors of live
`~/.claude/jacked.db` rows and the recorded Codex app-server result) to lock the
extraction, the binding-selection rule, and the AccountResponse surfacing.
"""

import json
from datetime import datetime

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
        "is_active": True,
        # The weekly length lets the dashboard draw the week-elapsed marker.
        "window_seconds": 7 * 86400,
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


def test_parse_usage_details_carries_window_seconds():
    # The dashboard draws the week-elapsed marker from window_seconds, so
    # every per-model entry carries the length of its limit's window.
    pm, _, _ = _parse_usage_details(json.dumps(CLAUDE_FABLE_ACTIVE))
    assert pm["fable"].window_seconds == 7 * 86400
    pm, _, _ = _parse_usage_details(json.dumps(CODEX_SPARK))
    # Spark has no weekly window here: the primary slot, 5 hours.
    assert pm["codex_bengalfox"].window_seconds == 5 * 3600
    pm, _, _ = _parse_usage_details(json.dumps(LEGACY_SEVEN_DAY))
    assert pm["sonnet"].window_seconds == 7 * 86400


def test_parse_usage_details_passes_the_plan_through(monkeypatch):
    from jacked.service import menubar_summary

    seen = []
    real = menubar_summary.parse_per_model

    def _spy(raw, plan=None):
        seen.append(plan)
        return real(raw, plan)

    monkeypatch.setattr(menubar_summary, "parse_per_model", _spy)
    marker = object()
    _parse_usage_details(json.dumps(CLAUDE_FABLE_ACTIVE), marker)
    _parse_usage_details(json.dumps(CLAUDE_FABLE_ACTIVE))
    assert seen == [marker, None]


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
                   "resets_at": None, "severity": None, "is_active": False,
                   "window_seconds": 7 * 86400}]


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
    # it degrades to no per-model data while 5h/7d still render. The limits
    # list names the session limit, as a real payload with a 5h window does
    # (a list without one says the account has no 5h window).
    row = {
        "id": 1, "email": "u@x.com",
        "cached_usage_5h": 20.0, "cached_usage_7d": 40.0,
        "cached_usage_raw": '{"limits": [{"kind": "session", "percent": 20}, '
                            '{"kind": "weekly_scoped", '
                            '"percent": {"nested": "junk"}, '
                            '"scope": {"model": {"display_name": "Fable"}}}]}',
    }
    u = _build_account_usage(row)  # must not raise
    assert u is not None
    assert u.five_hour == 20.0  # windows still surface
    assert u.seven_day == 40.0


def test_account_response_includes_binding_model_for_ws_payload():
    # The bulk-refresh WS path sends _account_to_response(...).model_dump();
    # the inline bar depends on usage.binding_model being present there.
    row = _row(CLAUDE_FABLE_ACTIVE)
    row.update({"expires_at": 9999999999, "provider": "claude"})
    dumped = _account_to_response(row).model_dump()
    assert dumped["usage"]["binding_model"]["label"] == "Fable"
    assert dumped["usage"]["per_model"]["fable"]["utilization"] == 90
    assert dumped["usage"]["binding_model"]["window_seconds"] == 7 * 86400
    assert dumped["usage"]["per_model"]["fable"]["window_seconds"] == 7 * 86400


# --- Values that are not finite numbers (#231i) ----------------------------
#
# A provider value that is not a finite number is no reading (never 0%), and
# it must never reach a surface: a browser refuses a WebSocket frame that
# holds a bare NaN or Infinity token and drops the whole message. Each source
# gets one entry with the value under test beside one good entry.

INF_FROM_JSON = json.loads("1e999")  # Python reads 1e999 as infinity
GOOD_RESET = "2026-11-08T00:00:00+00:00"

BAD_PERCENTS = [
    pytest.param(INF_FROM_JSON, id="1e999"),
    pytest.param(float("nan"), id="nan"),
    pytest.param("Infinity", id="Infinity-string"),
    pytest.param(float("-inf"), id="-inf"),
]

BAD_RESETS = [
    pytest.param(INF_FROM_JSON, id="1e999"),
    pytest.param(float("nan"), id="nan"),
    pytest.param("junk", id="junk"),
    pytest.param([1], id="list"),
    pytest.param(True, id="bool"),
]


def _claude_payload(percent=50, resets_at=GOOD_RESET):
    """Fable carries the value under test; Opus is the good entry."""
    return {"limits": [
        {"kind": "weekly_scoped", "percent": percent, "resets_at": resets_at,
         "is_active": True, "scope": {"model": {"display_name": "Fable"}}},
        {"kind": "weekly_scoped", "percent": 30, "resets_at": GOOD_RESET,
         "is_active": False, "scope": {"model": {"display_name": "Opus"}}},
    ]}


def _codex_payload(percent=50, resets_at=1793750400):
    """Spark carries the value under test; Nova is the good entry."""
    return {"rateLimitsByLimitId": {
        "codex_spark": {"limitName": "Spark",
                        "secondary": {"usedPercent": percent, "resetsAt": resets_at}},
        "codex_nova": {"limitName": "Nova",
                       "secondary": {"usedPercent": 30, "resetsAt": 1793750400}},
    }}


def _legacy_dict_payload(percent=50, resets_at=GOOD_RESET):
    """seven_day_opus carries the value under test; Sonnet is the good entry."""
    return {
        "seven_day_opus": {"utilization": percent, "resets_at": resets_at},
        "seven_day_sonnet": {"utilization": 30, "resets_at": GOOD_RESET},
    }


def _legacy_bare_payload(percent=50):
    """The bare numeric form: no reset time at all."""
    return {"seven_day_opus": percent, "seven_day_sonnet": 30}


PERCENT_SOURCES = [
    pytest.param(_claude_payload, "fable", "opus", id="claude-limits"),
    pytest.param(_codex_payload, "codex_spark", "codex_nova", id="codex-named"),
    pytest.param(_legacy_dict_payload, "opus", "sonnet", id="legacy-dict"),
    pytest.param(_legacy_bare_payload, "opus", "sonnet", id="legacy-bare"),
]

RESET_SOURCES = [
    pytest.param(_claude_payload, "fable", id="claude-limits"),
    pytest.param(_codex_payload, "codex_spark", id="codex-named"),
    pytest.param(_legacy_dict_payload, "opus", id="legacy-dict"),
]


def _assert_json_safe(models):
    """Strict JSON (no NaN or Infinity token), and every reset is ISO or None."""
    json.dumps(models, allow_nan=False)
    for m in models:
        reset = m["resets_at"]
        assert reset is None or datetime.fromisoformat(reset)


@pytest.fixture
def _fresh_skip_log(monkeypatch):
    from jacked.service import per_model_sources

    monkeypatch.setattr(per_model_sources, "_SKIPPED_LIMITS_LOGGED", set())


@pytest.mark.parametrize("percent", BAD_PERCENTS)
@pytest.mark.parametrize("build,bad_key,good_key", PERCENT_SOURCES)
def test_a_non_finite_percent_drops_only_its_entry(build, bad_key, good_key, percent):
    models = parse_per_model(build(percent=percent))
    by_key = {m["key"]: m for m in models}
    assert bad_key not in by_key
    assert by_key[good_key]["utilization"] == 30.0
    _assert_json_safe(models)


@pytest.mark.parametrize("resets_at", BAD_RESETS)
@pytest.mark.parametrize("build,bad_key", RESET_SOURCES)
def test_a_reset_that_is_not_a_timestamp_is_no_reset(build, bad_key, resets_at):
    models = parse_per_model(build(resets_at=resets_at))
    by_key = {m["key"]: m for m in models}
    assert by_key[bad_key]["resets_at"] is None
    assert by_key[bad_key]["utilization"] == 50.0
    _assert_json_safe(models)


@pytest.mark.parametrize("build,key", [
    pytest.param(_claude_payload, "fable", id="claude-limits"),
    pytest.param(_legacy_dict_payload, "opus", id="legacy-dict"),
])
def test_an_epoch_reset_is_stored_as_iso(build, key):
    # The same normalization as the usage cache: epoch seconds become ISO.
    by_key = {m["key"]: m for m in parse_per_model(build(resets_at=1774933200))}
    assert by_key[key]["resets_at"] == "2026-03-31T05:00:00+00:00"


def test_binding_compact_moves_to_the_next_finite_model():
    compact = binding_model_compact(_claude_payload(percent=INF_FROM_JSON))
    assert (compact["label"], compact["utilization"]) == ("Opus", 30.0)
    json.dumps(compact, allow_nan=False)
    only_bad = {"limits": _claude_payload(percent=float("nan"))["limits"][:1]}
    assert binding_model_compact(only_bad) is None


def test_a_severity_that_is_not_text_is_dropped():
    raw = _claude_payload()
    raw["limits"][0]["severity"] = INF_FROM_JSON
    by_key = {m["key"]: m for m in parse_per_model(raw)}
    assert by_key["fable"]["severity"] is None
    assert by_key["fable"]["utilization"] == 50.0
    _assert_json_safe(list(by_key.values()))


def test_codex_uses_the_session_window_when_the_weekly_one_has_no_finite_reading():
    raw = {"rateLimitsByLimitId": {"codex_spark": {
        "limitName": "Spark",
        "secondary": {"usedPercent": float("nan"), "windowDurationMins": 10080},
        "primary": {"usedPercent": 7, "windowDurationMins": 300},
    }}}
    [model] = parse_per_model(raw)
    assert (model["utilization"], model["window_seconds"]) == (7.0, 300 * 60)


@pytest.mark.parametrize("percent", [None, True, "92%", {"nested": 1}, 10**400])
def test_a_percent_that_is_not_a_number_is_no_reading(percent):
    # No reading is never 0%: the entry is skipped, not shown as 0%.
    assert [m["key"] for m in parse_per_model(_claude_payload(percent=percent))] == ["opus"]


@pytest.mark.usefixtures("_fresh_skip_log")
def test_a_skipped_limit_warns_once_and_never_logs_the_value(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="jacked.service.per_model_sources"):
        for _ in range(3):
            parse_per_model(_claude_payload(percent=INF_FROM_JSON))
    ours = [r for r in caplog.records if r.name == "jacked.service.per_model_sources"]
    warnings = [r.getMessage() for r in ours if r.levelno == logging.WARNING]
    debugs = [r.getMessage() for r in ours if r.levelno == logging.DEBUG]
    assert len(warnings) == 1 and "entry fable from limits" in warnings[0]
    assert len(debugs) == 2
    for text in warnings + debugs:
        assert "inf" not in text.lower() and "1e999" not in text
