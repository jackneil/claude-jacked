"""Truth table for the credential-mutation scope policy.

``jacked/api/remote_access.py`` is deliberately pure: it takes a client host
plus the persisted remote-access setting and answers "may this client switch
accounts?" with no Request, no DB, and no network. That makes the security
decision itself testable as a matrix, so the route layer only has to prove it
wires the right three values in.
"""

import ipaddress
import logging
import sqlite3

import pytest

from jacked.api.remote_access import (
    DEFAULT_SCOPE,
    LOOPBACK_HOSTS,
    TAILSCALE_ULA,
    SwitchRateLimiter,
    client_in_scope,
    credential_mutation_allowed,
    credential_mutation_decision,
    credential_policy_editable,
    read_enabled_scope,
)

# A real tailnet address (CGNAT 100.64.0.0/10) and a LAN address that is NOT
# on the tailnet — the pair the whole policy turns on.
TAILNET_IP = "100.116.47.72"
LAN_IP = "192.168.42.5"
TAILNET_IPV6 = "fd7a:115c:a1e0::1"


class _FakeDB:
    """Minimal stand-in for Database: remote_access_* are plain string settings."""

    def __init__(self, **settings):
        self._settings = settings

    def get_setting(self, key):
        return self._settings.get(key)


# ---------------------------------------------------------------------------
# read_enabled_scope
# ---------------------------------------------------------------------------


def test_read_enabled_scope_without_a_db_is_off():
    assert read_enabled_scope(None) == (False, "tailscale")


def test_read_enabled_scope_absent_keys_are_off():
    assert read_enabled_scope(_FakeDB()) == (False, "tailscale")


@pytest.mark.parametrize(
    "stored,expected",
    [("true", True), ("false", False), ("TRUE", False), ("1", False), (None, False)],
)
def test_read_enabled_scope_uses_the_bare_string_convention(stored, expected):
    db = _FakeDB(remote_access_enabled=stored)
    assert read_enabled_scope(db)[0] is expected


@pytest.mark.parametrize(
    "stored,expected",
    [("all", "all"), ("tailscale", "tailscale"), ("bogus", "tailscale"), (None, "tailscale")],
)
def test_read_enabled_scope_falls_back_to_tailscale(stored, expected):
    db = _FakeDB(remote_access_enabled="true", remote_access_scope=stored)
    assert read_enabled_scope(db)[1] == expected


# ---------------------------------------------------------------------------
# client_in_scope
# ---------------------------------------------------------------------------


def test_client_in_scope_all_accepts_any_parsable_address():
    assert client_in_scope(LAN_IP, "all") is True
    assert client_in_scope(TAILNET_IP, "all") is True
    assert client_in_scope("2001:db8::1", "all") is True


def test_client_in_scope_tailscale_accepts_cgnat_ipv4():
    assert client_in_scope(TAILNET_IP, "tailscale") is True
    assert client_in_scope("100.64.0.1", "tailscale") is True
    assert client_in_scope("100.127.255.254", "tailscale") is True


def test_client_in_scope_tailscale_rejects_lan_ipv4():
    assert client_in_scope(LAN_IP, "tailscale") is False
    assert client_in_scope("10.0.0.5", "tailscale") is False
    # Just outside 100.64.0.0/10 on both sides.
    assert client_in_scope("100.63.255.255", "tailscale") is False
    assert client_in_scope("100.128.0.1", "tailscale") is False


def test_client_in_scope_tailscale_accepts_the_ipv6_ula_prefix():
    assert client_in_scope(TAILNET_IPV6, "tailscale") is True
    assert client_in_scope("fd7a:115c:a1e0:ab12:4843:cd96:6274:2f01", "tailscale") is True


def test_client_in_scope_tailscale_rejects_other_ipv6():
    assert client_in_scope("2001:db8::1", "tailscale") is False
    assert client_in_scope("fd00::1", "tailscale") is False


@pytest.mark.parametrize("host", ["hank-llm", "", None, "not an ip", "100.64.0.9/10"])
@pytest.mark.parametrize("scope", ["tailscale", "all"])
def test_client_in_scope_rejects_unparsable_hosts(host, scope):
    assert client_in_scope(host, scope) is False


@pytest.mark.parametrize("scope", ["", "lan", "TAILSCALE", None, "everything"])
def test_client_in_scope_treats_unknown_scopes_as_tailscale(scope):
    """Matches read_enabled_scope's own default, so a corrupted settings row
    fails closed to the narrow scope instead of opening the dashboard up."""
    assert client_in_scope(TAILNET_IP, scope) is True
    assert client_in_scope(LAN_IP, scope) is False


# ---------------------------------------------------------------------------
# credential_mutation_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", list(LOOPBACK_HOSTS))
@pytest.mark.parametrize("enabled,scope", [(False, "tailscale"), (True, "tailscale"), (True, "all")])
def test_loopback_is_always_allowed(host, enabled, scope):
    assert credential_mutation_allowed(host, enabled, scope) is True


def test_loopback_is_matched_case_insensitively():
    assert credential_mutation_allowed("LOCALHOST", False, "tailscale") is True
    assert credential_mutation_allowed("::1", False, "tailscale") is True


def test_remote_denied_while_remote_access_is_off():
    assert credential_mutation_allowed(TAILNET_IP, False, "tailscale") is False
    assert credential_mutation_allowed(TAILNET_IP, False, "all") is False
    assert credential_mutation_allowed(LAN_IP, False, "all") is False


def test_scope_all_allows_any_remote_client():
    assert credential_mutation_allowed(LAN_IP, True, "all") is True
    assert credential_mutation_allowed(TAILNET_IP, True, "all") is True


def test_scope_tailscale_allows_only_tailnet_clients():
    assert credential_mutation_allowed(TAILNET_IP, True, "tailscale") is True
    assert credential_mutation_allowed(TAILNET_IPV6, True, "tailscale") is True
    assert credential_mutation_allowed(LAN_IP, True, "tailscale") is False
    assert credential_mutation_allowed("2001:db8::1", True, "tailscale") is False


@pytest.mark.parametrize("host", ["hank-llm", "", None])
@pytest.mark.parametrize("enabled,scope", [(False, "tailscale"), (True, "tailscale"), (True, "all")])
def test_unparsable_or_missing_host_is_denied(host, enabled, scope):
    assert credential_mutation_allowed(host, enabled, scope) is False


@pytest.mark.parametrize("scope", ["", "lan", None, "everything"])
def test_unknown_scope_is_treated_as_tailscale(scope):
    assert credential_mutation_allowed(TAILNET_IP, True, scope) is True
    assert credential_mutation_allowed(LAN_IP, True, scope) is False


# ---------------------------------------------------------------------------
# Constants + doctests
# ---------------------------------------------------------------------------


def test_ipv4_range_is_shared_with_the_bind_planner():
    """One definition of Tailscale's CGNAT range, not two that can drift."""
    from jacked.api import remote_access
    from jacked.service import bind

    assert remote_access.TAILSCALE_CGNAT is bind.TAILSCALE_CGNAT
    assert bind.TAILSCALE_CGNAT == ipaddress.ip_network("100.64.0.0/10")


def test_ipv6_range_is_tailscales_ula_prefix():
    assert TAILSCALE_ULA == ipaddress.ip_network("fd7a:115c:a1e0::/48")


def test_remote_access_doctests_run():
    """CI runs bare pytest with testpaths=["tests"] and no --doctest-modules,
    so the policy truth table in the module's docstrings would otherwise
    never execute."""
    import doctest

    from jacked.api import remote_access

    results = doctest.testmod(remote_access)
    assert results.failed == 0
    assert results.attempted > 0


# ---------------------------------------------------------------------------
# A settings DB that cannot be read must not 500 the credential routes
# ---------------------------------------------------------------------------


class _BrokenDB:
    def get_setting(self, key):
        raise sqlite3.OperationalError("database is locked")


def test_read_enabled_scope_fails_closed_on_a_broken_db(caplog):
    """The docstring promises fail-closed, and the sibling read in
    service/bind.py guards the identical keys. A locked settings DB must
    deny remote mutation, not turn every polled route into a 500."""
    with caplog.at_level(logging.WARNING, logger="jacked.api.remote_access"):
        assert read_enabled_scope(_BrokenDB()) == (False, "tailscale")
    assert "remote-access" in caplog.text.lower()


def test_read_enabled_scope_error_log_carries_no_secret(caplog):
    import logging

    db = _BrokenDB()
    with caplog.at_level(logging.WARNING, logger="jacked.api.remote_access"):
        assert read_enabled_scope(db) == (False, DEFAULT_SCOPE)
    text = caplog.text
    # The failure class is named so an operator can act on it ...
    assert "OperationalError" in text or "database is locked" in text
    # ... and no setting value or credential-looking token is echoed.
    for secret in ("remote_access_scope", "sk-ant", "refresh_token", "true"):
        assert secret not in text, secret


# ---------------------------------------------------------------------------
# credential_policy_editable: who may flip the switch itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", list(LOOPBACK_HOSTS))
def test_policy_is_editable_from_loopback(host):
    assert credential_policy_editable(host) is True


@pytest.mark.parametrize("host", [TAILNET_IP, LAN_IP, TAILNET_IPV6, "hank-llm", "", None])
def test_policy_is_not_editable_from_anywhere_else(host):
    """The switch that grants remote credential control is flipped only at
    the host, whatever the setting currently says."""
    assert credential_policy_editable(host) is False


def test_policy_editable_is_case_insensitive():
    assert credential_policy_editable("LocalHost") is True


# ---------------------------------------------------------------------------
# credential_mutation_decision: the reason behind the answer
# ---------------------------------------------------------------------------


def test_decision_allows_loopback_with_no_reason():
    assert credential_mutation_decision("127.0.0.1", False, "tailscale") == (True, None)
    assert credential_mutation_decision("127.0.0.1", True, "all") == (True, None)


def test_decision_reports_remote_access_off():
    assert credential_mutation_decision(TAILNET_IP, False, "tailscale") == (
        False,
        "remote_access_off",
    )
    assert credential_mutation_decision(LAN_IP, False, "all") == (
        False,
        "remote_access_off",
    )


def test_decision_reports_outside_scope():
    assert credential_mutation_decision(LAN_IP, True, "tailscale") == (
        False,
        "outside_scope",
    )
    assert credential_mutation_decision("2001:db8::1", True, "tailscale") == (
        False,
        "outside_scope",
    )
    # An unparsable host is outside every scope, not "remote access off".
    assert credential_mutation_decision("hank-llm", True, "all") == (
        False,
        "outside_scope",
    )


def test_decision_allows_in_scope_remote_with_no_reason():
    assert credential_mutation_decision(TAILNET_IP, True, "tailscale") == (True, None)
    assert credential_mutation_decision(LAN_IP, True, "all") == (True, None)


def test_decision_and_boolean_helper_never_disagree():
    for host in [TAILNET_IP, LAN_IP, TAILNET_IPV6, "127.0.0.1", "hank-llm", None]:
        for enabled in (True, False):
            for scope in ("tailscale", "all"):
                allowed, reason = credential_mutation_decision(host, enabled, scope)
                assert allowed is credential_mutation_allowed(host, enabled, scope)
                assert (reason is None) is allowed


# ---------------------------------------------------------------------------
# SwitchRateLimiter
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_rate_limiter_allows_up_to_the_limit():
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=6, window=60.0, clock=clock)
    for _ in range(6):
        assert limiter.check(TAILNET_IP) == (True, 0.0)


def test_rate_limiter_blocks_the_next_request_with_a_retry_after():
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=6, window=60.0, clock=clock)
    for _ in range(6):
        limiter.check(TAILNET_IP)
    clock.now += 10.0
    allowed, retry_after = limiter.check(TAILNET_IP)
    assert allowed is False
    # The oldest of the six is 10s old, so the budget frees in 50s.
    assert retry_after == pytest.approx(50.0)


def test_rate_limiter_retry_after_is_at_least_one_second():
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=1, window=60.0, clock=clock)
    limiter.check(TAILNET_IP)
    clock.now += 59.9
    allowed, retry_after = limiter.check(TAILNET_IP)
    assert allowed is False
    assert retry_after >= 1.0


def test_rate_limiter_window_rolls_over():
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=2, window=60.0, clock=clock)
    assert limiter.check(TAILNET_IP)[0] is True
    assert limiter.check(TAILNET_IP)[0] is True
    assert limiter.check(TAILNET_IP)[0] is False
    clock.now += 60.1
    assert limiter.check(TAILNET_IP)[0] is True


def test_rate_limiter_is_per_host():
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=1, window=60.0, clock=clock)
    assert limiter.check(TAILNET_IP)[0] is True
    assert limiter.check(TAILNET_IP)[0] is False
    # A different peer has its own budget.
    assert limiter.check("100.64.0.9")[0] is True


def test_rate_limiter_does_not_grow_without_bound():
    """Expired host buckets are dropped, so a scanner cannot grow the map
    forever just by rotating source addresses."""
    clock = _Clock()
    limiter = SwitchRateLimiter(limit=6, window=60.0, clock=clock)
    for i in range(50):
        limiter.check(f"100.64.0.{i}")
    clock.now += 61.0
    limiter.check(TAILNET_IP)
    assert len(limiter._hits) == 1
