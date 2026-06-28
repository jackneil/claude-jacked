"""M4: Codex usage via `codex app-server` JSON-RPC.

Covers the normalizer (against a RECORDED real app-server response), the
DB cache write (5h/7d percents + ISO reset times land in the same columns the
Anthropic path uses), a real subprocess round-trip through a fake app-server
that speaks the newline-delimited JSON-RPC protocol, and the web.auth.fetch_usage
provider DISPATCH (codex accounts route to the Codex leaf — and only the
account live in the shared root is polled; non-active ones keep their cache).
"""

import asyncio
import base64
import json
import stat
import sys
from datetime import datetime, timezone

import pytest

from jacked.codex import usage as cu
from jacked.web.database import Database

# Verbatim result captured from a live `codex app-server` 0.142.3
# account/rateLimits/read (token values were never in this payload).
RECORDED_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 2, "windowDurationMins": 300, "resetsAt": 1782683811},
        "secondary": {"usedPercent": 26, "windowDurationMins": 10080, "resetsAt": 1783199300},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "pro",
        "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 2}, "planType": "pro"},
        "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark", "primary": {"usedPercent": 0}},
    },
    "rateLimitResetCredits": {"availableCount": 0},
}


# --------------------------------------------------------------------------
# Normalizer
# --------------------------------------------------------------------------

def test_normalize_maps_primary_to_5h_secondary_to_7d():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    assert norm["five_hour"]["utilization"] == 2
    assert norm["seven_day"]["utilization"] == 26
    assert norm["plan_type"] == "pro"


def test_normalize_converts_epoch_resets_to_iso():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    five = norm["five_hour"]["resets_at"]
    # ISO string that parses back to the original epoch
    assert datetime.fromisoformat(five) == datetime.fromtimestamp(
        1782683811, tz=timezone.utc
    )


def test_normalize_handles_missing_windows():
    norm = cu.normalize_rate_limits({"rateLimits": {}})
    assert norm["five_hour"]["utilization"] is None
    assert norm["five_hour"]["resets_at"] is None
    assert norm["seven_day"]["utilization"] is None


def test_normalize_surfaces_credits_and_reset_credits():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    assert norm["credits"]["balance"] == "0"
    assert norm["reset_credits"]["availableCount"] == 0
    assert "codex_bengalfox" in norm["by_limit"]


def test_epoch_to_iso_none_and_garbage():
    assert cu._epoch_to_iso(None) is None
    assert cu._epoch_to_iso("not-a-number") is None


# --------------------------------------------------------------------------
# fetch_codex_usage -> DB cache (mocked driver)
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


def test_fetch_codex_usage_writes_cache(db, tmp_path, monkeypatch):
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def fake_read(**kwargs):
        return RECORDED_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    # home=tmp keeps the swap lock out of the real ~/.codex
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 2
    assert row["cached_usage_7d"] == 26
    assert row["cached_5h_resets_at"] is not None
    assert row["cached_7d_resets_at"] is not None
    assert row["usage_cached_at"] is not None


def test_fetch_codex_usage_records_error_on_failure(db, tmp_path, monkeypatch):
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def boom(**kwargs):
        raise cu.CodexUsageError("app-server exploded")

    monkeypatch.setattr(cu, "read_codex_rate_limits", boom)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is None
    row = db.get_account(acct["id"])
    assert row["last_error"] == "app-server exploded"
    assert row["consecutive_failures"] == 1


def test_fetch_codex_usage_all_none_is_soft_failure(db, tmp_path, monkeypatch):
    """An empty/partial app-server read must NOT stamp the account fresh+valid."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def empty(**kwargs):
        return {"rateLimits": {}}  # no primary/secondary -> all None

    monkeypatch.setattr(cu, "read_codex_rate_limits", empty)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is None
    row = db.get_account(acct["id"])
    assert row["consecutive_failures"] == 1
    assert row["cached_usage_5h"] is None  # not stamped with empty data


def test_fetch_codex_usage_skips_when_swap_locked(db, tmp_path, monkeypatch):
    """If a swap holds the lock, the poll skips (returns cached) — no app-server."""
    from jacked.codex.switching import _codex_swap_lock

    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    home = tmp_path / ".codex"
    home.mkdir()
    called = []

    async def fake_read(**kwargs):
        called.append(1)
        return RECORDED_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    # Hold the swap lock, then poll — it must skip without spawning app-server.
    with _codex_swap_lock(home):
        result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=home))
    assert result == {"_cached": True}
    assert called == []


# --------------------------------------------------------------------------
# Real subprocess round-trip through a fake app-server
# --------------------------------------------------------------------------

_FAKE_CODEX = """import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"id": mid, "result": {"userAgent": "fake-codex"}}), flush=True)
        # emit a notification too — the reader must skip it
        print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": mid, "result": {"rateLimits": {
            "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1782683811},
            "secondary": {"usedPercent": 33, "windowDurationMins": 10080, "resetsAt": 1783199300},
            "planType": "pro"}}}), flush=True)
    # 'initialized' notification: no response
"""


def _write_fake_codex(tmp_path):
    p = tmp_path / "codex_fake.py"
    # Use this interpreter's path in the shebang so the fake runs even when
    # `python3` isn't on PATH (skip cleanly nowhere — it must work, not error).
    p.write_text(f"#!{sys.executable}\n{_FAKE_CODEX}")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(p)


def test_read_rate_limits_real_subprocess_roundtrip(tmp_path):
    fake = _write_fake_codex(tmp_path)
    home = tmp_path / ".codex"
    home.mkdir()
    result = asyncio.run(cu.read_codex_rate_limits(home=home, codex_bin=fake))
    norm = cu.normalize_rate_limits(result)
    assert norm["five_hour"]["utilization"] == 7
    assert norm["seven_day"]["utilization"] == 33


def test_read_rate_limits_missing_binary_raises(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    with pytest.raises(cu.CodexUsageError):
        asyncio.run(
            cu.read_codex_rate_limits(home=home, codex_bin=str(tmp_path / "nope"))
        )


def _b64url(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()


def _root_auth(account_id, email):
    payload = {"email": email, "exp": 9999999999,
               "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    jwt = f"{_b64url({'alg': 'RS256'})}.{_b64url(payload)}.{sig}"
    return {
        "OPENAI_API_KEY": None, "auth_mode": "chatgpt",
        "tokens": {"id_token": jwt, "access_token": "a", "refresh_token": "r",
                   "account_id": account_id},
        "last_refresh": "2026-06-27T00:00:00Z",
    }


def test_fetch_usage_dispatches_codex_active_only(db, tmp_path, monkeypatch):
    """web.auth.fetch_usage routes a codex account to the Codex leaf, but ONLY
    the account live in the shared root — non-active ones return cached."""
    from jacked.web import auth as webauth

    webauth._account_usage_state.clear()  # avoid cross-test pacing residue
    home = tmp_path / ".codex"
    home.mkdir()
    a = db.create_account("a@x.com", "codex-managed", 9999999999,
                          provider="codex", organization_uuid="acct-A")
    b = db.create_account("b@x.com", "codex-managed", 9999999999,
                          provider="codex", organization_uuid="acct-B")
    (home / "auth.json").write_text(json.dumps(_root_auth("acct-A", "a@x.com")))
    monkeypatch.setenv("CODEX_HOME", str(home))

    called = []

    async def fake_fetch(account_id, db_, state=None):
        called.append(account_id)
        return {"ok": True}

    monkeypatch.setattr("jacked.codex.usage.fetch_codex_usage", fake_fetch)

    # Non-active B → cached sentinel, NOT delegated.
    assert asyncio.run(webauth.fetch_usage(b["id"], db)) == {"_cached": True}
    assert b["id"] not in called
    # Active A (live in the root) → delegated to the Codex leaf.
    assert asyncio.run(webauth.fetch_usage(a["id"], db)) == {"ok": True}
    assert called == [a["id"]]


def test_read_rate_limits_passes_codex_home_env(tmp_path):
    # A fake that echoes CODEX_HOME back as the plan, proving env wiring.
    script = tmp_path / "echo_home.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, json, os\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    m=json.loads(line); mid=m.get('id')\n"
        "    if m.get('method')=='initialize':\n"
        "        print(json.dumps({'id':mid,'result':{}}), flush=True)\n"
        "    elif m.get('method')=='account/rateLimits/read':\n"
        "        print(json.dumps({'id':mid,'result':{'rateLimits':{'planType':os.environ.get('CODEX_HOME','')}}}), flush=True)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    home = tmp_path / "my-codex-home"
    home.mkdir()
    result = asyncio.run(cu.read_codex_rate_limits(home=home, codex_bin=str(script)))
    assert result["rateLimits"]["planType"] == str(home)
