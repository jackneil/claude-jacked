"""A Codex window Codex does not report, and Codex's own usage block.

The live shape that drove this (2026-10-27): a weekly-only Pro account
reports one primary window of 10080 minutes and ``secondary: null``, so the
5h cache columns are cleared to null while the 7d reads 100%, and the same
payload says ``ordinaryUsageAllowed: false``. Every surface used to coerce
the null 5h to 0, which read as free headroom on a locked-out account.
"""
import base64
import json
import time
from unittest.mock import patch

from jacked.api.routes.auth import _account_to_response, _build_account_usage
from jacked.service.menubar_summary import (
    compute_worst_account_summary,
    menubar_title,
    summarize_account,
    usage_block,
    usage_block_of_row,
)

RESET = int(time.time()) + 6 * 86400

# The real account/rateLimits/read result, identifiers dropped.
LOCKED_OUT = {
    "ordinaryUsageAllowed": False,
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": RESET},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "spendControlReached": False,
        "planType": "pro",
        "rateLimitReachedType": "rate_limit_reached",
    },
    "rateLimitResetCredits": {"availableCount": 0},
    "rateLimitUpsell": {
        "banner_type": "pro_rate_limit_reached",
        "title": "You're out of Codex messages",
        "reset_at": RESET,
    },
}

OPEN = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": RESET},
        "secondary": None,
        "spendControlReached": False,
        "rateLimitReachedType": None,
    },
}


def _codex_row(raw, five=None, seven=100.0):
    return {
        "id": 14,
        "provider": "codex",
        "email": "c@x.com",
        "expires_at": int(time.time()) + 3600,
        "cached_usage_5h": five,
        "cached_usage_7d": seven,
        "cached_7d_resets_at": "2099-01-01T00:00:00+00:00",
        "usage_cached_at": int(time.time()) - 60,
        "cached_usage_raw": json.dumps(raw),
    }


class TestUsageBlock:
    def test_locked_out_payload_is_limit_reached_until_the_upsell_reset(self):
        block = usage_block(LOCKED_OUT)
        assert block["reason"] == "limit_reached"
        assert block["resets_at"].startswith(time.strftime("%Y-", time.gmtime(RESET)))

    def test_open_payload_has_no_block(self):
        assert usage_block(OPEN) is None

    def test_spend_control_blocks_below_100_percent(self):
        raw = json.loads(json.dumps(OPEN))
        raw["ordinaryUsageAllowed"] = False
        raw["rateLimits"]["spendControlReached"] = True
        assert usage_block(raw)["reason"] == "spend_limit"

    def test_reset_falls_back_to_the_exhausted_window(self):
        raw = json.loads(json.dumps(LOCKED_OUT))
        del raw["rateLimitUpsell"]
        assert usage_block(raw, now=0)["resets_at"] is not None

    def test_a_block_past_its_reset_is_dropped(self):
        assert usage_block(LOCKED_OUT, now=RESET + 1) is None

    def test_claude_payload_never_blocks(self):
        assert usage_block({"five_hour": {"utilization": 100}}) is None

    def test_malformed_row_reads_as_no_block(self):
        assert usage_block_of_row({"cached_usage_raw": "{"}) is None


class TestAccountApi:
    def test_unreported_codex_window_is_null_not_zero(self):
        usage = _build_account_usage(_codex_row(LOCKED_OUT))
        assert usage.five_hour is None
        assert usage.seven_day == 100.0

    def test_claude_null_window_stays_zero(self):
        row = _codex_row(OPEN, five=None, seven=12.0)
        row["provider"] = "claude"
        assert _build_account_usage(row).five_hour == 0

    def test_response_carries_the_usage_block(self):
        resp = _account_to_response(_codex_row(LOCKED_OUT))
        assert resp.usage_block is not None
        assert resp.usage_block.reason == "limit_reached"
        assert resp.cached_usage_5h is None

    def test_open_account_has_no_usage_block(self):
        assert _account_to_response(_codex_row(OPEN, seven=40.0)).usage_block is None

    def test_pro_plan_drops_a_stale_five_hour_value(self):
        # ChatGPT Pro has no 5h window in Codex: a 5h value left in the cache
        # from an older payload is not a reading.
        row = _codex_row(OPEN, five=55.0, seven=40.0)
        row["subscription_type"] = "pro"
        resp = _account_to_response(row)
        assert resp.usage.five_hour is None
        assert resp.windows.five_hour.state == "absent"
        assert resp.plan.label == "Pro"


class TestMenubarSummary:
    def test_codex_missing_window_stays_none(self):
        s = summarize_account(_codex_row(OPEN, seven=40.0))
        assert s["five_hour"] is None
        assert s["seven_day"] == 40.0
        assert s["max_pct"] == 40.0
        assert s["color"] == "green"
        assert s["blocked"] is False

    def test_blocked_account_is_red_at_any_percent(self):
        raw = json.loads(json.dumps(OPEN))
        raw["ordinaryUsageAllowed"] = False
        raw["rateLimits"]["spendControlReached"] = True
        s = summarize_account(_codex_row(raw, seven=40.0))
        assert s["blocked"] is True
        assert s["color"] == "red"

    def test_worst_summary_keeps_the_codex_slot_empty(self):
        claude = {"id": 1, "email": "a@x.com", "cached_usage_5h": 10, "cached_usage_7d": 20}
        worst = compute_worst_account_summary([claude, _codex_row(LOCKED_OUT)])
        assert worst["account_id"] == 14
        assert worst["five_hour"] is None
        assert menubar_title(worst) == "-·100%"

    def test_claude_null_window_still_reads_zero(self):
        s = summarize_account({"id": 1, "cached_usage_5h": None, "cached_usage_7d": 30})
        assert s["five_hour"] == 0.0
        assert menubar_title(s) == "0%·30%"

    def test_pro_plan_keeps_the_five_hour_slot_empty(self):
        row = _codex_row(OPEN, five=55.0, seven=40.0)
        row["subscription_type"] = "pro"
        s = summarize_account(row)
        assert s["five_hour"] is None
        assert s["max_pct"] == 40.0
        assert menubar_title(s) == "-·40%"


class TestReviewEngineBlock:
    """``jacked dcr engine`` must not report Codex usable while Codex blocks it."""

    @staticmethod
    def _home(tmp_path, raw, org="org-live", email="dev@example.com", stored=None):
        """A jacked home with one stored Codex account and a live auth.json.

        ``stored`` overrides the stored account's (org, email); by default it
        is the live account.
        """
        from jacked.web.database import Database

        def b64(obj):
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        jwt = ".".join([
            b64({"alg": "RS256"}),
            b64({"email": email, "exp": 9999999999,
                 "https://api.openai.com/auth": {"chatgpt_account_id": org,
                                                 "chatgpt_plan_type": "pro"}}),
            "c2ln",
        ])
        codex = tmp_path / "codex"
        codex.mkdir()
        (codex / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {"id_token": jwt, "access_token": "a", "refresh_token": "r",
                       "account_id": org},
        }))
        (tmp_path / ".claude").mkdir()
        db = Database(db_path=str(tmp_path / ".claude" / "jacked.db"))
        s_org, s_email = stored or (org, email)
        acct = db.create_account(s_email, "tok", int(time.time()) + 3600,
                                 provider="codex", organization_uuid=s_org)
        db.update_account(acct["id"], cached_usage_raw=json.dumps(raw))
        return {"CODEX_HOME": str(codex)}

    def test_blocked_live_account_is_reported(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block

        env = self._home(tmp_path, LOCKED_OUT)
        assert codex_usage_block(tmp_path, env=env)["reason"] == "limit_reached"

    def test_open_account_is_not_blocked(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block

        env = self._home(tmp_path, OPEN)
        assert codex_usage_block(tmp_path, env=env) is None

    def test_other_account_blocked_does_not_count(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block

        env = self._home(tmp_path, LOCKED_OUT, stored=("org-other", "other@example.com"))
        assert codex_usage_block(tmp_path, env=env) is None

    def test_resolve_reports_not_usable_with_the_reason(self, tmp_path, monkeypatch):
        from jacked import dcr_settings

        env = self._home(tmp_path, LOCKED_OUT)
        monkeypatch.setenv("CODEX_HOME", env["CODEX_HOME"])
        dcr_settings.update_config(tmp_path, engine="codex")
        ready = {"codex_installed": True, "codex_logged_in": True,
                 "codex_path": "/x/codex", "codex_version": "9.9.9", "reason": None}
        with patch("jacked.dcr_settings.codex_preflight", return_value=ready):
            r = dcr_settings.resolve(tmp_path)
        assert r["usable"] is False
        assert r["reason"].startswith("Codex usage limit reached (resets ")


class TestEngineRowSetAndFreshness:
    """The engine check reads the row the usage poller keeps current."""

    def test_disabled_row_is_ignored(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block
        from jacked.web.database import Database

        env = TestReviewEngineBlock._home(tmp_path, LOCKED_OUT)
        db = Database(db_path=str(tmp_path / ".claude" / "jacked.db"))
        acct = db.list_accounts(include_inactive=True)[0]
        db.update_account(acct["id"], is_active=0)
        assert codex_usage_block(tmp_path, env=env) is None

    def test_deleted_row_is_ignored(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block
        from jacked.web.database import Database

        env = TestReviewEngineBlock._home(tmp_path, LOCKED_OUT)
        db = Database(db_path=str(tmp_path / ".claude" / "jacked.db"))
        acct = db.list_accounts(include_inactive=True)[0]
        db.delete_account(acct["id"])
        assert codex_usage_block(tmp_path, env=env) is None

    def test_email_fallback_matches_a_personal_account(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block

        env = TestReviewEngineBlock._home(tmp_path, LOCKED_OUT, org="",
                                          stored=("", "dev@example.com"))
        assert codex_usage_block(tmp_path, env=env)["reason"] == "limit_reached"

    def test_stale_block_without_a_reset_is_ignored(self, tmp_path):
        from jacked.dcr_settings import codex_usage_block
        from jacked.web.database import Database

        spend = json.loads(json.dumps(OPEN))
        spend["ordinaryUsageAllowed"] = False
        spend["rateLimits"]["spendControlReached"] = True
        env = TestReviewEngineBlock._home(tmp_path, spend)
        db = Database(db_path=str(tmp_path / ".claude" / "jacked.db"))
        acct = db.list_accounts()[0]
        db.update_account(acct["id"], usage_cached_at=int(time.time()) - 60)
        assert codex_usage_block(tmp_path, env=env)["reason"] == "spend_limit"
        db.update_account(acct["id"], usage_cached_at=int(time.time()) - 3 * 86400)
        assert codex_usage_block(tmp_path, env=env) is None

    def test_open_live_account_keeps_the_engine_usable(self, tmp_path, monkeypatch):
        from jacked import dcr_settings

        env = TestReviewEngineBlock._home(tmp_path, OPEN)
        monkeypatch.setenv("CODEX_HOME", env["CODEX_HOME"])
        dcr_settings.update_config(tmp_path, engine="codex")
        ready = {"codex_installed": True, "codex_logged_in": True,
                 "codex_path": "/x/codex", "codex_version": "9.9.9", "reason": None}
        with patch("jacked.dcr_settings.codex_preflight", return_value=ready):
            assert dcr_settings.resolve(tmp_path)["usable"] is True


class TestVerdictGate:
    def test_credits_keep_ordinary_usage_allowed(self):
        raw = json.loads(json.dumps(LOCKED_OUT))
        raw["ordinaryUsageAllowed"] = True
        raw["rateLimits"]["credits"] = {"hasCredits": True, "unlimited": False, "balance": "40"}
        assert usage_block(raw) is None


class TestCallSites:
    """Source guards on the JavaScript call sites of the usage bar helpers.

    A literal ``null`` elapsed fraction is how the Fable row lost its
    week-elapsed marker while the 7d row above it kept one, and a bar whose
    options do not name their window cannot read the plan's window state
    (an absent 5h window then reads as a 0% bar). Every call must have the
    checked shape, so a new call in another shape fails here.
    """

    @staticmethod
    def _is_window_key(arg):
        """True for a quoted 'five_hour' or 'seven_day' literal."""
        import re

        return re.fullmatch(r"""(['"])(five_hour|seven_day)\1""", arg) is not None

    @staticmethod
    def _skip_quoted(text, i):
        """Index after the string literal that starts at text[i]."""
        quote = text[i]
        i += 1
        while text[i] != quote:
            i += 2 if text[i] == "\\" else 1
        return i + 1

    @classmethod
    def _skip_template(cls, text, i):
        """Index after the template literal that starts at text[i]."""
        i += 1
        while text[i] != "`":
            if text[i] == "\\":
                i += 2
            elif text.startswith("${", i):
                i = cls._scan(text, i + 2, "}")[1]
            else:
                i += 1
        return i + 1

    @classmethod
    def _scan(cls, text, i, close):
        """Top-level comma-separated parts up to the matching ``close``.

        Returns (parts, index after the close). Strings, template literals
        and nested brackets are skipped as units.
        """
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack, parts, start = [close], [], i
        while True:
            ch = text[i]
            if ch in "'\"":
                i = cls._skip_quoted(text, i)
                continue
            if ch == "`":
                i = cls._skip_template(text, i)
                continue
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in ")]}":
                assert ch == stack.pop(), f"unbalanced {ch!r} at {i}"
                if not stack:
                    parts.append(text[start:i].strip())
                    return [p for p in parts if p], i + 1
            elif ch == "," and len(stack) == 1:
                parts.append(text[start:i].strip())
                start = i + 1
            i += 1

    @classmethod
    def _calls(cls, name):
        """(file, line, args) for every call of ``name`` in the web JS.

        Skips the vendored libraries, the function's own definition and
        comment lines (a docblock may name the function with parentheses).
        """
        import re
        from pathlib import Path

        web = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"
        calls = []
        pattern = re.compile(r"(?<![\w.$])" + re.escape(name) + r"\s*\(")
        for path in sorted(web.rglob("*.js")):
            if "vendor" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                prefix = text[line_start:m.start()]
                if re.search(r"function\s*$", prefix):
                    continue
                if prefix.lstrip().startswith(("*", "//", "/*")) or "//" in prefix:
                    continue
                args, _ = cls._scan(text, m.end(), ")")
                line = text.count("\n", 0, m.start()) + 1
                calls.append((path.name, line, args))
        return calls

    def test_scanner_reads_nested_and_quoted_arguments(self):
        text = "f(a(1, 2), `x ${g(3, 4)} ,`, 'a,b', {c: [5, 6]}, ')')"
        args, end = self._scan(text, 2, ")")
        assert args == ["a(1, 2)", "`x ${g(3, 4)} ,`", "'a,b'", "{c: [5, 6]}", "')'"]
        assert end == len(text)

    def test_every_usage_bar_passes_an_elapsed_fraction(self):
        calls = self._calls("renderUsageBar")
        # card 5h/7d, panel 5h/7d, WebSocket 5h/7d and renderModelBar.
        assert len(calls) >= 7, calls
        for name, line, args in calls:
            assert len(args) >= 3, (name, line, args)
            assert args[2] not in ("null", "undefined", "void 0"), (
                f"{name}:{line} passes a literal {args[2]} elapsed fraction: "
                "route model bars through renderModelBar and window bars "
                "through windowElapsed"
            )

    def test_every_usage_bar_opts_call_names_its_window(self):
        calls = self._calls("usageBarOpts")
        assert len(calls) >= 6, calls
        for name, line, args in calls:
            assert len(args) >= 2 and self._is_window_key(args[1]), (
                f"{name}:{line} must pass a quoted window key "
                f"('five_hour' or 'seven_day') as the second argument: {args}"
            )

    def test_every_window_elapsed_call_names_its_window(self):
        calls = self._calls("windowElapsed")
        assert len(calls) >= 6, calls
        for name, line, args in calls:
            assert len(args) == 2 and self._is_window_key(args[1]), (name, line, args)
