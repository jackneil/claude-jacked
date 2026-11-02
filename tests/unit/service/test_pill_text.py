"""Pill text (macOS menu bar) and tray text (Windows/Linux) for every plan.

Covers jacked.service.pill_text on summaries from
jacked.service.menubar_summary.summarize_account: the plan-aware slots, the
model limit, rounding, the tooltip limit, and summaries that cannot be
trusted (a malformed payload or a response from the network).
"""

import json
import time

import pytest

from jacked.service.menubar_summary import (
    compute_active_account_summary,
    compute_worst_account_summary,
    summarize_account,
)
from jacked.service.pill_text import (
    TOOLTIP_MAX_UNITS,
    menubar_title,
    tray_tooltip,
    usage_line,
)


@pytest.fixture
def _fresh_plan_state(monkeypatch):
    from jacked import usage_plans
    from jacked.service import menubar_summary

    monkeypatch.setattr(usage_plans, "_catalog_cache", None)
    monkeypatch.setattr(usage_plans, "_drift_logged", set())
    monkeypatch.setattr(menubar_summary, "_VIEW_FAILURES_LOGGED", set())
    monkeypatch.setattr(menubar_summary, "_CAP_FAILURES_LOGGED", set())


def _ultra(**extra):
    raw = json.dumps({"five_hour": None, "limits": [
        {"kind": "weekly_all", "percent": 33},
        {"kind": "weekly_scoped", "percent": 61, "is_active": True,
         "scope": {"model": {"display_name": "Fable"}}}]})
    row = {"id": 3, "email": "u@x.com", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_20x",
           "cached_usage_5h": 67.0,  # stale, from before verification
           "cached_usage_7d": 33.0, "cached_usage_raw": raw}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Pill text and tooltip for every plan in the shipped catalog
# ---------------------------------------------------------------------------


def _catalog_variants():
    """(provider, subscription, tier) for every plan the shipped catalog names,
    plus each provider's default (an unknown plan). A plan added to the catalog
    joins this walk without a test edit."""
    from jacked import usage_plans

    out = []
    for provider, pcat in usage_plans.catalog().providers.items():
        out.append((provider, "", ""))
        for sub, entry in pcat.subscriptions.items():
            out.append((provider, sub, ""))
            out.extend((provider, sub, tier) for tier in entry.tiers)
    return out


def _variant_row(provider, sub, tier, session_reported):
    """A row for one catalog plan: 5h 12% (when the payload reports a session
    window), 7d 34%, and for Claude a weekly Fable limit at 56%."""
    if provider == "claude":
        limits = [{"kind": "weekly_all", "percent": 34},
                  {"kind": "weekly_scoped", "percent": 56, "is_active": True,
                   "scope": {"model": {"display_name": "Fable"}}}]
        if session_reported:
            limits.insert(0, {"kind": "session", "percent": 12})
        raw = {"five_hour": {"utilization": 12.0} if session_reported else None,
               "limits": limits}
    else:
        week = {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 4102444800}
        five = {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 4102444800}
        raw = {"ordinaryUsageAllowed": True,
               "rateLimits": ({"primary": five, "secondary": week} if session_reported
                              else {"primary": week, "secondary": None})}
    return {"id": 1, "provider": provider, "subscription_type": sub or None,
            "rate_limit_tier": tier or None,
            "cached_usage_5h": 12.0 if session_reported else None,
            "cached_usage_7d": 34.0, "cached_usage_raw": json.dumps(raw)}


_VARIANTS = [
    pytest.param(provider, sub, tier, reported,
                 id=f"{provider}-{sub or 'default'}-{tier or 'any'}-{'5h' if reported else 'no5h'}")
    for provider, sub, tier in _catalog_variants()
    for reported in (True, False)
]


@pytest.mark.usefixtures("_fresh_plan_state")
class TestPillForEveryCatalogPlan:
    """Every plan and provider jacked knows renders a pill with no dead slot."""

    @pytest.mark.parametrize("provider,sub,tier,reported", _VARIANTS)
    def test_pill_shows_only_the_limits_the_account_has(self, provider, sub, tier, reported):
        s = summarize_account(_variant_row(provider, sub, tier, reported))
        title = menubar_title(s)
        slots = title.split("·")
        assert "-" not in slots, title  # never a dead slot
        if s["five_hour"] is None:
            # The weekly number carries its tag, so it cannot read as 5h.
            assert slots[0] == "7d 34%", title
        else:
            assert slots[:2] == [f"{round(s['five_hour'])}%", "34%"], title
        if provider == "claude":
            assert slots[-1] == "F56%", title
            assert s["max_pct"] == 56.0 and s["color"] == "green"
        else:
            assert s["model_cap"] is None
        tip = tray_tooltip(s)
        assert ("5h " in tip) is (s["five_hour"] is not None), tip
        assert "7d 34%" in tip
        assert ("Fable 56%" in tip) is (provider == "claude"), tip

    def test_the_walk_covers_both_shapes_of_the_pill(self):
        # Guard against a catalog change that leaves only one branch tested.
        shapes = set()
        for provider, sub, tier in _catalog_variants():
            for reported in (True, False):
                s = summarize_account(_variant_row(provider, sub, tier, reported))
                shapes.add((provider, s["five_hour"] is None))
        assert shapes == {("claude", True), ("claude", False),
                          ("codex", True), ("codex", False)}


@pytest.mark.usefixtures("_fresh_plan_state")
class TestModelLimitInThePill:
    def _ultra13(self):
        # Live shape (2026-11-02, acct 13): weekly 74%, weekly Fable 100%.
        raw = json.dumps({"five_hour": None, "limits": [
            {"kind": "weekly_all", "percent": 74},
            {"kind": "weekly_scoped", "percent": 100, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}}]})
        return _ultra(cached_usage_7d=74.0, cached_usage_raw=raw)

    def test_a_capped_fable_limit_turns_the_pill_red(self):
        s = summarize_account(self._ultra13())
        assert menubar_title(s) == "7d 74%·F100%"
        assert (s["max_pct"], s["color"]) == (100.0, "red")
        assert tray_tooltip(s) == "Jacked · Claude · 7d 74% · Fable 100%"

    def test_max_shows_all_three_limits(self):
        raw = json.dumps({"limits": [
            {"kind": "session", "percent": 5}, {"kind": "weekly_all", "percent": 11},
            {"kind": "weekly_scoped", "percent": 18, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}}]})
        s = summarize_account({"id": 5, "subscription_type": "max",
                               "rate_limit_tier": "default_claude_max_5x",
                               "cached_usage_5h": 5.0, "cached_usage_7d": 11.0,
                               "cached_usage_raw": raw})
        assert menubar_title(s) == "5%·11%·F18%"
        assert s["max_pct"] == 18.0

    def test_the_pill_shows_the_limit_the_dashboard_shows(self):
        # The provider marks Fable active: the pill shows Fable (as the
        # dashboard does), and the tint follows the shown limit only.
        raw = json.dumps({"five_hour": None, "limits": [
            {"kind": "weekly_all", "percent": 20},
            {"kind": "weekly_scoped", "percent": 40, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "weekly_scoped", "percent": 88, "is_active": False,
             "scope": {"model": {"display_name": "Sonnet"}}}]})
        s = summarize_account(_ultra(cached_usage_7d=20.0, cached_usage_raw=raw))
        assert menubar_title(s) == "7d 20%·F40%"
        assert s["color"] == "green"

    def test_a_codex_model_limit_is_tagged_by_its_last_word(self):
        raw = json.dumps({"ordinaryUsageAllowed": True,
                          "rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 300},
                                         "secondary": {"usedPercent": 40,
                                                       "windowDurationMins": 10080}},
                          "rateLimitsByLimitId": {
                              "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark",
                                                  "secondary": {"usedPercent": 91}}}})
        s = summarize_account({"id": 14, "provider": "codex", "subscription_type": "plus",
                               "cached_usage_5h": 5.0, "cached_usage_7d": 40.0,
                               "cached_usage_raw": raw})
        assert menubar_title(s) == "5%·40%·S91%"
        assert s["color"] == "red"
        assert tray_tooltip(s) == "Jacked · Codex · 5h 5% · 7d 40% · GPT-5.3-Codex-Spark 91%"

    def test_an_unreadable_payload_has_no_model_limit(self):
        s = summarize_account({"id": 1, "cached_usage_5h": 10, "cached_usage_7d": 20,
                               "cached_usage_raw": "not json"})
        assert s["model_cap"] is None
        assert menubar_title(s) == "10%·20%"

    def test_a_model_limit_failure_does_not_stop_the_pill(self, monkeypatch):
        from jacked.service import menubar_summary

        def _boom(*args, **kwargs):
            raise RuntimeError("bad payload")

        monkeypatch.setattr(menubar_summary, "binding_model_compact", _boom)
        s = summarize_account(self._ultra13())
        assert s["model_cap"] is None
        assert menubar_title(s) == "7d 74%"


def test_pill_rounds_half_up_like_the_dashboard():
    # usage.js uses Math.round: 40.5 shows 41%. Python's round() gives 40.
    assert menubar_title({"five_hour": 40.5, "seven_day": 2.5}) == "41%·3%"


class TestTrayTooltip:
    def test_no_data_and_unknown_provider(self):
        assert tray_tooltip(None) == "Jacked"
        assert tray_tooltip({"provider": "grok", "seven_day": 9.0}) == "Jacked · Grok · 7d 9%"

    def test_the_longest_real_tooltip_is_cut_to_the_windows_limit(self):
        # Names are cleaned to 40 characters, so the longest real tooltip is a
        # 40-character provider, a 40-character model name, both windows at
        # 100% and a block: 133 units, cut to 127.
        cap = {"label": "X" * 300, "utilization": 100.0}
        tip = tray_tooltip({"provider": "y" * 300, "five_hour": 100.0, "seven_day": 100.0,
                            "model_cap": cap, "blocked": True})
        assert len(tip.encode("utf-16-le")) // 2 == TOOLTIP_MAX_UNITS
        assert tip.endswith("…") and tip.startswith("Jacked · " + "Y" + "y" * 39 + " · 5h 100%")

    def test_the_limit_counts_utf16_units_not_characters(self):
        # An emoji is two UTF-16 units: a cut by characters would overflow.
        cap = {"label": "\U0001F600" * 100, "utilization": 50.0}
        tip = tray_tooltip({"provider": "claude", "seven_day": 2.0, "model_cap": cap})
        assert len(tip.encode("utf-16-le")) // 2 <= TOOLTIP_MAX_UNITS


# ---------------------------------------------------------------------------
# Values that are not finite numbers are no reading (never 0%, never a 500)
# ---------------------------------------------------------------------------


def _fable_row(percent_json: str, **extra):
    raw = ('{"five_hour": null, "limits": [{"kind": "weekly_all", "percent": 20}, '
           '{"kind": "weekly_scoped", "percent": ' + percent_json + ', "is_active": true, '
           '"scope": {"model": {"display_name": "Fable"}}}]}')
    return _ultra(id=9, cached_usage_7d=20.0, cached_usage_raw=raw, **extra)


@pytest.mark.usefixtures("_fresh_plan_state")
class TestUnreadableNumbers:
    @pytest.mark.parametrize("percent", ['"Infinity"', "1e999", '"nan"', "NaN", "1" + "0" * 400])
    def test_a_bad_model_percent_drops_only_the_model_limit(self, percent):
        bad = _fable_row(percent)
        good = _ultra(id=4)
        s = summarize_account(bad)
        assert s["model_cap"] is None
        assert (s["seven_day"], s["max_pct"]) == (20.0, 20.0)
        assert menubar_title(s) == "7d 20%"
        # The menubar-summary endpoint answers with allow_nan=False.
        json.dumps({"active": compute_active_account_summary([good, bad], 4),
                    "worst": compute_worst_account_summary([good, bad])}, allow_nan=False)

    def test_a_bad_model_percent_warns_once_without_the_value(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="jacked.service.menubar_summary"):
            for _ in range(3):
                summarize_account(_fable_row('"Infinity"'))
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "account 9" in warnings[0] and "inf" not in warnings[0].lower()

    @pytest.mark.parametrize("value", [float("inf"), float("nan"), "junk"])
    def test_a_bad_cached_window_value_is_no_reading(self, value):
        s = summarize_account({"id": 1, "cached_usage_5h": value, "cached_usage_7d": 30})
        assert s["five_hour"] is None and s["seven_day"] == 30.0
        assert menubar_title(s) == "7d 30%"
        json.dumps(s, allow_nan=False)


# ---------------------------------------------------------------------------
# A summary from the network is untrusted (the tray reads it over HTTP)
# ---------------------------------------------------------------------------


class TestUntrustedSummary:
    @pytest.mark.parametrize("summary", [
        ["x"], "x", {"five_hour": "12", "seven_day": None},
        {"seven_day": 10, "model_cap": {"label": "Fable"}},
        {"seven_day": 10, "model_cap": {"label": "Fable", "utilization": "50"}},
        {"seven_day": float("nan")}, {"seven_day": True},
    ])
    def test_a_malformed_summary_never_raises(self, summary):
        assert isinstance(menubar_title(summary), str)
        assert isinstance(tray_tooltip(summary), str)
        usage_line(summary)

    def test_the_tag_comes_from_the_name_not_the_wire(self):
        cap = {"label": "Fable", "tag": "ANY TEXT FROM THE WIRE ", "utilization": 5.0}
        assert menubar_title({"five_hour": 1.0, "seven_day": 2.0, "model_cap": cap}) == "1%·2%·F5%"

    def test_names_from_the_wire_are_cleaned(self):
        cap = {"label": "Fa‮ble\n", "utilization": 5.0}
        tip = tray_tooltip({"provider": "x\ny", "seven_day": 2.0, "model_cap": cap})
        assert tip == "Jacked · X Y · 7d 2% · Fa ble 5%"
        assert all(ch.isprintable() for ch in tip)

    def test_a_huge_label_is_cut_in_linear_time(self):
        cap = {"label": "X" * 1_000_000, "utilization": 50.0}
        started = time.perf_counter()
        tip = tray_tooltip({"provider": "claude", "seven_day": 2.0, "model_cap": cap})
        assert time.perf_counter() - started < 0.5
        assert len(tip.encode("utf-16-le")) // 2 <= TOOLTIP_MAX_UNITS


def test_the_cut_never_splits_a_two_unit_character():
    from jacked.service.pill_text import fit_tooltip

    # 125 units of text, then an emoji (2 units) across the 126-unit cut point.
    text = "a" * 125 + "\U0001F600" * 3
    cut = fit_tooltip(text)
    assert cut == "a" * 125 + "…"
    assert len(cut.encode("utf-16-le")) // 2 <= TOOLTIP_MAX_UNITS
