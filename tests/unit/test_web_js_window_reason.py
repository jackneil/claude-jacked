"""The reset column says WHY a usage window has no future reset.

Evals utils.js + usage.js under node and asserts on windowReason() and the
rendered renderUsageBar() HTML: every keeper reason string, the muted
"last read" form for Codex and stale readings, the compact variant's short
caption with the full reason in its hover title, and no elapsed marker for a
lapsed window. Skipped when node is not on PATH.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
UTILS_JS = WEB_JS / "utils.js"
USAGE_JS = WEB_JS / "components" / "usage.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = r"""
const fs = require('fs');
global.window = { jackedState: { settings: {} } };
eval(fs.readFileSync(__UTILS__, 'utf8'));
// utils.js escapeHtml needs a DOM; the harness has none.
escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
eval(fs.readFileSync(__USAGE__, 'utf8'));
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
"""

def _now():
    """Read the clock at the call: a module-level timestamp goes stale over a
    long gate run, and a "+300s" retry deadline then reads as already due."""
    return int(time.time())
PAST = "2020-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"


def _run(tmp_path, snippet):
    program = (
        _HARNESS.replace("__UTILS__", json.dumps(str(UTILS_JS)))
        .replace("__USAGE__", json.dumps(str(USAGE_JS)))
        + "\n" + snippet
    )
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _reason(tmp_path, acct, resets_at=PAST, settings=None):
    """windowReason of the account's 5h window with this reset. The window
    carries a reading unless the account sets cached_usage_5h itself."""
    acct = dict(acct)
    acct.setdefault("cached_usage_5h", 40.0)
    acct["cached_5h_resets_at"] = resets_at
    return _run(
        tmp_path,
        f"window.jackedState.settings = {json.dumps(settings or {})};\n"
        f"out(windowReason({json.dumps(acct)}, 'five_hour'));",
    )


def _fresh(**kw):
    """An account whose usage was read a minute ago (not stale)."""
    acct = {"id": 1, "provider": "claude", "usage_cached_at": _now() - 60}
    acct.update(kw)
    return acct


class TestWindowReason:
    def test_open_window_has_no_reason(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "needs_reauth"})
        assert _reason(tmp_path, acct, FUTURE) is None

    def test_needs_reauth_uses_the_warning_tone(self, tmp_path):
        r = _reason(tmp_path, _fresh(keeper_state={"last_status": "needs_reauth"}))
        assert r == {"text": "needs re-auth", "short": "needs re-auth", "tone": "warn", "muted": False}

    def test_needs_reauth_beats_a_stale_reading(self, tmp_path):
        acct = _fresh(usage_cached_at=_now() - 86400, keeper_state={"last_status": "needs_reauth"})
        assert _reason(tmp_path, acct)["text"] == "needs re-auth"

    def test_paused_shows_the_servers_next_start_in_local_time(self, tmp_path):
        """The server attaches the epoch of the next active start; the
        client renders it in the viewer's zone (never a settings lookup)."""
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours", "paused_until": _now() + 3 * 3600})
        r = _reason(tmp_path, acct)
        assert r["text"].startswith("paused until ")
        assert "06:00" not in r["text"]
        assert r["short"] == "paused"

    def test_paused_ignores_a_retry_deadline(self, tmp_path):
        """next_attempt_at is a retry deadline, never the pause time."""
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours", "next_attempt_at": _now() + 600})
        assert _reason(tmp_path, acct)["text"] == "paused"

    def test_a_time_within_a_day_carries_no_date(self, tmp_path):
        """A pause 7 hours away crosses midnight; the column is sized for
        the time alone, so the date is added only beyond 24 hours."""
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours", "paused_until": _now() + 7 * 3600})
        text = _reason(tmp_path, acct)["text"]
        assert text.startswith("paused until ")
        assert len(text) <= 21, text
        far = _fresh(keeper_state={"last_status": "rate_limited", "next_attempt_at": _now() + 3 * 86400})
        assert len(_reason(tmp_path, far)["text"]) > 21

    def test_paused_without_a_time_is_just_paused(self, tmp_path):
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours"})
        assert _reason(tmp_path, acct)["text"] == "paused"

    @pytest.mark.parametrize(
        "skip, text, short, tone",
        [
            ("keeper_disabled", "keeper off", "keeper off", "muted"),
            ("no_token", "no token, re-auth", "no token", "warn"),
            ("auto_swap_disabled", "auto-swap off", "auto-swap off", "muted"),
        ],
    )
    def test_skip_reasons(self, tmp_path, skip, text, short, tone):
        r = _reason(tmp_path, _fresh(keeper_state={"skip_reason": skip}))
        assert r == {"text": text, "short": short, "tone": tone, "muted": False}

    def test_rate_limited_shows_the_retry_time(self, tmp_path):
        acct = _fresh(keeper_state={
            "last_status": "rate_limited", "skip_reason": "backoff",
            "next_attempt_at": _now() + 600,
        })
        r = _reason(tmp_path, acct)
        assert r["text"].startswith("limited until ")
        assert r["short"] == "rate limited"

    def test_past_next_attempt_is_not_a_promise(self, tmp_path):
        """A persisted schedule the machine slept through must not read as
        a future time."""
        acct = _fresh(keeper_state={"last_status": "rate_limited", "next_attempt_at": _now() - 600})
        assert _reason(tmp_path, acct)["text"] == "limit lifting"
        acct = _fresh(keeper_state={"last_status": "error", "next_attempt_at": _now() - 600})
        assert _reason(tmp_path, acct)["text"] == "retry due"

    def test_every_short_form_fits_the_compact_caption(self, tmp_path):
        states = [
            {"last_status": "needs_reauth"},
            {"skip_reason": "keeper_disabled"}, {"skip_reason": "no_token"},
            {"skip_reason": "outside_active_hours", "paused_until": _now() + 3600},
            {"skip_reason": "auto_swap_disabled"},
            {"last_status": "rate_limited", "next_attempt_at": _now() + 600},
            {"last_status": "rate_limited"},
            {"last_status": "error", "next_attempt_at": _now() + 600},
            {"last_status": "ok"},
            {"last_status": "ping_pending", "pending_windows": ["5h"]},
            {"last_status": "ping_pending", "pending_windows": ["5h"], "skip_reason": "confirming",
             "next_attempt_at": _now() + 600},
        ]
        for ks in states:
            r = _reason(tmp_path, _fresh(keeper_state=ks))
            assert r is not None and len(r["short"]) <= 14, (ks, r)

    @pytest.mark.parametrize(
        "ks",
        [
            # The ping was accepted and waits for a usage read (#238i).
            {"last_status": "ping_pending", "pending_windows": ["5h"]},
            # The sweep holds the account until the confirmation deadline.
            {"last_status": "ping_pending", "pending_windows": ["5h"],
             "skip_reason": "confirming", "next_attempt_at": "+600"},
        ],
    )
    def test_a_pending_ping_reads_as_window_opening(self, tmp_path, ks):
        ks = {k: (_now() + 600 if v == "+600" else v) for k, v in ks.items()}
        r = _reason(tmp_path, _fresh(keeper_state=ks))
        assert r == {"text": "window opening", "short": "opening", "tone": "muted", "muted": False}

    @pytest.mark.parametrize("reason_code", ["no_header", "read_not_landed"])
    def test_a_pending_ping_with_a_stale_reading_still_reads_as_opening(self, tmp_path, reason_code):
        """A read-back that did not land leaves the last reading old: the
        pending caption still wins over "last read", and the stale dim stays."""
        ks = {"last_status": "ping_pending", "pending_reason": reason_code,
              "pending_windows": ["5h"], "pending_until": _now() + 600}
        acct = _fresh(keeper_state=ks, usage_cached_at=_now() - 2 * 3600)
        r = _reason(tmp_path, acct)
        assert r == {"text": "window opening", "short": "opening", "tone": "muted", "muted": True}

    @pytest.mark.parametrize(
        "skip, extra, text, short",
        [
            ("keeper_disabled", {}, "keeper off", "keeper off"),
            ("outside_active_hours", {"paused_until": "+3h"}, "paused until ", "paused"),
            ("outside_active_hours", {}, "paused", "paused"),
        ],
    )
    def test_a_keeper_that_does_not_run_wins_over_the_pending_caption(
        self, tmp_path, skip, extra, text, short,
    ):
        """The sweep writes these two reasons and leaves last_status alone, so
        the ping stays pending. "window opening" would claim progress that no
        sweep is making, for as long as the keeper stays off."""
        ks = {"last_status": "ping_pending", "pending_windows": ["5h"], "skip_reason": skip}
        ks.update({k: _now() + 3 * 3600 for k in extra})
        r = _reason(tmp_path, _fresh(keeper_state=ks))
        assert r["text"].startswith(text)
        assert r["short"] == short

    @pytest.mark.parametrize("skip", ["keeper_disabled", "outside_active_hours"])
    def test_a_stopped_keeper_with_a_stale_reading_never_reads_as_opening(self, tmp_path, skip):
        ks = {"last_status": "ping_pending", "pending_windows": ["5h"], "skip_reason": skip}
        r = _reason(tmp_path, _fresh(keeper_state=ks, usage_cached_at=_now() - 2 * 3600))
        assert r["text"].startswith("last read ")
        assert r["muted"] is True

    @pytest.mark.parametrize(
        "pending, five_hour, seven_day",
        [
            (["5h"], "window opening", None),
            (["7d"], None, "window opening"),
            (["5h", "7d"], "window opening", "window opening"),
        ],
    )
    def test_only_a_window_the_ping_waits_for_reads_as_opening(
        self, tmp_path, pending, five_hour, seven_day,
    ):
        """A reset-less weekly reading is normal (the API omits the weekly reset
        for days): the weekly bar must not borrow the 5h window's caption."""
        acct = _fresh(
            cached_usage_5h=40.0, cached_5h_resets_at=PAST,
            cached_usage_7d=30.0, cached_7d_resets_at=None,
            keeper_state={"last_status": "ping_pending", "pending_windows": pending},
        )
        r = _run(tmp_path, f"const a = {json.dumps(acct)};\n"
                           "out([windowReason(a, 'five_hour'), windowReason(a, 'seven_day')]);")
        assert [x and x["text"] for x in r] == [five_hour, seven_day]

    @pytest.mark.parametrize("pending", [None, "5h"], ids=["missing", "not-a-list"])
    def test_a_pending_status_without_a_window_list_claims_nothing(self, tmp_path, pending):
        """Same rule as keeper_confirmation._pending_windows: the sweep does not
        treat this state as pending, so the caption does not either."""
        ks = {"last_status": "ping_pending", "pending_windows": pending}
        assert _reason(tmp_path, _fresh(keeper_state=ks)) is None

    @pytest.mark.parametrize(
        "status, text",
        [("ok", "window lapsed"), ("ping_ok_no_window", "retry ")],
    )
    def test_the_confirming_reason_alone_is_not_a_pending_ping(self, tmp_path, status, text):
        """Every status write clears skip_reason, and the hold is a swap on the
        pending ping, so this pair is not a state the keeper leaves behind. If
        it appears, last_status decides, also beside an old window list."""
        ks = {"last_status": status, "skip_reason": "confirming", "pending_windows": ["5h"],
              "next_attempt_at": _now() + 600}
        assert _reason(tmp_path, _fresh(keeper_state=ks))["text"].startswith(text)

    def test_the_server_payload_carries_the_pending_windows(self, tmp_path):
        """The contract end to end: the stored blob, the API model, the caption."""
        from jacked.api.routes.auth import _build_keeper_state

        blob = json.dumps({"last_status": "ping_pending", "pending_windows": ["7d"],
                           "pending_until": _now() + 600, "pending_reason": "no_header",
                           "token_fp": "not-a-real-fp"})
        payload = _build_keeper_state(blob).model_dump()
        assert payload["pending_windows"] == ["7d"]
        assert "token_fp" not in payload
        acct = _fresh(cached_usage_5h=40.0, cached_5h_resets_at=PAST,
                      cached_usage_7d=30.0, cached_7d_resets_at=None, keeper_state=payload)
        r = _run(tmp_path, f"const a = {json.dumps(acct)};\n"
                           "out([windowReason(a, 'five_hour'), windowReason(a, 'seven_day')]);")
        assert [x and x["text"] for x in r] == [None, "window opening"]

    def test_rate_limited_without_a_time(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "rate_limited"})
        assert _reason(tmp_path, acct)["text"] == "rate limited"

    @pytest.mark.parametrize("status", ["transport", "auth_failed", "error", "ping_ok_no_window"])
    def test_retrying_shows_the_next_attempt(self, tmp_path, status):
        acct = _fresh(keeper_state={"last_status": status, "next_attempt_at": _now() + 300})
        r = _reason(tmp_path, acct)
        assert r["text"].startswith("retry ")
        assert r["short"] == "retrying"

    def test_ok_ping_but_past_reset_is_window_lapsed(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "ok"})
        assert _reason(tmp_path, acct)["text"] == "window lapsed"

    def test_codex_reads_as_last_read_and_muted(self, tmp_path):
        acct = _fresh(provider="codex", usage_cached_at=_now() - 51 * 86400)
        r = _reason(tmp_path, acct)
        assert r["text"].startswith("last read ")
        assert r["short"].startswith("read ")
        assert r["muted"] is True

    def test_fresh_codex_reading_is_not_muted(self, tmp_path):
        """Dimmed means stale; a minute-old Codex reading is current."""
        r = _reason(tmp_path, _fresh(provider="codex"))
        assert r["text"].startswith("last read ")
        assert r["muted"] is False

    def test_stale_claude_reading_is_muted(self, tmp_path):
        r = _reason(tmp_path, _fresh(usage_cached_at=_now() - 3600))
        assert r["text"].startswith("last read ")
        assert r["muted"] is True

    def test_no_keeper_state_keeps_the_plain_text(self, tmp_path):
        assert _reason(tmp_path, _fresh()) is None


class TestRenderUsageBar:
    def _bar(self, tmp_path, acct, resets_at, compact=False):
        base = "{ compact: true }" if compact else "undefined"
        acct = dict(acct, cached_usage_5h=40, cached_5h_resets_at=resets_at)
        return _run(
            tmp_path,
            f"const a = {json.dumps(acct)};\n"
            f"out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, "
            f"windowElapsed(a, 'five_hour'), '5h limit', "
            f"usageBarOpts(a, 'five_hour', {base})));",
        )

    def test_reason_replaces_no_active_window(self, tmp_path):
        html = self._bar(tmp_path, _fresh(keeper_state={"last_status": "needs_reauth"}), PAST)
        assert "needs re-auth" in html
        assert "no active window" not in html
        assert "text-amber-400" in html
        assert "elapsed-marker" not in html

    def test_plain_fallback_without_keeper_state(self, tmp_path):
        assert "no active window" in self._bar(tmp_path, _fresh(), PAST)

    def test_future_reset_ignores_the_reason(self, tmp_path):
        html = self._bar(tmp_path, _fresh(keeper_state={"last_status": "needs_reauth"}), FUTURE)
        assert "resets " in html
        assert "needs re-auth" not in html

    def test_stale_bar_dims_the_bar_but_not_the_reason_text(self, tmp_path):
        html = self._bar(tmp_path, _fresh(provider="codex", usage_cached_at=_now() - 86400 * 3), PAST)
        assert 'class="usage-bar flex-1 opacity-60"' in html
        assert "last read " in html
        # The reason text keeps its base tone: it explains the dimming.
        assert 'reset-text text-xs text-slate-500' in html
        assert 'mb-1 opacity-60' not in html

    def test_reset_column_never_wraps(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "rate_limited", "next_attempt_at": _now() + 86400 * 2})
        html = self._bar(tmp_path, acct, PAST)
        assert "w-32 text-right whitespace-nowrap truncate" in html

    def test_compact_shows_the_short_reason_with_its_tone(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "needs_reauth"})
        html = self._bar(tmp_path, acct, PAST, compact=True)
        assert ">needs re-auth</span>" in html
        assert "reset-caption text-[10px] text-amber-400" in html
        assert 'title="5h limit · needs re-auth"' in html
        assert "—" not in html

    def test_compact_lapsed_window_without_a_reason_reads_no_window(self, tmp_path):
        html = self._bar(tmp_path, _fresh(), PAST, compact=True)
        assert ">no window</span>" in html
        assert "—" not in html

    def test_a_pending_ping_renders_window_opening_with_its_hover_title(self, tmp_path):
        acct = _fresh(keeper_state={
            "last_status": "ping_pending", "pending_windows": ["5h"],
            "skip_reason": "confirming", "next_attempt_at": _now() + 600,
        })
        full = self._bar(tmp_path, acct, PAST)
        assert "window opening" in full
        assert "retry" not in full and "window lapsed" not in full
        compact = self._bar(tmp_path, acct, PAST, compact=True)
        assert ">opening</span>" in compact
        assert 'title="5h limit · window opening"' in compact


class TestElapsedFraction:
    def test_past_reset_has_no_marker(self, tmp_path):
        r = _run(
            tmp_path,
            f"out([computeElapsedFraction({json.dumps(PAST)}, 18000), "
            f"computeElapsedFraction({json.dumps(PAST)}, 604800)]);",
        )
        assert r == [None, None]

    def test_mid_window_fraction(self, tmp_path):
        r = _run(
            tmp_path,
            "const reset = new Date(Date.now() + 2.5 * 3600 * 1000).toISOString();\n"
            "out(computeElapsedFraction(reset, 5 * 3600));",
        )
        assert 0.45 < r < 0.55

    def test_missing_reset(self, tmp_path):
        r = _run(tmp_path, "out([computeElapsedFraction(null, 18000), computeElapsedFraction('', 604800)]);")
        assert r == [None, None]

    def test_missing_or_non_positive_length_has_no_marker(self, tmp_path):
        r = _run(
            tmp_path,
            "const reset = new Date(Date.now() + 3600 * 1000).toISOString();\n"
            "out([computeElapsedFraction(reset), computeElapsedFraction(reset, null),\n"
            "     computeElapsedFraction(reset, 0), computeElapsedFraction(reset, -5),\n"
            "     computeElapsedFraction(reset, 'x')]);",
        )
        assert r == [None] * 5

    def test_reset_further_than_one_window_reads_as_the_start(self, tmp_path):
        r = _run(
            tmp_path,
            "const reset = new Date(Date.now() + 10 * 3600 * 1000).toISOString();\n"
            "out(computeElapsedFraction(reset, 5 * 3600));",
        )
        assert r == 0

    def test_unparseable_reset(self, tmp_path):
        assert _run(tmp_path, "out(computeElapsedFraction('not a date', 18000));") is None

    def test_fixed_length_helpers_are_gone(self, tmp_path):
        """Every bar reads its window length from the payload: no fixed 5h/7d math."""
        r = _run(tmp_path, "out([typeof computeElapsedFraction5h, typeof computeElapsedFraction7d]);")
        assert r == ["undefined", "undefined"]


class TestUnreportedCodexWindow:
    """A Codex window Codex does not report is "not reported", never 0%.

    Live shape (2026-10-27): a weekly-only Pro account reports a lone
    primary window of 10080 minutes and ``secondary: null``, so the 5h
    columns are cleared to null while the 7d reads 100%.
    """

    def _bar(self, tmp_path, acct, pct, compact=False):
        base = "{ compact: true }" if compact else "null"
        acct = dict(acct, cached_usage_5h=pct, cached_5h_resets_at=None)
        return _run(
            tmp_path,
            f"const a = {json.dumps(acct)};\n"
            f"out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, "
            f"windowElapsed(a, 'five_hour'), '5h limit', "
            f"usageBarOpts(a, 'five_hour', {base})));",
        )

    def test_codex_null_window_is_not_reported(self, tmp_path):
        acct = dict(_fresh(provider="codex"), cached_usage_5h=None)
        r = _run(tmp_path, f"out(windowReason({json.dumps(acct)}, 'five_hour'));")
        assert r["text"] == "not reported"
        assert r["absent"] is True
        assert "none" not in r, "not reported is n/a, never none"

    def test_codex_null_window_draws_no_fill_and_no_percent(self, tmp_path):
        html = self._bar(tmp_path, _fresh(provider="codex"), None)
        assert 'class="fill' not in html
        assert ">n/a</span>" in html
        assert "0%" not in html
        assert "not reported" in html

    def test_compact_codex_null_window(self, tmp_path):
        html = self._bar(tmp_path, _fresh(provider="codex"), None, compact=True)
        assert ">not reported</span>" in html
        assert ">n/a</span>" in html
        assert 'class="fill' not in html

    def test_codex_reported_window_keeps_its_bar(self, tmp_path):
        html = self._bar(tmp_path, _fresh(provider="codex"), 100)
        assert 'class="fill red" style="width: 100.0%"' in html
        assert ">100%</span>" in html

    def test_claude_null_window_stays_zero(self, tmp_path):
        # Claude's null window is "no active window": 0% used is the truth.
        html = self._bar(tmp_path, _fresh(), None)
        assert ">0%</span>" in html
        assert 'class="fill green"' in html
        assert "not reported" not in html

    def test_never_read_codex_account_has_no_absent_reason(self, tmp_path):
        acct = {"id": 1, "provider": "codex", "usage_cached_at": None, "cached_usage_5h": None}
        assert _run(tmp_path, f"out(windowReason({json.dumps(acct)}, 'five_hour'));") is None


class TestUsageBlockBadge:
    def _badge(self, tmp_path, acct):
        return _run(tmp_path, f"out(renderUsageBlockBadge({json.dumps(acct)}));")

    def test_no_block_no_badge(self, tmp_path):
        assert self._badge(tmp_path, _fresh(provider="codex")) == ""

    def test_limit_reached_badge_names_the_reset(self, tmp_path):
        html = self._badge(tmp_path, _fresh(
            provider="codex", usage_block={"reason": "limit_reached", "resets_at": FUTURE}))
        assert ">Limit reached</span>" in html
        assert "badge badge-danger" in html
        assert "Codex blocks new requests for this account. The limit resets " in html
        assert "—" not in html

    def test_spend_limit_and_generic_labels(self, tmp_path):
        spend = self._badge(tmp_path, _fresh(provider="codex", usage_block={"reason": "spend_limit"}))
        other = self._badge(tmp_path, _fresh(provider="codex", usage_block={"reason": "blocked"}))
        assert ">Spend limit reached</span>" in spend
        assert ">Usage blocked</span>" in other
        assert "The limit" not in spend


class TestBlockAndHintTitles:
    def test_stale_block_title_names_the_last_read(self, tmp_path):
        acct = _fresh(provider="codex", usage_block={
            "reason": "limit_reached", "resets_at": FUTURE, "read_at": _now() - 3 * 86400})
        r = _run(tmp_path, f"out(usageBlockText({json.dumps(acct)}));")
        assert r["label"] == "Limit reached"
        assert "Last usage read " in r["title"]

    def test_fresh_block_title_has_no_read_note(self, tmp_path):
        acct = _fresh(provider="codex", usage_block={
            "reason": "limit_reached", "resets_at": FUTURE, "read_at": _now() - 60})
        r = _run(tmp_path, f"out(usageBlockText({json.dumps(acct)}));")
        assert "Last usage read" not in r["title"]

    def test_not_reported_hover_explains_itself(self, tmp_path):
        acct = dict(_fresh(provider="codex"), cached_usage_5h=None)
        html = _run(
            tmp_path,
            f"const a = {json.dumps(acct)};\n"
            "out(renderUsageBar(a.cached_usage_5h, a.cached_5h_resets_at, "
            "windowElapsed(a, 'five_hour'), '5h limit', usageBarOpts(a, 'five_hour')));",
        )
        assert 'title="Codex does not report this limit for this account."' in html
