"""The reset column says WHY a usage window has no future reset.

Evals utils.js + usage.js under node and asserts on windowReason() and the
rendered renderUsageBar() HTML: every keeper reason string, the muted
"last read" form for Codex and stale readings, the compact variant's em-dash
caption with the reason in its hover title, and no elapsed marker for a
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

NOW = int(time.time())
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
    acct = {"id": 1, "provider": "claude", "usage_cached_at": NOW - 60}
    acct.update(kw)
    return acct


class TestWindowReason:
    def test_open_window_has_no_reason(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "needs_reauth"})
        assert _reason(tmp_path, acct, FUTURE) is None

    def test_needs_reauth_uses_the_warning_tone(self, tmp_path):
        r = _reason(tmp_path, _fresh(keeper_state={"last_status": "needs_reauth"}))
        assert r == {"text": "needs re-auth", "tone": "warn", "muted": False}

    def test_needs_reauth_beats_a_stale_reading(self, tmp_path):
        acct = _fresh(usage_cached_at=NOW - 86400, keeper_state={"last_status": "needs_reauth"})
        assert _reason(tmp_path, acct)["text"] == "needs re-auth"

    def test_paused_uses_the_default_keeper_start(self, tmp_path):
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours"})
        assert _reason(tmp_path, acct)["text"] == "paused until 06:00"

    def test_paused_uses_the_configured_start(self, tmp_path):
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours"})
        r = _reason(tmp_path, acct, settings={"window_keeper_active_start": "07:30"})
        assert r["text"] == "paused until 07:30"

    @pytest.mark.parametrize(
        "skip, text",
        [
            ("keeper_disabled", "keeper off"),
            ("no_token", "no Claude Code token"),
            ("auto_swap_disabled", "auto-swap off"),
        ],
    )
    def test_skip_reasons(self, tmp_path, skip, text):
        r = _reason(tmp_path, _fresh(keeper_state={"skip_reason": skip}))
        assert r == {"text": text, "tone": "muted", "muted": False}

    def test_rate_limited_shows_the_retry_time(self, tmp_path):
        acct = _fresh(keeper_state={
            "last_status": "rate_limited", "skip_reason": "backoff",
            "next_attempt_at": NOW + 600,
        })
        assert _reason(tmp_path, acct)["text"].startswith("rate limited until ")

    def test_rate_limited_without_a_time(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "rate_limited"})
        assert _reason(tmp_path, acct)["text"] == "rate limited"

    @pytest.mark.parametrize("status", ["transport", "auth_failed", "error", "ping_ok_no_window"])
    def test_retrying_shows_the_next_attempt(self, tmp_path, status):
        acct = _fresh(keeper_state={"last_status": status, "next_attempt_at": NOW + 300})
        assert _reason(tmp_path, acct)["text"].startswith("retrying ")

    def test_ok_ping_but_past_reset_is_window_lapsed(self, tmp_path):
        acct = _fresh(keeper_state={"last_status": "ok"})
        assert _reason(tmp_path, acct)["text"] == "window lapsed"

    def test_codex_reads_as_last_read_and_muted(self, tmp_path):
        acct = _fresh(provider="codex", usage_cached_at=NOW - 51 * 86400)
        r = _reason(tmp_path, acct)
        assert r["text"].startswith("last read ")
        assert r["muted"] is True

    def test_stale_claude_reading_is_muted(self, tmp_path):
        r = _reason(tmp_path, _fresh(usage_cached_at=NOW - 3600))
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

    def test_stale_bar_is_muted(self, tmp_path):
        html = self._bar(tmp_path, _fresh(provider="codex", usage_cached_at=NOW - 86400 * 3), PAST)
        assert "opacity-60" in html
        assert "last read " in html

    def test_compact_keeps_the_em_dash_caption_with_reason_in_title(self, tmp_path):
        acct = _fresh(keeper_state={"skip_reason": "outside_active_hours"})
        html = self._bar(tmp_path, acct, PAST, compact=True)
        assert ">—</span>" in html
        assert 'title="5h limit · paused until 06:00"' in html


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
