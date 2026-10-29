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
    return _run(
        tmp_path,
        f"window.jackedState.settings = {json.dumps(settings or {})};\n"
        f"out(windowReason({json.dumps(acct)}, {json.dumps(resets_at)}));",
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
        ]
        for ks in states:
            r = _reason(tmp_path, _fresh(keeper_state=ks))
            assert r is not None and len(r["short"]) <= 14, (ks, r)

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
        return _run(
            tmp_path,
            f"const a = {json.dumps(acct)};\n"
            f"out(renderUsageBar(40, {json.dumps(resets_at)}, "
            f"computeElapsedFraction5h({json.dumps(resets_at)}), '5h limit', "
            f"usageBarOpts(a, {json.dumps(resets_at)}, {base})));",
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


class TestElapsedFraction:
    def test_past_reset_has_no_marker(self, tmp_path):
        r = _run(tmp_path, f"out([computeElapsedFraction5h({json.dumps(PAST)}), computeElapsedFraction7d({json.dumps(PAST)})]);")
        assert r == [None, None]

    def test_mid_window_fraction(self, tmp_path):
        r = _run(
            tmp_path,
            "const reset = new Date(Date.now() + 2.5 * 3600 * 1000).toISOString();\n"
            "out(computeElapsedFraction5h(reset));",
        )
        assert 0.45 < r < 0.55

    def test_missing_reset(self, tmp_path):
        assert _run(tmp_path, "out([computeElapsedFraction5h(null), computeElapsedFraction7d('')]);") == [None, None]


class TestUnreportedCodexWindow:
    """A Codex window Codex does not report is "not reported", never 0%.

    Live shape (2026-10-27): a weekly-only Pro account reports a lone
    primary window of 10080 minutes and ``secondary: null``, so the 5h
    columns are cleared to null while the 7d reads 100%.
    """

    def _bar(self, tmp_path, acct, pct, compact=False):
        base = "{ compact: true }" if compact else "null"
        return _run(
            tmp_path,
            f"const a = {json.dumps(acct)};\n"
            f"out(renderUsageBar({json.dumps(pct)}, null, null, '5h limit', "
            f"usageBarOpts(a, null, {base}, {json.dumps(pct)})));",
        )

    def test_codex_null_window_is_not_reported(self, tmp_path):
        r = _run(tmp_path, f"out(windowReason({json.dumps(_fresh(provider='codex'))}, null, undefined, null));")
        assert r["text"] == "not reported"
        assert r["absent"] is True

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
        acct = {"id": 1, "provider": "codex", "usage_cached_at": None}
        assert _run(tmp_path, f"out(windowReason({json.dumps(acct)}, null, undefined, null));") is None


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
        html = _run(
            tmp_path,
            f"const a = {json.dumps(_fresh(provider='codex'))};\n"
            "out(renderUsageBar(null, null, null, '5h limit', usageBarOpts(a, null, null, null)));",
        )
        assert 'title="Codex does not report this limit for this account."' in html
